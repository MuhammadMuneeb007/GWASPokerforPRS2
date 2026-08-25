"""File resolution, downloading, extraction and failure classification.

The file-selection tests encode the counterexamples that defeat v1's
"largest file wins" rule.
"""

from __future__ import annotations

import hashlib

import pytest
import responses

from gwaspoker.catalog.models import FileCandidate
from gwaspoker.download.downloader import SummaryStatisticsDownloader, compute_md5
from gwaspoker.download.resolver import (
    SummaryStatisticsResolver,
    accession_block,
    classify_file,
    parse_size_label,
    score_candidate,
)
from gwaspoker.failures import (
    FAILURES,
    FailureCategory,
    FileResolutionError,
    GWASPokerError,
    classify_exception,
    http_status_category,
)
from gwaspoker.http import HttpClient

BASE = "https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics"
DIR = f"{BASE}/X/GCST90000001/"
HARMONISED_DIR = f"{DIR}harmonised/"


# ----------------------------------------------------------------------
# FTP path conventions
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("accession", "expected"),
    [
        ("GCST90038646", "GCST90038001-GCST90039000"),
        ("GCST006867", "GCST006001-GCST007000"),
        ("GCST012023", "GCST012001-GCST013000"),
        ("GCST000001", "GCST000001-GCST001000"),
        ("GCST001000", "GCST000001-GCST001000"),
        ("GCST001001", "GCST001001-GCST002000"),
    ],
)
def test_accession_block(accession, expected) -> None:
    assert accession_block(accession) == expected


def test_accession_block_rejects_non_numeric() -> None:
    with pytest.raises(FileResolutionError):
        accession_block("GCSTABC")


@pytest.mark.parametrize(
    ("label", "expected"),
    [("1.2G", 1288490188), ("218M", 228589568), ("650", 650), ("-", None), ("", None)],
)
def test_parse_size_label(label, expected) -> None:
    assert parse_size_label(label) == expected


def test_parse_size_label_distinguishes_unknown_from_zero() -> None:
    """v1's convert_to_numeric returned 0 for '-', so every file tied at zero."""
    assert parse_size_label("-") is None
    assert parse_size_label("0") == 0


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("GCST90038646_buildGRCh37.tsv", "data"),
        ("33959723-GCST90038646-EFO_0003821.h.tsv.gz", "data"),
        ("GCST90038646_buildGRCh37.tsv-meta.yaml", "metadata"),
        ("md5sum.txt", "checksum"),
        ("README.txt", "readme"),
        ("Supplementary_manuscript.pdf", "auxiliary"),
        ("harmonised/", "directory"),
    ],
)
def test_classify_file(name, kind) -> None:
    assert classify_file(name) == kind


# ----------------------------------------------------------------------
# Candidate scoring
# ----------------------------------------------------------------------


def _candidate(name, size, harmonised=False):
    return FileCandidate(
        name=name,
        url=f"https://x/{name}",
        size_bytes=size,
        is_harmonised=harmonised,
        kind=classify_file(name),
    )


def test_non_data_files_are_excluded_outright() -> None:
    scored = score_candidate(_candidate("paper.pdf", 3_600_000_000), prefer_harmonised=True)
    assert scored.score < 0


def test_harmonised_h_file_outranks_f_file() -> None:
    """.h.tsv.gz is fully harmonised; .f.tsv.gz is format-harmonised only."""
    h = score_candidate(
        _candidate("1-GCST1-EFO_1.h.tsv.gz", 378_000_000, harmonised=True), prefer_harmonised=True
    )
    f = score_candidate(
        _candidate("1-GCST1-EFO_1-Build37.f.tsv.gz", 218_000_000, harmonised=True),
        prefer_harmonised=True,
    )
    assert h.score > f.score


def test_size_cannot_overturn_a_naming_decision() -> None:
    """A 1.2 GB raw TSV must not beat a 378 MB harmonised gzip when asked for one."""
    raw = score_candidate(
        _candidate("GCST1_buildGRCh37.tsv", 1_288_490_188), prefer_harmonised=True
    )
    harmonised = score_candidate(
        _candidate("1-GCST1-EFO_1.h.tsv.gz", 396_130_130, harmonised=True), prefer_harmonised=True
    )
    assert harmonised.score > raw.score


