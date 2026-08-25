"""Live-API integration checks.

Deselected by default (``pyproject.toml`` sets ``addopts = "-m 'not integration'"``).
Run them explicitly::

    pytest -m integration

They contact ``www.ebi.ac.uk`` and ``ftp.ebi.ac.uk``, so they are slow, need a
network, and will fail when EBI is down -- which is precisely why they are not
part of the default suite.

Their purpose is to detect upstream drift: a changed field name, a withdrawn
endpoint, a reorganised FTP layout. Each test asserts the *contract* GWASPoker
depends on, not a specific value that the Catalog is free to update.
"""

from __future__ import annotations

import pytest

from gwaspoker.catalog.models import ApiAvailability
from gwaspoker.catalog.rest_api import GwasCatalogClient
from gwaspoker.catalog.sumstats_api import SummaryStatisticsAssessor
from gwaspoker.config import GWASPokerConfig
from gwaspoker.download.resolver import SummaryStatisticsResolver
from gwaspoker.http import HttpClient
from gwaspoker.probe.remote import RemoteProber

pytestmark = pytest.mark.integration

#: A stable, long-published study with harmonised summary statistics.
ACCESSION = "GCST90038646"
#: A study whose data file declares GWAS-SSF v1.0 conformance.
SSF_ACCESSION = "GCST90271311"


@pytest.fixture(scope="module")
def config() -> GWASPokerConfig:
    return GWASPokerConfig()


@pytest.fixture(scope="module")
def http(config) -> HttpClient:
    client = HttpClient(config)
    yield client
    client.close()


# ----------------------------------------------------------------------
# Metadata API
# ----------------------------------------------------------------------


def test_metadata_endpoint_is_reachable(config, http) -> None:
    metadata = GwasCatalogClient(config, http).api_metadata()
    assert metadata is not None, "the v2 /metadata endpoint did not answer"
    assert metadata.get("version")
    assert metadata.get("data_release_date")


def test_study_metadata_normalizes(config, http) -> None:
    study = GwasCatalogClient(config, http).get_study(ACCESSION)
    assert study.study_accession == ACCESSION
    assert study.reported_trait
    assert study.summary_statistics_available is True
    assert study.api_source.startswith("gwas_catalog_rest_v")


def test_trait_index_resolves_free_text(config, http) -> None:
    traits = GwasCatalogClient(config, http).search_traits("migraine")
    assert traits, "the trait index returned no ontology term for 'migraine'"
    assert any("migraine" in t.label.lower() for t in traits)
    assert all(t.efo_id for t in traits)


def test_studies_by_efo_trait(config, http) -> None:
    studies = GwasCatalogClient(config, http).studies_by_efo_trait("migraine disorder", limit=5)
    assert studies
    assert all(s.study_accession.startswith("GCST") for s in studies)


# ----------------------------------------------------------------------
# Summary Statistics API status
# ----------------------------------------------------------------------


def test_legacy_sumstats_api_is_still_deprecated(config, http) -> None:
    """Documents the upstream state GWASPoker's design responds to.

    If this ever stops being DEPRECATED, a replacement API has appeared and the
    structured assessment route should be revisited -- see docs/API_SOURCES.md.
    """
    assessor = SummaryStatisticsAssessor(config, http)
    availability, size, latency, fields, error = assessor.query_legacy_sumstats_api(ACCESSION)
    assert availability is ApiAvailability.DEPRECATED, (
        f"the Summary Statistics API returned {availability.value}, not the expected "
        "deprecation. Re-read docs/API_SOURCES.md and update the adapter."
    )


# ----------------------------------------------------------------------
# FTP layout and file resolution
# ----------------------------------------------------------------------


def test_ftp_directory_convention_holds(config, http) -> None:
    resolver = SummaryStatisticsResolver(config, http)
    url = resolver.directory_url_for(ACCESSION)
    candidates = resolver.list_directory(url)
    names = [c.name for c in candidates]
    assert any(c.kind == "data" for c in candidates)
    assert any(n.endswith("-meta.yaml") for n in names), "no GWAS-SSF sidecar found"
    assert "md5sum.txt" in names


