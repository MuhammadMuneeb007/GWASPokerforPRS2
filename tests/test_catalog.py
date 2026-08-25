"""Catalog API adapters: response normalization, fallback and honest failures.

Every HTTP call is mocked. The point of these tests is that a change in the
upstream schema breaks exactly one layer -- the adapter -- and that a transient
server error is never reported as "the study does not exist".
"""

from __future__ import annotations

import json

import pytest
import responses

from gwaspoker.catalog.models import ApiAvailability, SsfMetadata
from gwaspoker.catalog.rest_api import GwasCatalogClient, is_accession
from gwaspoker.catalog.sumstats_api import (
    SSF_MANDATORY_FIELDS,
    SummaryStatisticsAssessor,
    _fields_cover_prs,
    parse_ssf_metadata,
)
from gwaspoker.failures import AccessionNotFoundError, CatalogApiError
from gwaspoker.http import HttpClient

V1 = "https://www.ebi.ac.uk/gwas/rest/api"
V2 = "https://www.ebi.ac.uk/gwas/rest/api/v2"
SOLR = "https://www.ebi.ac.uk/gwas/api/search"
SUMSTATS = "https://www.ebi.ac.uk/gwas/summary-statistics/api"

# Trimmed from the live response for GCST90038646, recorded 2026-08-24.
V1_STUDY = {
    "cohort": "UKB",
    "initialSampleSize": "13,971 cases, 470,627 controls",
    "snpCount": 9886868,
    "accessionId": "GCST90038646",
    "fullPvalueSet": True,
    "ancestries": [
        {
            "type": "initial",
            "numberOfIndividuals": 484598,
            "ancestralGroups": [{"ancestralGroup": "NR"}],
            "countryOfRecruitment": [{"countryName": "U.K."}],
        }
    ],
    "diseaseTrait": {"trait": "Migraine"},
    "genotypingTechnologies": [{"genotypingTechnology": "Genome-wide genotyping array"}],
    "replicationSampleSize": "NA",
    "publicationInfo": {
        "pubmedId": "33959723",
        "publicationDate": "2021-04-08",
        "publication": "Nat Aging",
        "title": "Common genetic associations between age-related diseases.",
        "author": {"fullname": "Dönertaş HM"},
    },
}

# Shaped after the v2 StudyDto schema.
V2_STUDY = {
    "accession_id": "GCST90038646",
    "disease_trait": "Migraine",
    "initial_sample_size": "13,971 cases, 470,627 controls",
    "replication_sample_size": "NA",
    "pubmed_id": 33959723,
    "full_summary_stats_available": True,
    "full_summary_stats": (
        "http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/"
        "GCST90038001-GCST90039000/GCST90038646"
    ),
    "efo_traits": [{"efo_id": "EFO_0003821", "efo_trait": "migraine disorder"}],
    "discovery_ancestry": ["NR"],
    "genotyping_technologies": ["Genome-wide genotyping array"],
    "cohort": ["UKB"],
    "snp_count": 9886868,
}


@pytest.fixture
def client(config):
    return GwasCatalogClient(config, HttpClient(config))


# ----------------------------------------------------------------------
# Accession recognition
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GCST90038646", True),
        ("gcst90038646", True),
        ("  GCST006867 ", True),
        ("GCST", False),
        ("https://example.org/x.tsv", False),
        ("migraine", False),
    ],
)
def test_is_accession(value, expected) -> None:
    assert is_accession(value) is expected


# ----------------------------------------------------------------------
# v1 normalization
# ----------------------------------------------------------------------


@responses.activate
def test_v1_study_normalization(client) -> None:
    responses.add(responses.GET, f"{V2}/studies/GCST90038646", status=500)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646", json=V1_STUDY, status=200)
    responses.add(
        responses.GET,
        f"{V1}/studies/GCST90038646/efoTraits",
        json={
            "_embedded": {
                "efoTraits": [
                    {
                        "trait": "migraine disorder",
                        "shortForm": "MONDO_0005277",
                        "uri": "http://purl.obolibrary.org/obo/MONDO_0005277",
                    }
                ]
            }
        },
        status=200,
    )

    study = client.get_study("GCST90038646")

    assert study.study_accession == "GCST90038646"
    assert study.reported_trait == "Migraine"
    assert study.summary_statistics_available is True
    assert study.pubmed_id == "33959723"
    assert study.study_year == 2021
    assert study.first_author == "Dönertaş HM"
    assert study.api_source == "gwas_catalog_rest_v1"
    assert study.discovery_sample_size == 484598
    assert study.mapped_traits[0].efo_id == "MONDO_0005277"