def test_raw_is_preferred_when_harmonised_is_not_wanted() -> None:
    raw = score_candidate(
        _candidate("GCST1_buildGRCh37.tsv", 1_288_490_188), prefer_harmonised=False
    )
    harmonised = score_candidate(
        _candidate("1-GCST1-EFO_1.h.tsv.gz", 396_130_130, harmonised=True), prefer_harmonised=False
    )
    assert raw.score > harmonised.score


def test_zero_length_files_are_penalised() -> None:
    scored = score_candidate(_candidate("GCST1.tsv.gz", 0), prefer_harmonised=False)
    assert any("zero-length" in reason for reason in scored.reasons)


def test_every_candidate_records_its_reasons() -> None:
    scored = score_candidate(_candidate("GCST1.tsv.gz", 1000), prefer_harmonised=False)
    assert scored.reasons


# ----------------------------------------------------------------------
# Directory listing and resolution
# ----------------------------------------------------------------------


@pytest.fixture
def resolver(config):
    return SummaryStatisticsResolver(config, HttpClient(config))


@responses.activate
def test_list_directory_parses_the_apache_index(resolver, fixture_text) -> None:
    responses.add(responses.GET, DIR, body=fixture_text("ftp_index.html"), status=200)
    candidates = resolver.list_directory(DIR)
    names = [c.name for c in candidates]

    assert "GCST90000001_buildGRCh37.tsv" in names
    assert "harmonised/" in names
    # Sort links and the parent link are index furniture, not files.
    assert "Parent Directory" not in names
    assert not any(c.name in ("Name", "Size", "Last modified") for c in candidates)

    data = next(c for c in candidates if c.name == "GCST90000001_buildGRCh37.tsv")
    assert data.size_bytes == parse_size_label("1.2G")


@responses.activate
def test_resolve_prefers_harmonised_by_default(resolver, fixture_text) -> None:
    responses.add(responses.GET, DIR, body=fixture_text("ftp_index.html"), status=200)
    responses.add(
        responses.GET, HARMONISED_DIR, body=fixture_text("ftp_index_harmonised.html"), status=200
    )
    resolved = resolver.resolve(directory_url=DIR, harmonised="auto")

    assert resolved.name == "12345678-GCST90000001-EFO_0000001.h.tsv.gz"
    assert resolved.is_harmonised
    assert resolved.selection_reason


@responses.activate
def test_resolve_never_selects_the_bundled_pdf(resolver, fixture_text) -> None:
    """The 3.4 GB PDF is the largest entry; v1 would have downloaded it."""
    responses.add(responses.GET, DIR, body=fixture_text("ftp_index.html"), status=200)
    resolved = resolver.resolve(directory_url=DIR, harmonised="no")
    assert resolved.name == "GCST90000001_buildGRCh37.tsv"
    assert not resolved.name.endswith(".pdf")


@responses.activate
def test_resolve_harmonised_no_returns_the_raw_file(resolver, fixture_text) -> None:
    responses.add(responses.GET, DIR, body=fixture_text("ftp_index.html"), status=200)
    resolved = resolver.resolve(directory_url=DIR, harmonised="no")
    assert not resolved.is_harmonised
    assert resolved.name == "GCST90000001_buildGRCh37.tsv"


@responses.activate
def test_resolve_finds_the_metadata_and_checksum_sidecars(resolver, fixture_text) -> None:
    responses.add(responses.GET, DIR, body=fixture_text("ftp_index.html"), status=200)
    resolved = resolver.resolve(directory_url=DIR, harmonised="no")
    assert resolved.metadata_url.endswith("GCST90000001_buildGRCh37.tsv-meta.yaml")
    assert resolved.checksum_url.endswith("md5sum.txt")


@responses.activate
def test_resolve_records_all_candidates(resolver, fixture_text) -> None:
    responses.add(responses.GET, DIR, body=fixture_text("ftp_index.html"), status=200)
    resolved = resolver.resolve(directory_url=DIR, harmonised="no")
    assert len(resolved.candidates) >= 1
    assert all(c.reasons for c in resolved.candidates)


@responses.activate
def test_resolve_raises_when_harmonised_is_demanded_but_absent(resolver, fixture_text) -> None:
    index = fixture_text("ftp_index.html").replace('<a href="harmonised/">harmonised/</a>', "")
    responses.add(responses.GET, DIR, body=index, status=200)
    with pytest.raises(FileResolutionError) as excinfo:
        resolver.resolve(directory_url=DIR, harmonised="yes")
    assert "no harmonised" in str(excinfo.value)


