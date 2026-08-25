"""Compression detection, partial decompression, encoding and bounded probing.

The network is mocked throughout with :mod:`responses`. Nothing here contacts
EBI; see ``test_integration.py`` for the live checks.
"""

from __future__ import annotations

import gzip

import pytest
import responses

from gwaspoker.failures import CompressionError, FailureCategory
from gwaspoker.http import HttpClient, parse_content_length, supports_ranges
from gwaspoker.probe.compression import (
    Compression,
    _pick_archive_member,
    decompress_prefix,
    detect_compression,
)
from gwaspoker.probe.encoding import detect_bom, detect_encoding, split_complete_lines
from gwaspoker.probe.remote import RemoteProber

URL = "https://ftp.example.org/data/study.tsv.gz"


# ----------------------------------------------------------------------
# Compression detection
# ----------------------------------------------------------------------


def test_detect_gzip_by_magic_bytes() -> None:
    data = gzip.compress(b"chromosome\tposition\n1\t123\n")
    assert detect_compression(data, "mislabelled.tsv") is Compression.GZIP


def test_magic_bytes_beat_the_extension() -> None:
    """Files served as .tsv that are in fact gzipped are common in the Catalog."""
    data = gzip.compress(b"a\tb\n1\t2\n")
    assert detect_compression(data, "study.tsv") is Compression.GZIP


def test_detect_plain_text() -> None:
    assert detect_compression(b"CHR\tPOS\n1\t123\n", "study.tsv") is Compression.NONE


def test_detect_zip(fixtures_dir) -> None:
    data = (fixtures_dir / "archive.zip").read_bytes()
    assert detect_compression(data, "archive.zip") is Compression.ZIP


def test_detect_tar_gzip(fixtures_dir) -> None:
    data = (fixtures_dir / "archive.tar.gz").read_bytes()
    assert detect_compression(data, "archive.tar.gz") is Compression.TAR_GZIP


def test_detect_by_extension_when_no_magic() -> None:
    assert detect_compression(b"not really compressed", "study.zst") is Compression.ZSTD


# ----------------------------------------------------------------------
# Partial decompression -- the core capability
# ----------------------------------------------------------------------


def test_gzip_prefix_yields_content_without_the_whole_file(fixtures_dir) -> None:
    """A truncated gzip stream must still give up its header.

    This replaces v1's ``gunzip -c ... | head -n 100``, which needs GNU
    coreutils and a shell.
    """
    full = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    prefix = full[:8192]
    assert len(prefix) < len(full)

    result = decompress_prefix(prefix, Compression.GZIP)
    assert result.data
    assert not result.complete  # truncated at the probe boundary, as intended
    assert result.data.startswith(b"chromosome\tbase_pair_location")
    assert result.expansion_ratio > 1


def test_gzip_complete_stream_is_marked_complete(fixtures_dir) -> None:
    full = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    result = decompress_prefix(full, Compression.GZIP)
    assert result.complete
    assert result.data.count(b"\n") > 100


def test_gzip_prefix_too_short_to_produce_output() -> None:
    """A prefix shorter than the first deflate block yields nothing, but says so.

    zlib does not treat "need more input" as an error, so this returns empty
    data marked incomplete rather than raising. The probe pipeline turns that
    into a TRUNCATED_PROBE failure with an actionable message -- see
    :func:`test_probe_reports_empty_body`.
    """
    data = gzip.compress(b"x" * 10_000)
    result = decompress_prefix(data[:5], Compression.GZIP)
    assert result.data == b""
    assert not result.complete
    assert "truncated" in (result.note or "")


def test_corrupt_gzip_raises() -> None:
    """Data that is not a gzip stream at all is an error, not a truncation."""
    gzip_magic = bytes([0x1F, 0x8B])
    with pytest.raises(CompressionError):
        decompress_prefix(gzip_magic + bytes([0xFF]) * 200, Compression.GZIP)


def test_truncated_gzip_probe_is_reported_to_the_user(config, fixtures_dir) -> None:
    """An unusably small probe must produce a clear, categorised message."""
    from gwaspoker.probe.remote import RemoteProber

    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(fixtures_dir / "ssf_like.tsv.gz", probe_bytes=4)
    assert not result.succeeded
    assert result.failure_category is FailureCategory.TRUNCATED_PROBE
    assert "probe-bytes" in (result.error or "")


def test_zip_prefix_without_central_directory(fixtures_dir) -> None:
    """A zip prefix has no central directory; the local header still decodes."""
    full = (fixtures_dir / "archive.zip").read_bytes()
    result = decompress_prefix(full[:20000], Compression.ZIP)
    assert result.data.startswith(b"chromosome\t")
    assert result.member_name