@responses.activate
def test_na_markers_become_none(client) -> None:
    """The Catalog writes 'NA' and 'NR'; those are absences, not values."""
    responses.add(responses.GET, f"{V2}/studies/GCST90038646", status=500)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646", json=V1_STUDY, status=200)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646/efoTraits", json={}, status=200)
    study = client.get_study("GCST90038646")
    assert study.replication_sample_description is None


# ----------------------------------------------------------------------
# v2 normalization and fallback
# ----------------------------------------------------------------------


@responses.activate
def test_v2_is_preferred_and_carries_the_file_location(client) -> None:
    """Only v2 exposes full_summary_stats, which is why it is tried first."""
    responses.add(responses.GET, f"{V2}/studies/GCST90038646", json=V2_STUDY, status=200)
    responses.add(responses.GET, f"{V2}/publications/33959723", status=404)

    study = client.get_study("GCST90038646")
    assert study.api_source == "gwas_catalog_rest_v2"
    assert study.summary_statistics_location.endswith("GCST90038646")
    assert study.mapped_traits[0].efo_id == "EFO_0003821"


@responses.activate
def test_v2_failure_falls_back_to_v1(client) -> None:
    """At audit time /v2/studies returned HTTP 500 for every accession."""
    responses.add(responses.GET, f"{V2}/studies/GCST90038646", status=500)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646", json=V1_STUDY, status=200)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646/efoTraits", json={}, status=200)

    study = client.get_study("GCST90038646")
    assert study.api_source == "gwas_catalog_rest_v1"


@responses.activate
def test_v2_failure_is_remembered_for_the_session(client) -> None:
    responses.add(responses.GET, f"{V2}/studies/GCST90038646", status=500)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646", json=V1_STUDY, status=200)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646/efoTraits", json={}, status=200)
    client.get_study("GCST90038646")
    assert client._v2_healthy is False
    assert client._route_order()[0] == "v1"


# ----------------------------------------------------------------------
# Honest failure reporting
# ----------------------------------------------------------------------


@responses.activate
def test_definite_404_raises_not_found(client) -> None:
    responses.add(responses.GET, f"{V2}/studies/GCST99999999", status=404)
    responses.add(responses.GET, f"{V1}/studies/GCST99999999", status=404)
    with pytest.raises(AccessionNotFoundError):
        client.get_study("GCST99999999")


@responses.activate
def test_server_errors_do_not_claim_the_study_is_missing(client) -> None:
    """A 500 from both routes is a server problem, not evidence of absence."""
    responses.add(responses.GET, f"{V2}/studies/GCST90038646", status=503)
    responses.add(responses.GET, f"{V1}/studies/GCST90038646", status=503)
    with pytest.raises(CatalogApiError) as excinfo:
        client.get_study("GCST90038646")
    assert not isinstance(excinfo.value, AccessionNotFoundError)


def test_malformed_accession_is_rejected_without_a_request(client) -> None:
    with pytest.raises(AccessionNotFoundError):
        client.get_study("not-an-accession")


# ----------------------------------------------------------------------
# Trait discovery
# ----------------------------------------------------------------------


@responses.activate
def test_search_traits_reads_the_ontology_index(client) -> None:
    responses.add(
        responses.GET,
        SOLR,
        json={
            "response": {
                "docs": [
                    {"resourcename": "study", "accessionId": "GCST012022"},
                    {
                        "resourcename": "trait",
                        "mappedTrait": "migraine disorder",
                        "shortForm": ["MONDO_0005277"],
                        "mappedUri": "http://purl.obolibrary.org/obo/MONDO_0005277",
                    },
                ]
            }
        },
        status=200,
    )
    traits = client.search_traits("migraine")
    assert [t.label for t in traits] == ["migraine disorder"]
    assert traits[0].efo_id == "MONDO_0005277"