@responses.activate
def test_resolve_404_is_reported_clearly(resolver) -> None:
    responses.add(responses.GET, DIR, status=404)
    with pytest.raises(FileResolutionError) as excinfo:
        resolver.resolve(directory_url=DIR)
    assert "404" in str(excinfo.value)


@responses.activate
def test_fetch_expected_md5(resolver) -> None:
    url = f"{DIR}md5sum.txt"
    responses.add(
        responses.GET,
        url,
        body=(
            "499e75d7ba83a354d42101624daf6342 GCST90000001_buildGRCh37.tsv-meta.yaml\n"
            "66b4c5f7091208cd518dd6ca2399c561 GCST90000001_buildGRCh37.tsv\n"
        ),
        status=200,
    )
    assert resolver.fetch_expected_md5(url, "GCST90000001_buildGRCh37.tsv") == (
        "66b4c5f7091208cd518dd6ca2399c561"
    )
    assert resolver.fetch_expected_md5(url, "not-listed.tsv") is None


def test_directory_url_from_convention(resolver) -> None:
    url = resolver.directory_url_for("GCST90038646")
    assert url.endswith("GCST90038001-GCST90039000/GCST90038646/")


def test_directory_url_normalises_the_api_hint(resolver) -> None:
    """v2 returns an http:// FTP path with no trailing slash."""
    hint = "http://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/X/GCST90000001"
    url = resolver.directory_url_for("GCST90000001", hint=hint)
    assert url.startswith("https://")
    assert url.endswith("/")


# ----------------------------------------------------------------------
# Downloading
# ----------------------------------------------------------------------


@pytest.fixture
def downloader(config):
    return SummaryStatisticsDownloader(config, HttpClient(config))


FILE_URL = f"{DIR}GCST90000001.tsv"
BODY = b"chromosome\tbase_pair_location\n1\t12345\n" * 100
BODY_MD5 = hashlib.md5(BODY).hexdigest()  # noqa: S324


@responses.activate
def test_download_writes_and_verifies(downloader, tmp_path) -> None:
    responses.add(responses.GET, FILE_URL, body=BODY, status=200)
    result = downloader.download(FILE_URL, tmp_path, expected_md5=BODY_MD5)

    assert result.succeeded
    assert result.checksum_verified is True
    assert result.path.name == "GCST90000001.tsv"
    assert result.path.read_bytes() == BODY
    # The .part file must be gone once the transfer is verified.
    assert not (tmp_path / "GCST90000001.tsv.part").exists()


@responses.activate
def test_checksum_mismatch_keeps_the_bytes_as_part(downloader, tmp_path) -> None:
    """A corrupt file must never be presented under its real name."""
    responses.add(responses.GET, FILE_URL, body=BODY, status=200)
    result = downloader.download(FILE_URL, tmp_path, expected_md5="0" * 32)

    assert not result.succeeded
    assert result.checksum_verified is False
    assert "Checksum mismatch" in result.error
    assert not (tmp_path / "GCST90000001.tsv").exists()
    assert (tmp_path / "GCST90000001.tsv.part").exists()


@responses.activate
def test_existing_file_is_not_overwritten(downloader, tmp_path) -> None:
    target = tmp_path / "GCST90000001.tsv"
    target.write_bytes(b"existing")
    result = downloader.download(FILE_URL, tmp_path)

    assert result.skipped
    assert target.read_bytes() == b"existing"
    assert "--overwrite" in result.notes[0]


@responses.activate
def test_overwrite_replaces_the_file(downloader, tmp_path) -> None:
    responses.add(responses.GET, FILE_URL, body=BODY, status=200)
    (tmp_path / "GCST90000001.tsv").write_bytes(b"old")
    result = downloader.download(FILE_URL, tmp_path, overwrite=True)
    assert result.succeeded
    assert result.path.read_bytes() == BODY


@responses.activate
def test_resume_continues_from_a_part_file(downloader, tmp_path) -> None:
    part = tmp_path / "GCST90000001.tsv.part"
    part.write_bytes(BODY[:100])
    responses.add(
        responses.GET,
        FILE_URL,
        body=BODY[100:],
        status=206,
        headers={"Content-Range": f"bytes 100-{len(BODY) - 1}/{len(BODY)}"},
    )
    result = downloader.download(FILE_URL, tmp_path, expected_md5=BODY_MD5)

    assert result.resumed_from == 100
    assert result.succeeded
    assert result.path.read_bytes() == BODY