def test_resolver_prefers_the_harmonised_file(config, http) -> None:
    resolved = SummaryStatisticsResolver(config, http).resolve(ACCESSION, harmonised="auto")
    assert resolved.is_harmonised
    assert ".h.tsv" in resolved.name
    assert resolved.selection_reason
    assert resolved.metadata_url


def test_ssf_metadata_sidecar_parses(config, http) -> None:
    resolver = SummaryStatisticsResolver(config, http)
    resolved = resolver.resolve(SSF_ACCESSION, harmonised="no")
    meta = SummaryStatisticsAssessor(config, http).fetch_ssf_metadata(resolved.metadata_url)
    assert meta is not None
    assert meta.file_type
    assert meta.bytes_received < 5000, "the sidecar should be under a few kilobytes"


# ----------------------------------------------------------------------
# Bounded probing
# ----------------------------------------------------------------------


def test_range_requests_are_honoured(config, http) -> None:
    """The whole value proposition depends on this."""
    resolved = SummaryStatisticsResolver(config, http).resolve(ACCESSION, harmonised="auto")
    probe = RemoteProber(config, http).probe_url(resolved.url, probe_bytes=65_536)

    assert probe.succeeded, probe.error
    assert probe.transfer.range_used, "ftp.ebi.ac.uk did not honour a Range request"
    assert probe.transfer.received_bytes <= 65_536
    assert probe.transfer.remote_file_size > 1_000_000
    assert probe.transfer.transfer_reduction > 0.99


def test_gzip_header_recovered_from_a_small_prefix(config, http) -> None:
    resolved = SummaryStatisticsResolver(config, http).resolve(ACCESSION, harmonised="auto")
    probe = RemoteProber(config, http).probe_url(resolved.url, probe_bytes=65_536)

    assert probe.header is not None
    assert len(probe.header.raw_header) > 5
    assert probe.mapping.resolved, "no column mapped to a canonical concept"


# ----------------------------------------------------------------------
# End-to-end assessment
# ----------------------------------------------------------------------


def test_ssf_study_needs_no_data_bytes(config) -> None:
    """The API-sufficient branch, end to end."""
    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        result = service.assess(SSF_ACCESSION, harmonised="no")

    assert result.api_assessment.sufficient_for_prs_assessment
    assert not result.probe_performed
    assert result.probe is None
    assert result.readiness.verdict.value in ("READY", "PARTIAL")


def test_forced_probe_agrees_with_the_declared_fields(config) -> None:
    """The benchmark's central comparison, on one study.

    A GWAS-SSF declaration and the file's observed header must reach the same
    verdict. Disagreement means either the file does not honour its declaration
    or GWASPoker's header detection is wrong -- both worth knowing.
    """
    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        api_only = service.assess(SSF_ACCESSION, harmonised="no")
        probed = service.assess(SSF_ACCESSION, harmonised="no", force_probe=True)

    assert probed.probe_performed
    assert probed.readiness.verdict is api_only.readiness.verdict


def test_pre_ssf_study_triggers_a_probe(config) -> None:
    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        result = service.assess(ACCESSION)

    assert not result.api_assessment.sufficient_for_prs_assessment
    assert result.probe_required
    assert result.probe_performed
    assert result.readiness.evidence_source == "file_probe"


def test_search_end_to_end(config) -> None:
    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        results = service.search("migraine", limit=5, summary_stats_only=True)

    assert results
    assert all(r.study.summary_statistics_available for r in results)
    assert any(r.study.samples.resolved for r in results)


def test_unknown_accession_is_reported_as_such(config) -> None:
    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        result = service.assess("GCST99999999")

    assert result.error
    assert result.failure_category is not None
    assert result.readiness is None