@responses.activate
def test_search_traits_returns_empty_on_api_failure(client) -> None:
    """A failed lookup returns nothing and is logged; it never raises upward."""
    responses.add(responses.GET, SOLR, status=502)
    assert client.search_traits("migraine") == []

    from gwaspoker.failures import FAILURES

    assert len(FAILURES) == 1


@responses.activate
def test_find_study_accessions_for_text(client) -> None:
    responses.add(
        responses.GET,
        SOLR,
        json={
            "response": {
                "docs": [
                    {"resourcename": "study", "accessionId": "GCST012022"},
                    {"resourcename": "study", "accessionId": "GCST012023"},
                    {"resourcename": "trait", "mappedTrait": "migraine disorder"},
                ]
            }
        },
        status=200,
    )
    assert client.find_study_accessions_for_text("migraine") == ["GCST012022", "GCST012023"]


@responses.activate
def test_studies_by_efo_trait_falls_back_and_filters(client) -> None:
    """v1 cannot filter on summary-statistics availability, so we do it here."""
    responses.add(responses.GET, f"{V2}/studies", status=500)
    with_stats = dict(V1_STUDY, accessionId="GCST000001", fullPvalueSet=True)
    without = dict(V1_STUDY, accessionId="GCST000002", fullPvalueSet=False)
    responses.add(
        responses.GET,
        f"{V1}/studies/search/findByEfoTrait",
        json={
            "_embedded": {"studies": [with_stats, without]},
            "page": {"totalPages": 1},
        },
        status=200,
    )
    responses.add(responses.GET, f"{V1}/studies/GCST000001/efoTraits", json={}, status=200)
    responses.add(responses.GET, f"{V1}/studies/GCST000002/efoTraits", json={}, status=200)

    studies = client.studies_by_efo_trait("migraine disorder", limit=10, summary_stats_only=True)
    assert [s.study_accession for s in studies] == ["GCST000001"]


# ----------------------------------------------------------------------
# GWAS-SSF metadata
# ----------------------------------------------------------------------


def test_parse_ssf_metadata(fixture_text) -> None:
    meta = parse_ssf_metadata(fixture_text("ssf_meta.yaml"), "https://example.org/meta.yaml")
    assert meta.is_ssf
    assert meta.ssf_status == "GWAS-SSF"
    assert meta.file_type == "GWAS-SSF v1.0"
    assert meta.genome_assembly == "GRCh38"
    assert meta.sample_size == 123456
    assert meta.case_count == 12345
    assert meta.control_count == 111111
    assert meta.case_control_study is True
    assert meta.md5sum == "0123456789abcdef0123456789abcdef"
    assert meta.ancestry_categories == ("European",)
    assert meta.is_harmonised is False


def test_parse_pre_ssf_metadata(fixture_text) -> None:
    """A pre-GWAS-SSF file guarantees nothing about its columns."""
    meta = parse_ssf_metadata(fixture_text("pre_ssf_meta.yaml"))
    assert not meta.is_ssf
    assert meta.ssf_status == "pre-GWAS-SSF"
    assert meta.sample_size == 484598
    assert meta.case_count is None


def test_parse_ssf_metadata_rejects_non_mapping() -> None:
    with pytest.raises(ValueError):
        parse_ssf_metadata("- just\n- a\n- list\n")


# ----------------------------------------------------------------------
# Structured assessment
# ----------------------------------------------------------------------


@pytest.fixture
def assessor(config):
    return SummaryStatisticsAssessor(config, HttpClient(config))


@responses.activate
def test_ssf_declaration_is_sufficient(assessor, fixture_text) -> None:
    """The branch that makes GWASPoker cheap: a verdict with zero data bytes."""
    meta_url = "https://ftp.example.org/GCST90000001.tsv.gz-meta.yaml"
    responses.add(responses.GET, meta_url, body=fixture_text("ssf_meta.yaml"), status=200)
    responses.add(
        responses.GET,
        f"{SUMSTATS}/studies/GCST90000001/associations",
        status=410,
        body="This API has been deprecated.",
    )

    result = assessor.assess("GCST90000001", metadata_url=meta_url)

    assert result.available
    assert result.sufficient_for_prs_assessment
    assert result.route == "gwas_ssf_metadata"
    assert result.fields_available == SSF_MANDATORY_FIELDS
    assert result.bytes_received < 2000
    assert result.ssf_metadata.is_ssf