@responses.activate
def test_resume_restarts_when_the_server_ignores_the_range(downloader, tmp_path) -> None:
    part = tmp_path / "GCST90000001.tsv.part"
    part.write_bytes(BODY[:100])
    responses.add(responses.GET, FILE_URL, body=BODY, status=200)
    result = downloader.download(FILE_URL, tmp_path, expected_md5=BODY_MD5)

    assert result.resumed_from == 0
    assert result.succeeded
    assert any("resume unsupported" in note for note in result.notes)


@responses.activate
def test_download_reports_progress(downloader, tmp_path) -> None:
    responses.add(
        responses.GET,
        FILE_URL,
        body=BODY,
        status=200,
        headers={"Content-Length": str(len(BODY))},
    )
    seen = []
    downloader.download(FILE_URL, tmp_path, progress=lambda w, t: seen.append((w, t)))
    assert seen
    assert seen[-1][0] == len(BODY)


@responses.activate
def test_download_http_error_is_categorised(downloader, tmp_path) -> None:
    responses.add(responses.GET, FILE_URL, status=403)
    result = downloader.download(FILE_URL, tmp_path)
    assert not result.succeeded
    assert "403" in result.error


@responses.activate
def test_sidecars_are_fetched(downloader, tmp_path, fixture_text) -> None:
    from gwaspoker.catalog.models import ResolvedFile

    meta_url = f"{DIR}GCST90000001.tsv-meta.yaml"
    md5_url = f"{DIR}md5sum.txt"
    responses.add(responses.GET, meta_url, body=fixture_text("ssf_meta.yaml"), status=200)
    responses.add(responses.GET, md5_url, body="abc GCST90000001.tsv\n", status=200)

    resolved = ResolvedFile(
        url=FILE_URL, name="GCST90000001.tsv", metadata_url=meta_url, checksum_url=md5_url
    )
    paths = downloader.download_sidecars(resolved, tmp_path)
    assert {p.name for p in paths} == {"GCST90000001.tsv-meta.yaml", "md5sum.txt"}


def test_compute_md5(tmp_path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(BODY)
    assert compute_md5(path) == BODY_MD5


def test_windows_reserved_filenames_are_escaped() -> None:
    from gwaspoker.download.downloader import _sanitise_filename

    assert _sanitise_filename("CON.tsv").startswith("_")
    assert ":" not in _sanitise_filename("chr:pos.tsv")
    assert _sanitise_filename("normal.tsv") == "normal.tsv"


# ----------------------------------------------------------------------
# Failure classification
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (403, FailureCategory.HTTP_403),
        (404, FailureCategory.HTTP_404),
        (410, FailureCategory.API_DEPRECATED),
        (416, FailureCategory.RANGE_NOT_SUPPORTED),
        (500, FailureCategory.API_ERROR),
        (503, FailureCategory.API_ERROR),
        (418, FailureCategory.HTTP_ERROR),
    ],
)
def test_http_status_category(status, category) -> None:
    assert http_status_category(status) is category


def test_410_is_distinguished_from_404() -> None:
    """410 Gone is a permanent withdrawal; 404 is 'not here'."""
    assert http_status_category(410) is not http_status_category(404)


def test_classify_exception() -> None:
    import requests

    assert classify_exception(requests.exceptions.Timeout()) is FailureCategory.NETWORK_TIMEOUT
    assert classify_exception(FileNotFoundError()) is FailureCategory.FILE_NOT_FOUND
    assert classify_exception(ImportError()) is FailureCategory.DEPENDENCY_MISSING
    assert classify_exception(ValueError()) is FailureCategory.UNKNOWN


def test_classify_exception_uses_the_category_on_gwaspoker_errors() -> None:
    error = GWASPokerError("boom", category=FailureCategory.HEADER_NOT_FOUND)
    assert classify_exception(error) is FailureCategory.HEADER_NOT_FOUND


def test_failure_log_records_and_serialises() -> None:
    FAILURES.record("probe", FailureCategory.HTTP_404, "not found", study="GCST1", url="https://x")
    assert len(FAILURES) == 1
    payload = FAILURES.to_list()[0]
    assert payload["failure_category"] == "http_404"
    assert payload["study"] == "GCST1"
    assert payload["timestamp"]


