"""Input classification: accession, direct URL, or local file.

One resolver serves every command. These tests pin the classification itself
and, more importantly, the invariant that follows from it: **a direct URL takes
exactly the same path as an accession once the file is located**, including the
byte bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from gwaspoker.inputs import (
    FETCHABLE_SCHEMES,
    InputResolutionError,
    InputType,
    is_direct_url,
    normalise_url,
    resolve_input,
)

URL = "https://some-consortium.org/results/gwas.txt.gz"


# ----------------------------------------------------------------------
# Recognition
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org/gwas.txt.gz",
        "http://example.org/gwas.tsv",
        "https://example.org/a/b/c.sumstats.gz?token=1",
        "ftp://ftp.ebi.ac.uk/pub/x.tsv",
        "  https://example.org/x.gz  ",
    ],
)
def test_urls_are_recognised(value) -> None:
    assert is_direct_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "GCST90012345",
        "gcst90012345",
        "study.tsv.gz",
        "C:/data/gwas.tsv",
        "/home/me/gwas.tsv",
        "",
        "not a url",
        "example.org/gwas.gz",  # no scheme
        "https://",  # no host
        "mailto:someone@example.org",
    ],
)
def test_non_urls_are_not_recognised(value) -> None:
    assert not is_direct_url(value)


def test_accessions_resolve_to_the_catalog_route() -> None:
    target = resolve_input("GCST90012345")
    assert target.input_type is InputType.GWAS_CATALOG_ACCESSION
    assert target.accession == "GCST90012345"
    assert target.needs_catalog_lookup
    assert target.url is None


def test_lowercase_accessions_are_normalised() -> None:
    assert resolve_input("gcst90012345").accession == "GCST90012345"


def test_direct_urls_skip_the_catalog() -> None:
    target = resolve_input(URL)
    assert target.input_type is InputType.DIRECT_URL
    assert target.url == URL
    assert target.accession is None
    assert not target.needs_catalog_lookup


def test_local_files_only_when_allowed(tmp_path) -> None:
    path = tmp_path / "gwas.tsv"
    path.write_text("CHR\tBP\n1\t2\n", encoding="utf-8")

    target = resolve_input(str(path), allow_local=True)
    assert target.input_type is InputType.LOCAL_FILE
    assert target.path == path

    # `assess` and `probe` do not take local files, so the same string is an error.
    with pytest.raises(InputResolutionError):
        resolve_input(str(path), allow_local=False)


def test_unresolvable_input_raises_with_a_useful_message() -> None:
    with pytest.raises(InputResolutionError) as excinfo:
        resolve_input("definitely not a thing")
    assert "GCST accession" in str(excinfo.value)


def test_empty_input_raises() -> None:
    with pytest.raises(InputResolutionError):
        resolve_input("   ")


def test_a_url_is_never_mistaken_for_a_path(tmp_path) -> None:
    """URLs are checked first, so a URL-shaped string is never a filename."""
    target = resolve_input(URL, allow_local=True)
    assert target.input_type is InputType.DIRECT_URL


# ----------------------------------------------------------------------
# ftp:// normalisation
# ----------------------------------------------------------------------


def test_ftp_urls_are_rewritten_to_https() -> None:
    """requests has no FTP adapter, and repositories serve the same paths over HTTPS.

    Before the shared resolver, ``assess`` and ``probe`` accepted ``ftp://`` and
    then died inside requests with ``InvalidSchema``, while ``download`` and
    ``scan`` rejected it outright.
    """
    target = resolve_input("ftp://ftp.ebi.ac.uk/pub/databases/gwas/x.tsv.gz")

    assert target.input_type is InputType.DIRECT_URL
    assert target.url.startswith("https://")
    assert target.url == "https://ftp.ebi.ac.uk/pub/databases/gwas/x.tsv.gz"
    assert target.raw.startswith("ftp://")  # what the user typed is preserved
    assert target.normalisation_note
    assert "https" in target.normalisation_note


def test_https_urls_are_left_alone() -> None:
    url, note = normalise_url(URL)
    assert url == URL
    assert note is None


def test_every_resolved_url_uses_a_fetchable_scheme() -> None:
    from urllib.parse import urlparse

    for value in (URL, "http://example.org/x.gz", "ftp://example.org/x.gz"):
        resolved = resolve_input(value)
        assert urlparse(resolved.url).scheme in FETCHABLE_SCHEMES


# ----------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------


def test_input_target_serialises() -> None:
    import json

    payload = resolve_input(URL).to_dict()
    assert payload["input"] == URL
    assert payload["input_type"] == "direct_url"
    assert payload["url"] == URL
    assert payload["accession"] is None
    json.dumps(payload)


def test_accession_serialisation_records_the_type() -> None:
    payload = resolve_input("GCST90012345").to_dict()
    assert payload["input_type"] == "gwas_catalog_accession"
    assert payload["accession"] == "GCST90012345"


def test_input_type_labels_are_human_readable() -> None:
    assert InputType.DIRECT_URL.label == "direct URL"
    assert InputType.GWAS_CATALOG_ACCESSION.label == "GWAS Catalog accession"
    assert InputType.LOCAL_FILE.label == "local file"


# ----------------------------------------------------------------------
# The invariant that matters: same path after resolution
# ----------------------------------------------------------------------


@pytest.fixture
def gz_body(fixtures_dir) -> bytes:
    return (fixtures_dir / "ssf_like.tsv.gz").read_bytes()


@responses.activate
def test_direct_url_assessment_is_bounded(config, gz_body) -> None:
    """A direct URL must not trigger a full download."""
    from gwaspoker.catalog.discovery import DiscoveryService

    responses.add(
        responses.HEAD,
        URL,
        headers={"Content-Length": str(len(gz_body)), "Accept-Ranges": "bytes"},
        status=200,
    )
    responses.add(
        responses.GET,
        URL,
        body=gz_body[:16384],
        status=206,
        headers={"Content-Range": f"bytes 0-16383/{len(gz_body)}"},
    )

    with DiscoveryService(config) as service:
        result = service.assess(URL, probe_bytes=16384)

    assert result.probe is not None
    assert result.probe.transfer.received_bytes <= 16384
    assert result.probe.transfer.received_bytes < len(gz_body)
    assert result.input_target.input_type is InputType.DIRECT_URL


@responses.activate
def test_direct_url_runs_the_full_analysis(config, gz_body) -> None:
    """Header detection, mapping, value validation and readiness all still run."""
    responses.add(
        responses.HEAD,
        URL,
        headers={"Content-Length": str(len(gz_body)), "Accept-Ranges": "bytes"},
        status=200,
    )
    responses.add(
        responses.GET,
        URL,
        body=gz_body[:16384],
        status=206,
        headers={"Content-Range": f"bytes 0-16383/{len(gz_body)}"},
    )

    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        result = service.assess(URL, probe_bytes=16384)

    assert result.probe.header is not None
    assert result.probe.mapping is not None
    assert result.probe.value_validation is not None
    assert result.readiness is not None
    assert result.readiness.evidence_source == "file_probe"
    # The whole point: a direct URL is not a degraded mode.
    assert result.readiness.verdict.value in ("READY", "PARTIAL", "NOT_READY")


@responses.activate
def test_direct_url_result_records_its_input_type(config, gz_body) -> None:
    responses.add(responses.HEAD, URL, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, body=gz_body[:16384], status=206)

    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        payload = service.assess(URL, probe_bytes=16384).to_dict()

    assert payload["input_type"] == "direct_url"
    assert payload["input"]["url"] == URL
    assert payload["input"]["accession"] is None


@responses.activate
def test_direct_url_says_why_the_structured_route_does_not_apply(config, gz_body) -> None:
    responses.add(responses.HEAD, URL, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, body=gz_body[:16384], status=206)

    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        result = service.assess(URL, probe_bytes=16384)

    notes = " ".join(result.notes)
    assert "no GWAS Catalog metadata" in notes
    assert result.api_assessment is None


def test_bad_input_to_assess_is_reported_not_raised(config) -> None:
    from gwaspoker.catalog.discovery import DiscoveryService

    with DiscoveryService(config) as service:
        result = service.assess("definitely not a thing")

    assert result.error
    assert result.failure_category is not None
    assert result.readiness is None


# ----------------------------------------------------------------------
# One resolver, not four
# ----------------------------------------------------------------------


def test_no_command_reimplements_url_detection() -> None:
    """URL classification must live in exactly one module.

    It was previously duplicated across `discovery.py` and two `cli.py`
    branches with three different rules, which is how ``ftp://`` came to be
    accepted by two commands and rejected by two others.
    """
    import gwaspoker

    root = Path(gwaspoker.__file__).parent
    offenders = []
    for source in sorted(root.rglob("*.py")):
        if source.name == "inputs.py":
            continue
        text = source.read_text(encoding="utf-8")
        for marker in ('startswith(("http://', 'startswith(("http:', '.startswith("http'):
            if marker in text:
                offenders.append(f"{source.relative_to(root)}: {marker}")
    assert not offenders, "URL detection reimplemented outside inputs.py: " + "; ".join(offenders)
