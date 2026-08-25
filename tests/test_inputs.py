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


def test_ftp_urls_are_rewritten_for_verified_mirrors_only() -> None:
    """requests has no FTP adapter, so ftp:// must be rewritten or refused.

    Before the shared resolver, ``assess`` and ``probe`` accepted ``ftp://`` and
    then died inside requests with ``InvalidSchema``, while ``download`` and
    ``scan`` rejected it outright.

    The rewrite is now host-aware. ``ftp://host/path`` does not universally
    imply ``https://host/path``: many FTP servers have no HTTP front end, and
    guessing one would send the probe to a URL the user never supplied, turning
    "unsupported scheme" into a misleading 404.
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


def test_ftp_on_an_unverified_host_is_refused_not_guessed() -> None:
    """Silently rewriting an arbitrary FTP host would fetch a URL nobody asked for."""
    with pytest.raises(InputResolutionError) as excinfo:
        resolve_input("ftp://some-random-lab.example.org/gwas.gz")
    message = str(excinfo.value)
    assert "no FTP adapter" in message
    assert "FTP_HTTPS_MIRRORS" in message  # tells the user how to proceed


def test_ftp_mirror_subdomains_are_recognised() -> None:
    target = resolve_input("ftp://ftp.ebi.ac.uk/pub/x.gz")
    assert target.url.startswith("https://ftp.ebi.ac.uk/")


def test_every_resolved_url_uses_a_fetchable_scheme() -> None:
    from urllib.parse import urlparse

    for value in (URL, "http://example.org/x.gz", "ftp://ftp.ebi.ac.uk/pub/x.gz"):
        resolved = resolve_input(value)
        assert urlparse(resolved.url).scheme in FETCHABLE_SCHEMES


def test_normalisation_is_recorded_as_a_named_rule() -> None:
    """A supplementary table must be able to say which URLs were altered, and why."""
    target = resolve_input("ftp://ftp.ebi.ac.uk/pub/x.gz")
    payload = target.to_dict()
    assert payload["normalisation_rule"] == "ftp_https_mirror"
    assert payload["original_url"].startswith("ftp://")
    assert payload["url"].startswith("https://")


def test_unrewritten_urls_carry_no_rule() -> None:
    payload = resolve_input(URL).to_dict()
    assert payload["normalisation_rule"] is None
    assert payload["original_url"] == payload["url"]


# ----------------------------------------------------------------------
# Share links
# ----------------------------------------------------------------------


def test_dropbox_share_links_become_direct_downloads() -> None:
    """A Dropbox link ending .zip returns text/html unless dl=1 is set.

    That is why such URLs were previously reported as ZIP decompression errors:
    the bytes really were not a ZIP, because they were a preview page.
    """
    target = resolve_input("https://www.dropbox.com/s/abc123/gwas.zip?dl=0")
    assert "dl=1" in target.url
    assert target.normalisation_rule == "dropbox_direct_download"
    assert target.normalisation_note


def test_dropbox_links_without_a_dl_parameter_get_one() -> None:
    target = resolve_input("https://www.dropbox.com/s/abc123/gwas.zip")
    assert target.url.endswith("dl=1")


def test_already_direct_dropbox_links_are_untouched() -> None:
    url = "https://www.dropbox.com/s/abc123/gwas.zip?dl=1"
    target = resolve_input(url)
    assert target.url == url
    assert target.normalisation_rule is None


def test_unknown_hosts_are_not_rewritten() -> None:
    from gwaspoker.url_resolvers import resolve_public_share_url

    resolved = resolve_public_share_url("https://example.org/gwas.zip?dl=0")
    assert not resolved.was_rewritten
    assert resolved.rule is None


def test_only_dropbox_is_implemented() -> None:
    """Providers we have no failing examples for are deliberately absent.

    Adding speculative rewrite rules for OneDrive/SharePoint/Drive would be
    untestable against real behaviour and would rot as their APIs change.
    """
    from gwaspoker.url_resolvers import resolve_public_share_url

    for host in (
        "https://onedrive.live.com/download?id=1",
        "https://drive.google.com/file/d/abc/view",
        "https://example.sharepoint.com/x/gwas.zip",
    ):
        assert not resolve_public_share_url(host).was_rewritten


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