def test_failure_log_writes_jsonl(tmp_path) -> None:
    import json

    FAILURES.record("probe", FailureCategory.HTTP_404, "not found", study="GCST1")
    path = tmp_path / "failures.jsonl"
    FAILURES.write_jsonl(path)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[0])["failure_category"] == "http_404"


# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------


def test_extract_plain_tsv(fixtures_dir, tmp_path) -> None:
    from gwaspoker.processing.extract import Extractor

    result = Extractor().extract(
        fixtures_dir / "comment_preamble.tsv", output_path=tmp_path / "out.tsv"
    )
    assert result.succeeded
    assert result.rows_written == 3
    assert result.columns == ("CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P")
    assert result.header_row_index == 2


def test_extract_gzip(fixtures_dir, tmp_path) -> None:
    from gwaspoker.processing.extract import Extractor

    result = Extractor().extract(
        fixtures_dir / "ssf_like.tsv.gz", output_path=tmp_path / "out.tsv", max_rows=50
    )
    assert result.succeeded
    assert result.rows_written == 50
    assert result.columns[0] == "chromosome"


def test_extract_preserves_values_exactly(fixtures_dir, tmp_path) -> None:
    """v1 rewrote ':' to '_' and stripped quotes across the whole file."""
    from gwaspoker.processing.extract import Extractor

    result = Extractor().extract(
        fixtures_dir / "ssf_like.tsv.gz", output_path=tmp_path / "out.tsv", max_rows=5
    )
    text = result.output_path.read_text(encoding="utf-8")
    # chr:pos-style identifiers survive intact.
    assert "1_100000_A_G" in text
    # Scientific notation is not mangled.
    assert "e-" in text


def test_extract_reports_transformations(fixtures_dir, tmp_path) -> None:
    from gwaspoker.processing.extract import Extractor

    result = Extractor().extract(
        fixtures_dir / "quoted_comma.csv", output_path=tmp_path / "out.tsv"
    )
    assert result.succeeded
    payload = result.to_dict()
    assert "transformations" in payload["normalization"]
    # The unsafe rewrites v1 performed are explicitly declined and recorded.
    declined = " ".join(payload["normalization"]["declined"])
    assert "chr:pos" in declined


def test_extract_refuses_to_overwrite(fixtures_dir, tmp_path) -> None:
    from gwaspoker.processing.extract import Extractor

    out = tmp_path / "out.tsv"
    out.write_text("existing", encoding="utf-8")
    result = Extractor().extract(fixtures_dir / "comment_preamble.tsv", output_path=out)
    assert not result.succeeded
    assert "--overwrite" in result.error
    assert out.read_text(encoding="utf-8") == "existing"


def test_extract_missing_file(tmp_path) -> None:
    from gwaspoker.processing.extract import Extractor

    result = Extractor().extract(tmp_path / "nope.tsv")
    assert result.failure_category is FailureCategory.FILE_NOT_FOUND


def test_extract_renames_to_prs_symbols(fixtures_dir, tmp_path) -> None:
    from gwaspoker.processing.extract import Extractor

    result = Extractor().extract(
        fixtures_dir / "ssf_like.tsv.gz",
        output_path=tmp_path / "out.tsv",
        max_rows=5,
        rename_to_symbols=True,
    )
    assert "CHR" in result.columns
    assert "BP" in result.columns
    assert "A1" in result.columns


def test_archive_traversal_is_refused() -> None:
    from gwaspoker.failures import CompressionError
    from gwaspoker.processing.extract import _reject_unsafe_member

    for name in ("../escape.tsv", "/abs/path.tsv", "a/../../b.tsv"):
        with pytest.raises(CompressionError):
            _reject_unsafe_member(name)
    _reject_unsafe_member("results/sumstats.tsv")  # must not raise


def test_default_output_name_preserves_the_stem(fixtures_dir) -> None:
    """v1 wrote every file to a fixed gwas.csv, overwriting the previous run."""
    from gwaspoker.processing.extract import _default_output

    out = _default_output(fixtures_dir / "GCST90038646_buildGRCh37.tsv.gz")
    assert out.name == "GCST90038646_buildGRCh37.gwaspoker.tsv"