@responses.activate
def test_pre_ssf_declaration_is_insufficient(assessor, fixture_text) -> None:
    meta_url = "https://ftp.example.org/GCST90038646_buildGRCh37.tsv-meta.yaml"
    responses.add(responses.GET, meta_url, body=fixture_text("pre_ssf_meta.yaml"), status=200)
    responses.add(responses.GET, f"{SUMSTATS}/studies/GCST90038646/associations", status=410)

    result = assessor.assess("GCST90038646", metadata_url=meta_url)

    assert result.available  # metadata was retrievable
    assert not result.sufficient_for_prs_assessment  # but guarantees nothing
    assert any("file-level inspection" in note for note in result.notes)


@responses.activate
def test_deprecated_sumstats_api_is_reported_as_deprecated(assessor) -> None:
    """HTTP 410 is a permanent, documented withdrawal -- not 'unavailable'.

    Reporting it as unavailable would imply a transient fault and invite a
    pointless retry. The live endpoint returns 410 with the body
    'This API has been deprecated.'
    """
    responses.add(
        responses.GET,
        f"{SUMSTATS}/studies/GCST90038646/associations",
        status=410,
        body="This API has been deprecated.",
    )
    availability, size, latency, fields, error = assessor.query_legacy_sumstats_api("GCST90038646")

    assert availability is ApiAvailability.DEPRECATED
    assert availability is not ApiAvailability.SERVER_ERROR
    assert fields == ()
    assert "withdrawn" in error


@responses.activate
def test_sumstats_api_404_is_not_representation(assessor) -> None:
    responses.add(responses.GET, f"{SUMSTATS}/studies/GCST1/associations", status=404)
    availability, *_ = assessor.query_legacy_sumstats_api("GCST1")
    assert availability is ApiAvailability.NOT_REPRESENTED


@responses.activate
def test_sumstats_api_500_is_a_server_error(assessor) -> None:
    """A 500 must never be recorded as 'this study is not in the API'."""
    responses.add(responses.GET, f"{SUMSTATS}/studies/GCST1/associations", status=500)
    availability, *_ = assessor.query_legacy_sumstats_api("GCST1")
    assert availability is ApiAvailability.SERVER_ERROR
    assert availability is not ApiAvailability.NOT_REPRESENTED


@responses.activate
def test_working_sumstats_api_fields_are_extracted(assessor) -> None:
    """If a replacement API appears with this shape, the adapter reads it."""
    responses.add(
        responses.GET,
        f"{SUMSTATS}/studies/GCST1/associations",
        json={
            "_embedded": {
                "associations": {
                    "0": {
                        "chromosome": "1",
                        "base_pair_location": 12345,
                        "effect_allele": "A",
                        "other_allele": "G",
                        "beta": 0.1,
                        "p_value": 1e-6,
                    }
                }
            }
        },
        status=200,
    )
    availability, size, latency, fields, error = assessor.query_legacy_sumstats_api("GCST1")
    assert availability is ApiAvailability.AVAILABLE
    assert "effect_allele" in fields
    assert error is None


def test_fields_cover_prs() -> None:
    assert _fields_cover_prs(SSF_MANDATORY_FIELDS)
    assert _fields_cover_prs(("variant_id", "effect_allele", "other_allele", "beta", "p_value"))
    # No other allele: not enough to orient the effect.
    assert not _fields_cover_prs(("variant_id", "effect_allele", "beta", "p_value"))
    # No effect measure at all.
    assert not _fields_cover_prs(
        ("chromosome", "base_pair_location", "effect_allele", "other_allele")
    )