def test_zip_member_selection_ignores_a_larger_pdf(fixtures_dir) -> None:
    """v1 picked the largest member, which is the manuscript PDF here."""
    full = (fixtures_dir / "archive.zip").read_bytes()
    result = decompress_prefix(full, Compression.ZIP)
    assert result.member_name == "results/sumstats.tsv"
    assert b"%PDF" not in result.data[:100]


def test_tar_gzip_member(fixtures_dir) -> None:
    full = (fixtures_dir / "archive.tar.gz").read_bytes()
    result = decompress_prefix(full, Compression.TAR_GZIP)
    assert result.data.startswith(b"chromosome\t")


def test_pick_archive_member_prefers_data_extensions() -> None:
    names = ["README.txt", "paper.pdf", "results/sumstats.tsv", "__MACOSX/._x"]
    assert _pick_archive_member(names) == "results/sumstats.tsv"


def test_pick_archive_member_returns_none_when_only_noise() -> None:
    assert _pick_archive_member(["paper.pdf", "README.txt", "dir/"]) is None


def test_uncompressed_passthrough() -> None:
    payload = b"CHR\tPOS\n1\t123\n"
    result = decompress_prefix(payload, Compression.NONE)
    assert result.data == payload
    assert result.complete


# ----------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------


def test_detect_bom() -> None:
    assert detect_bom(b"\xef\xbb\xbfabc") == "utf-8-sig"
    assert detect_bom(b"\xff\xfea\x00") == "utf-16-le"
    assert detect_bom(b"abc") is None


def test_ascii_is_reported_as_utf8() -> None:
    result = detect_encoding(b"CHR\tPOS\n1\t123\n")
    assert result.encoding == "utf-8"
    assert result.confidence == 1.0
    assert result.method == "ascii"


def test_utf8_bom_decoding(fixtures_dir) -> None:
    result = detect_encoding((fixtures_dir / "utf8_bom.tsv").read_bytes())
    assert result.had_bom
    assert result.text.startswith("chromosome")


def test_latin1_decoding(fixtures_dir) -> None:
    """The bytes are invalid UTF-8; detection must not raise."""
    data = (fixtures_dir / "latin1_preamble.tsv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")
    result = detect_encoding(data)
    assert "CHR" in result.text
    assert result.confidence > 0


def test_truncated_multibyte_tail_is_tolerated() -> None:
    """A bounded probe usually ends mid-character; that is not corruption."""
    text = "chromosome\tposition\nBjörn\t1\n"
    data = text.encode("utf-8")
    truncated = data[:-1] + "ö".encode()[:1]  # split a two-byte character
    result = detect_encoding(truncated)
    assert "chromosome" in result.text


def test_empty_input() -> None:
    result = detect_encoding(b"")
    assert result.text == ""
    assert result.method == "empty"


def test_split_complete_lines_separates_the_partial_tail() -> None:
    complete, partial = split_complete_lines("a\nb\nc")
    assert complete == ["a", "b"]
    assert partial == "c"

    complete, partial = split_complete_lines("a\nb\n")
    assert complete == ["a", "b"]
    assert partial == ""


def test_split_complete_lines_normalises_crlf() -> None:
    complete, _ = split_complete_lines("a\r\nb\r\n")
    assert complete == ["a", "b"]


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------


def test_parse_content_length_from_content_range() -> None:
    assert parse_content_length({"Content-Range": "bytes 0-65535/396130130"}) == 396130130


def test_parse_content_length_from_content_length() -> None:
    assert parse_content_length({"Content-Length": "1234"}) == 1234
    assert parse_content_length({}) is None


def test_supports_ranges() -> None:
    assert supports_ranges({"Accept-Ranges": "bytes"})
    assert not supports_ranges({"Accept-Ranges": "none"})
    assert not supports_ranges({})


# ----------------------------------------------------------------------
# Bounded remote probing
# ----------------------------------------------------------------------


@pytest.fixture
def gz_body(fixtures_dir) -> bytes:
    return (fixtures_dir / "ssf_like.tsv.gz").read_bytes()


@responses.activate
def test_probe_uses_range_when_supported(config, gz_body) -> None:
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

    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_url(URL, probe_bytes=16384)

    assert result.succeeded
    assert result.transfer.range_used
    assert result.transfer.received_bytes == 16384
    assert result.transfer.remote_file_size == len(gz_body)
    assert result.header.raw_header[0] == "chromosome"


@responses.activate
def test_probe_bounds_the_stream_when_range_is_unsupported(config, gz_body) -> None:
    """The bound is on bytes, not on time. v1 used `timeout -s KILL 10 wget`."""
    responses.add(
        responses.HEAD,
        URL,
        headers={"Content-Length": str(len(gz_body))},
        status=200,
    )
    responses.add(responses.GET, URL, body=gz_body, status=200)

    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_url(URL, probe_bytes=8192)

    assert result.succeeded
    assert result.transfer.range_supported is False
    assert result.transfer.received_bytes <= 8192
    assert result.header.raw_header[0] == "chromosome"