@responses.activate
def test_missing_metadata_is_reported_not_guessed(assessor) -> None:
    meta_url = "https://ftp.example.org/absent-meta.yaml"
    responses.add(responses.GET, meta_url, status=404)
    responses.add(responses.GET, f"{SUMSTATS}/studies/GCST1/associations", status=410)

    result = assessor.assess("GCST1", metadata_url=meta_url)
    assert not result.sufficient_for_prs_assessment
    assert result.ssf_metadata is None
    assert any("No GWAS-SSF metadata" in note for note in result.notes)


def test_assessment_serialises() -> None:
    from gwaspoker.catalog.models import ApiAssessment

    payload = ApiAssessment(
        study_accession="GCST1",
        available=True,
        availability=ApiAvailability.AVAILABLE,
        sufficient_for_prs_assessment=True,
        ssf_metadata=SsfMetadata(url="u", file_type="GWAS-SSF v1.0"),
    ).to_dict()
    assert payload["availability"] == "available"
    assert payload["ssf_metadata"]["ssf_status"] == "GWAS-SSF"
    json.dumps(payload)  # must be JSON-serialisable


# ----------------------------------------------------------------------
# Search: file / API / harmonised / GWAS-SSF availability
# ----------------------------------------------------------------------

FTP = "https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"
STUDY_DIR = f"{FTP}/GCST90000001-GCST90001000/GCST90000001/"


def _search_result(**study_kwargs):
    from gwaspoker.catalog.discovery import SearchResult
    from gwaspoker.catalog.models import Study

    defaults = {"study_accession": "GCST90000001", "summary_statistics_available": True}
    defaults.update(study_kwargs)
    return SearchResult(study=Study(**defaults))


@pytest.fixture
def service(config):
    from gwaspoker.catalog.discovery import DiscoveryService

    svc = DiscoveryService(config)
    yield svc
    svc.close()


@responses.activate
def test_file_check_populates_all_four_columns(service, fixture_text) -> None:
    """The happy path: file present, sidecar readable, harmonised published."""
    responses.add(responses.GET, STUDY_DIR, body=fixture_text("ftp_index.html"), status=200)
    responses.add(
        responses.GET,
        f"{STUDY_DIR}harmonised/",
        body=fixture_text("ftp_index_harmonised.html"),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{STUDY_DIR}harmonised/12345678-GCST90000001-EFO_0000001.h.tsv.gz-meta.yaml",
        body=fixture_text("ssf_meta.yaml"),
        status=200,
    )

    result = _search_result()
    service._check_file_availability(result)

    assert result.file_available is True
    assert result.api_available is True
    assert result.harmonised_available is True
    assert result.ssf_status == "GWAS-SSF"
    assert result.is_ssf is True


@responses.activate
def test_pre_ssf_file_reports_ssf_false(service, fixture_text) -> None:
    """A pre-GWAS-SSF sidecar still gives API=yes, but GWAS-SSF=no."""
    responses.add(responses.GET, STUDY_DIR, body=fixture_text("ftp_index.html"), status=200)
    responses.add(responses.GET, f"{STUDY_DIR}harmonised/", status=404)
    responses.add(
        responses.GET,
        f"{STUDY_DIR}GCST90000001_buildGRCh37.tsv-meta.yaml",
        body=fixture_text("pre_ssf_meta.yaml"),
        status=200,
    )

    result = _search_result()
    service._check_file_availability(result)

    assert result.file_available is True
    assert result.api_available is True
    assert result.harmonised_available is False
    assert result.ssf_status == "pre-GWAS-SSF"
    assert result.is_ssf is False


@responses.activate
def test_missing_sidecar_gives_api_false_and_ssf_unknown(service) -> None:
    """No sidecar: the structured route cannot answer, and SSF status is '?'.

    ``api_available`` is False (we looked, it was not there) while ``ssf_status``
    stays None (we could not establish it) -- a distinction the table renders as
    ``no`` versus ``?``.
    """
    index = (
        '<table><tr><td><a href="GCST90000001.tsv.gz">GCST90000001.tsv.gz</a></td>'
        "<td>2025-01-01</td><td>500M</td></tr></table>"
    )
    responses.add(responses.GET, STUDY_DIR, body=index, status=200)
    responses.add(responses.GET, f"{STUDY_DIR}harmonised/", status=404)

    result = _search_result()
    service._check_file_availability(result)

    assert result.file_available is True
    assert result.api_available is False
    assert result.ssf_status is None
    assert result.is_ssf is None