@responses.activate
def test_probe_records_transfer_reduction(config, gz_body) -> None:
    responses.add(
        responses.HEAD,
        URL,
        headers={"Content-Length": "396130130", "Accept-Ranges": "bytes"},
        status=200,
    )
    responses.add(
        responses.GET,
        URL,
        body=gz_body[:16384],
        status=206,
        headers={"Content-Range": "bytes 0-16383/396130130"},
    )
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_url(URL, probe_bytes=16384)
    assert result.transfer.transfer_reduction > 0.999


@responses.activate
def test_probe_reports_http_404_as_a_category(config) -> None:
    responses.add(responses.HEAD, URL, status=404)
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_url(URL)
    assert not result.succeeded
    assert result.failure_category is FailureCategory.HTTP_404


@responses.activate
def test_probe_reports_http_403_as_a_category(config) -> None:
    responses.add(responses.HEAD, URL, status=403)
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_url(URL)
    assert result.failure_category is FailureCategory.HTTP_403


@responses.activate
def test_probe_reports_empty_body(config) -> None:
    responses.add(responses.HEAD, URL, status=200, headers={"Accept-Ranges": "bytes"})
    responses.add(responses.GET, URL, body=b"", status=206)
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_url(URL)
    assert not result.succeeded
    assert result.failure_category is FailureCategory.TRUNCATED_PROBE


# ----------------------------------------------------------------------
# Local probing
# ----------------------------------------------------------------------


def test_probe_local_plain(config, fixtures_dir) -> None:
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(fixtures_dir / "comment_preamble.tsv")
    assert result.succeeded
    assert result.source_kind == "local"
    assert result.compression is Compression.NONE
    assert result.header.header_row_index == 2
    assert result.transfer.received_bytes > 0


def test_probe_local_gzip(config, fixtures_dir) -> None:
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(fixtures_dir / "ssf_like.tsv.gz", probe_bytes=8192)
    assert result.succeeded
    assert result.compression is Compression.GZIP
    assert result.header.raw_header[0] == "chromosome"
    # Only the requested prefix was read, not the whole file.
    assert result.transfer.received_bytes == 8192
    assert result.transfer.remote_file_size > 8192


def test_probe_local_missing_file(config, tmp_path) -> None:
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(tmp_path / "nope.tsv")
    assert not result.succeeded
    assert result.failure_category is FailureCategory.FILE_NOT_FOUND


def test_probe_local_archive(config, fixtures_dir) -> None:
    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(fixtures_dir / "archive.zip")
    assert result.succeeded
    assert result.compression is Compression.ZIP
    assert result.header.raw_header[0] == "chromosome"


def test_format_label(config, fixtures_dir) -> None:
    prober = RemoteProber(config, HttpClient(config))
    assert prober.probe_local(fixtures_dir / "ssf_like.tsv.gz").format_label == "TSV.GZIP"
    assert prober.probe_local(fixtures_dir / "quoted_comma.csv").format_label == "CSV"


def test_probe_result_serialises(config, fixtures_dir) -> None:
    prober = RemoteProber(config, HttpClient(config))
    payload = prober.probe_local(fixtures_dir / "comment_preamble.tsv").to_dict()
    assert payload["succeeded"]
    assert payload["header"]["field_count"] == 8
    assert payload["transfer"]["received_bytes"] > 0
    assert payload["mapping"]["resolved_count"] == 8


def test_no_module_shells_out() -> None:
    """No module in the package may import subprocess or call os.system.

    v1 depended on wget, timeout, 7z, tar, gunzip, zcat, cat and bash, all
    invoked through ``os.system`` or ``subprocess.run(..., shell=True)``. None
    of them exist on a stock Windows install. This walks the AST of every
    module so a comment mentioning ``subprocess`` does not trip it, and an
    actual import cannot slip back in.
    """
    import ast
    from pathlib import Path

    import gwaspoker

    package_root = Path(gwaspoker.__file__).parent
    offenders = []

    for source_file in sorted(package_root.rglob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"subprocess", "commands", "pty"}:
                        offenders.append(f"{source_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in {"subprocess", "commands", "pty"}:
                    offenders.append(f"{source_file.name}:{node.lineno} imports from {node.module}")
            elif isinstance(node, ast.Attribute) and node.attr in {"system", "popen", "spawnl"}:
                value = node.value
                if isinstance(value, ast.Name) and value.id == "os":
                    offenders.append(f"{source_file.name}:{node.lineno} calls os.{node.attr}")

    assert not offenders, "Shell dependencies found: " + "; ".join(offenders)