def test_study_without_summary_statistics_costs_no_requests(service) -> None:
    """A top-associations-only study has no directory, so nothing is fetched.

    ``responses`` is not activated here: any HTTP call would raise.
    """
    result = _search_result(summary_statistics_available=False)
    service._check_file_availability(result)

    assert result.file_available is False
    assert result.api_available is False
    assert result.harmonised_available is False
    assert result.ssf_status is None


@responses.activate
def test_unlistable_directory_is_recorded_not_raised(service) -> None:
    """A study the Catalog says has a file, but whose directory 404s."""
    responses.add(responses.GET, STUDY_DIR, status=404)

    result = _search_result()
    service._check_file_availability(result)

    assert result.file_available is False
    assert result.file_check_error
    assert result.api_available is None  # never established, not "absent"

    from gwaspoker.failures import FAILURES

    assert len(FAILURES) == 1


@responses.activate
def test_sidecar_improves_sample_counts(service, fixture_text) -> None:
    """The sidecar is fetched anyway, so let it supply authoritative counts."""
    from gwaspoker.catalog.models import ValueSource

    responses.add(responses.GET, STUDY_DIR, body=fixture_text("ftp_index.html"), status=200)
    responses.add(responses.GET, f"{STUDY_DIR}harmonised/", status=404)
    responses.add(
        responses.GET,
        f"{STUDY_DIR}GCST90000001_buildGRCh37.tsv-meta.yaml",
        body=fixture_text("ssf_meta.yaml"),
        status=200,
    )

    result = _search_result()
    service._check_file_availability(result)

    samples = result.study.samples
    assert samples.total == 123456
    assert samples.cases == 12345
    assert samples.total_source is ValueSource.SSF_METADATA
    assert result.study.genome_build == "GRCh38"


def test_search_result_serialises_the_new_fields() -> None:
    result = _search_result()
    result.file_available = True
    result.api_available = False
    result.harmonised_available = True
    result.ssf_status = "pre-GWAS-SSF"

    payload = result.to_dict()
    assert payload["file_available"] is True
    assert payload["api_available"] is False
    assert payload["harmonised_available"] is True
    assert payload["ssf_status"] == "pre-GWAS-SSF"
    json.dumps(payload, default=str)


def test_ssf_status_preserves_the_declared_string() -> None:
    """The Catalog uses at least two non-conformant values; they are distinct.

    ``pre-GWAS-SSF`` means the file predates the standard. ``non-GWAS-SSF``
    means it is not variant-level summary statistics at all -- GCST90081731
    (gene-based burden) declares exactly that. Collapsing the second into the
    first would misreport it and blur a benchmark stratum.
    """
    assert SsfMetadata(url="", file_type="GWAS-SSF v1.0").ssf_status == "GWAS-SSF"
    assert SsfMetadata(url="", file_type="pre-GWAS-SSF").ssf_status == "pre-GWAS-SSF"
    assert SsfMetadata(url="", file_type="non-GWAS-SSF").ssf_status == "non-GWAS-SSF"
    assert SsfMetadata(url="", file_type=None).ssf_status == "unknown"


def test_only_gwas_ssf_declaration_counts_as_conformant() -> None:
    assert SsfMetadata(url="", file_type="GWAS-SSF v1.0").is_ssf
    assert not SsfMetadata(url="", file_type="non-GWAS-SSF").is_ssf
    assert not SsfMetadata(url="", file_type="pre-GWAS-SSF").is_ssf


def test_non_ssf_declaration_is_not_sufficient(service, fixture_text) -> None:
    """A non-GWAS-SSF file must still be probed, exactly like a pre-SSF one."""
    from gwaspoker.catalog.sumstats_api import parse_ssf_metadata

    text = fixture_text("pre_ssf_meta.yaml").replace(
        "file_type: pre-GWAS-SSF", "file_type: non-GWAS-SSF"
    )
    meta = parse_ssf_metadata(text)
    assert meta.ssf_status == "non-GWAS-SSF"
    assert not meta.is_ssf
