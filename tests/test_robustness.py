"""Robustness fixes prompted by an external run over 768 heterogeneous URLs.

That run produced a failure taxonomy which, on inspection, was largely
mis-attributed:

===========================  ====================================
Reported                     Actually
===========================  ====================================
111 "gzip decompression"     mostly share/landing pages on .gz URLs
29 "ZIP decompression"       mostly the same
36 valid ustar archives      real data, unreachable past metadata members
some "successful headers"    ``<meta | name= | content= | />``
===========================  ====================================

Each test below pins one of those diagnoses. They are written against fixtures,
not against the benchmark, so they stay meaningful after the cohort is rerun.
"""

from __future__ import annotations

import contextlib
import gzip

import pytest
import responses

from gwaspoker.failures import FailureCategory
from gwaspoker.http import HttpClient
from gwaspoker.mapping.mapper import UNKNOWN_CONCEPT, get_mapper
from gwaspoker.probe.compression import (
    Compression,
    decompress_prefix,
    detect_compression,
    detect_compression_by_magic,
)
from gwaspoker.probe.payload import (
    PayloadKind,
    classify_payload_prefix,
    looks_like_markup,
)
from gwaspoker.probe.remote import RemoteProber

URL = "https://example.org/study.tsv.gz"


# ======================================================================
# Item 1: suffix heuristics keep their underscore boundary
# ======================================================================


@pytest.mark.parametrize(
    "column",
    [
        "background_color",
        "span{background-color:",
        "FreqSE",
        "MinFreq",
        "MaxFreq",
        "Overall",
        "Direction_or_something",
    ],
)
def test_suffix_heuristics_do_not_match_mid_word(column) -> None:
    """`_or` must not match `background_color`, `_se` must not match `FreqSE`.

    Normalising the suffix pattern used to strip its leading underscore, turning
    `_or` into `or` -- which matches the tail of any word ending in those two
    letters. That is how a CSS fragment in an HTML response became an odds
    ratio.
    """
    assert get_mapper().map_column(column).canonical_name == UNKNOWN_CONCEPT


@pytest.mark.parametrize(
    ("column", "concept"),
    [
        ("trait_OR", "odds_ratio"),
        ("trait_SE", "standard_error"),
        ("height_beta", "beta"),
        ("ala_pval", "p_value"),
    ],
)
def test_suffix_heuristics_still_match_at_a_boundary(column, concept) -> None:
    """The fix must not disable the heuristic it protects."""
    mapped = get_mapper().map_column(column)
    assert mapped.canonical_name == concept
    assert mapped.mapping_method == "heuristic"


# ======================================================================
# Item 14/15: vetted aliases in, speculative ones out
# ======================================================================


@pytest.mark.parametrize(
    ("column", "concept"),
    [
        ("P.2gc", "p_value"),
        ("SE.2gc", "standard_error"),
        ("n_total_sum", "sample_size"),
        ("FreqAllele1HapMapCEU", "effect_allele_frequency"),
        ("eaf_hapmapceu", "effect_allele_frequency"),
        ("mach_r2", "info_score"),
    ],
)
def test_vetted_external_aliases_map(column, concept) -> None:
    assert get_mapper().map_column(column).canonical_name == concept


@pytest.mark.parametrize(
    "column",
    ["FreqSE", "MinFreq", "MaxFreq", "Overall", "SNP_hg18", "SNP_hg19"],
)
def test_ambiguous_columns_stay_unknown(column) -> None:
    """Frequent in external files, but their meaning is not established."""
    assert get_mapper().map_column(column).canonical_name == UNKNOWN_CONCEPT


@pytest.mark.parametrize("column", ["P_BMD", "P_LM", "P_bivariate", "beta_BMD", "beta_LM"])
def test_phenotype_specific_columns_stay_unknown(column) -> None:
    """A blanket `P_*` rule would pick arbitrarily among several analyses.

    Files carrying `P_BMD` and `P_LM` contain more than one analysis. Mapping
    both to `p_value` and letting the evaluator take the highest-confidence one
    would silently select a phenotype. Until there is an explicit policy for
    choosing the primary analysis, these stay unknown.
    """
    assert get_mapper().map_column(column).canonical_name == UNKNOWN_CONCEPT


# ======================================================================
# Item 2: markup detection
# ======================================================================


def test_html_is_detected(fixtures_dir) -> None:
    data = (fixtures_dir / "landing_page.html").read_bytes()
    assert looks_like_markup(data) == "<!doctype html"


def test_xml_is_detected(fixtures_dir) -> None:
    data = (fixtures_dir / "error_document.xml").read_bytes()
    assert looks_like_markup(data) == "<?xml"


def test_markup_without_a_doctype_is_detected() -> None:
    assert looks_like_markup(b'<meta name="MobileOptimized" content="width" />')


def test_a_tsv_containing_an_angle_bracket_is_not_markup() -> None:
    """One `<` in a data cell must not condemn the file."""
    data = b"CHR\tBP\tNOTE\n1\t123\ta<b\n2\t456\tplain\n"
    assert looks_like_markup(data) is None


def test_html_on_a_gz_url_is_non_data_not_a_decompression_error(fixtures_dir) -> None:
    """The core misdiagnosis: nothing is corrupt, the URL serves a page."""
    data = (fixtures_dir / "landing_page.html").read_bytes()
    verdict = classify_payload_prefix(
        data, filename="study.tsv.gz", headers={"Content-Type": "text/html; charset=utf-8"}
    )
    assert verdict.kind is PayloadKind.NON_DATA
    assert verdict.detected_markup


def test_content_type_alone_can_condemn_a_payload() -> None:
    verdict = classify_payload_prefix(
        b"some bytes that are not markup", filename="x.tsv", headers={"Content-Type": "text/html"}
    )
    assert verdict.kind is PayloadKind.NON_DATA


def test_octet_stream_is_not_evidence_of_anything() -> None:
    """`application/octet-stream` says "bytes"; it must not trigger a verdict."""
    verdict = classify_payload_prefix(
        b"CHR\tBP\n1\t2\n",
        filename="study.tsv",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert verdict.kind is PayloadKind.DATA


def test_real_gzip_beats_a_misleading_content_type() -> None:
    """Magic bytes outrank the header: many servers mislabel .gz as text/html."""
    verdict = classify_payload_prefix(
        gzip.compress(b"CHR\tBP\n1\t2\n"),
        filename="study.tsv.gz",
        headers={"Content-Type": "text/html"},
    )
    assert verdict.kind is PayloadKind.DATA
    assert verdict.declared_compression == "gzip"


def test_empty_payload_is_its_own_kind() -> None:
    assert classify_payload_prefix(b"").kind is PayloadKind.EMPTY


# ======================================================================
# Item 4: extension is a hint, not evidence
# ======================================================================


def test_magic_detection_returns_none_without_evidence() -> None:
    assert detect_compression_by_magic(b"CHR\tBP\n1\t2\n", "study.tsv.gz") is None


def test_magic_detection_recognises_real_compression() -> None:
    assert detect_compression_by_magic(gzip.compress(b"x")) is Compression.GZIP


def test_plaintext_named_gz_is_a_content_mismatch(fixtures_dir) -> None:
    """A `.gz` with no gzip magic is not a corrupt archive."""
    data = (fixtures_dir / "plaintext_named_gz.tsv.gz").read_bytes()
    verdict = classify_payload_prefix(data, filename="plaintext_named_gz.tsv.gz")
    assert verdict.kind is PayloadKind.CONTENT_MISMATCH
    assert verdict.declared_compression == "gzip"
    assert "hint, not evidence" in verdict.reason


def test_extension_still_works_as_a_last_resort() -> None:
    """detect_compression keeps the hint for callers that want it."""
    assert detect_compression(b"not compressed", "x.zip") is Compression.ZIP


# ======================================================================
# Item 5: the tar walker
# ======================================================================


def test_tar_walker_skips_metadata_members(fixtures_dir) -> None:
    """A directory entry and a README precede the data file.

    The old parser skipped exactly one 512-byte header, so it returned the
    README instead -- which is why valid ustar archives yielded no GWAS header
    despite containing one.
    """
    data = (fixtures_dir / "tar_with_metadata_first.tar").read_bytes()
    result = decompress_prefix(data, Compression.TAR)

    assert result.member_name == "study/metaanalysis.tbl"
    assert result.data.startswith(b"MarkerName\t")
    assert b"Supplementary information" not in result.data


def test_tar_walker_recovers_the_header_from_a_prefix(fixtures_dir) -> None:
    """The bytes were always there; only the parser could not reach them."""
    data = (fixtures_dir / "tar_with_metadata_first.tar").read_bytes()
    prober = RemoteProber()
    result = prober.probe_local(fixtures_dir / "tar_with_metadata_first.tar")

    assert result.succeeded, result.error
    assert "MarkerName" in result.header.raw_header
    assert "FreqAllele1HapMapCEU" in result.header.raw_header
    # And the newly vetted aliases resolve it.
    concepts = result.mapping.concepts()
    assert "variant_id" in concepts
    assert "effect_allele_frequency" in concepts
    assert len(data) > 0


def test_tar_walker_reports_when_no_data_member_exists() -> None:
    """Refuse honestly rather than returning a metadata block as data."""
    import io
    import tarfile

    from gwaspoker.failures import CompressionError

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo("only/")
        directory.type = tarfile.DIRTYPE
        directory.size = 0
        archive.addfile(directory)

    with pytest.raises(CompressionError):
        decompress_prefix(buffer.getvalue(), Compression.TAR)


# ======================================================================
# Item 6: zip member selection
# ======================================================================


def test_zip_selection_skips_resource_forks_and_documentation(fixtures_dir) -> None:
    """The data file is fifth in the archive, behind a PDF and a README."""
    data = (fixtures_dir / "zip_with_metadata_first.zip").read_bytes()
    result = decompress_prefix(data, Compression.ZIP)

    assert result.member_name == "study/sumstats.tsv"
    assert result.data.startswith(b"CHR\tBP\tSNP\t")
    assert b"%PDF" not in result.data
    assert b"Supplementary information" not in result.data


def test_zip_selection_works_without_the_central_directory(fixtures_dir) -> None:
    """A bounded prefix stops before the index, so selection walks local headers.

    This is the case that matters in practice: the central directory sits at the
    *end* of the archive, which a probe never reaches. Taking the first local
    header -- the previous behaviour -- returns the resource fork.
    """
    data = (fixtures_dir / "zip_with_metadata_first.zip").read_bytes()
    prefix = data[: data.index(b"PK\x01\x02")]

    result = decompress_prefix(prefix, Compression.ZIP)
    assert result.member_name == "study/sumstats.tsv"
    assert result.data.startswith(b"CHR\tBP\tSNP\t")
    assert "walked 4 non-data member(s)" in (result.note or "")


def test_zip_prefix_yields_a_mappable_header(fixtures_dir) -> None:
    """End to end: the columns were always present, only unreachable."""
    result = RemoteProber().probe_local(fixtures_dir / "zip_with_metadata_first.zip")

    assert result.succeeded, result.error
    assert "CHR" in result.header.raw_header
    assert {"p_value", "beta", "standard_error"} <= set(result.mapping.concepts())


def test_zip_with_only_noise_members_is_refused() -> None:
    """Say so, rather than scoring a PDF as a header."""
    import io
    import zipfile

    from gwaspoker.failures import CompressionError

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__MACOSX/._x", b"resource fork")
        archive.writestr("docs/README.md", b"Nothing to see here.\n" * 50)

    with pytest.raises(CompressionError):
        decompress_prefix(buffer.getvalue(), Compression.ZIP)


def test_streamed_zip_sizes_are_read_from_the_data_descriptor_path() -> None:
    """Zips written to a stream leave the local header's sizes at zero.

    General-purpose bit 3 defers them to a trailing descriptor, so a member
    cannot be skipped arithmetically; the walker scans for the next plausible
    signature instead. Verify it still lands on the data member.
    """
    import struct
    import zipfile
    import zlib

    from gwaspoker.probe.compression import _walk_zip_prefix

    def streamed_member(name: bytes, payload: bytes) -> bytes:
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
        body = compressor.compress(payload) + compressor.flush()
        header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0x08,  # bit 3: sizes follow the data
            zipfile.ZIP_DEFLATED,
            0,
            0,
            0,  # crc, in the descriptor
            0,  # compressed size, in the descriptor
            0,  # uncompressed size, in the descriptor
            len(name),
            0,
        )
        descriptor = struct.pack("<IIII", 0x08074B50, zlib.crc32(payload), len(body), len(payload))
        return header + name + body + descriptor

    blob = streamed_member(b"README.txt", b"Documentation only.\n" * 40) + streamed_member(
        b"sumstats.tsv",
        b"CHR\tBP\tSNP\tA1\tA2\tBETA\tSE\tP\n" + b"1\t1\trs1\tA\tG\t0.1\t0.02\t1e-8\n" * 100,
    )

    result = _walk_zip_prefix(blob, 65536)
    assert result.member_name == "sumstats.tsv"
    assert result.data.startswith(b"CHR\tBP\tSNP\t")


@pytest.mark.parametrize(
    ("name", "noise"),
    [
        ("study.results.txt", False),
        ("sumstats.tsv.gz", False),
        ("results/GCST123.tsv", False),
        ("manuscript.pdf", True),
        ("figure1.png", True),
        ("README", True),
        ("docs/readme.md", True),
        ("LICENSE.txt", True),
        ("__MACOSX/._x", True),
        ("checksums.md5", True),
        ("index.html", True),
        ("analysis.R", True),
        ("study/", True),
    ],
)
def test_archive_noise_classification(name, noise) -> None:
    """Extensions match at the end; documentation names match anywhere.

    Testing both matters: a plain substring rule would reject
    ``study.results.txt`` for containing ``.r``.
    """
    from gwaspoker.probe.compression import _is_archive_directory, _is_archive_noise

    assert (_is_archive_noise(name) or _is_archive_directory(name)) is noise


# ======================================================================
# Item 2/3 end to end: probe classification
# ======================================================================


@responses.activate
def test_probe_reports_html_as_non_data(config, fixtures_dir) -> None:
    body = (fixtures_dir / "landing_page.html").read_bytes()
    responses.add(
        responses.HEAD,
        URL,
        headers={"Accept-Ranges": "bytes", "Content-Type": "text/html"},
        status=200,
    )
    responses.add(responses.GET, URL, body=body, status=206, headers={"Content-Type": "text/html"})

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=8192)

    assert not result.succeeded
    assert result.failure_category is FailureCategory.NON_DATA_RESPONSE
    assert result.failure_category is not FailureCategory.DECOMPRESSION_ERROR
    assert "URL problem rather than a corrupt file" in result.error
    assert result.payload.kind is PayloadKind.NON_DATA


@responses.activate
def test_probe_reports_extension_mismatch_separately(config) -> None:
    """A `.gz` carrying readable text is a mismatch, not a broken archive."""
    body = b"\x00\x01\x02binary junk that is neither markup nor gzip" * 40
    responses.add(responses.HEAD, URL, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, body=body, status=206)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=8192)

    assert result.failure_category is FailureCategory.CONTENT_MISMATCH


@responses.activate
def test_html_never_becomes_a_detected_header(config, fixtures_dir) -> None:
    """`<meta | name= | content= | />` was previously a successful header."""
    body = (fixtures_dir / "landing_page.html").read_bytes()
    responses.add(responses.HEAD, "https://example.org/x.tsv", status=200)
    responses.add(responses.GET, "https://example.org/x.tsv", body=body, status=200)

    result = RemoteProber(config, HttpClient(config)).probe_url(
        "https://example.org/x.tsv", probe_bytes=8192
    )

    assert result.header is None
    assert not result.succeeded


# ======================================================================
# Item 7: resilient fetch
# ======================================================================


@responses.activate
def test_head_failure_does_not_abort_the_probe(config, fixtures_dir) -> None:
    """Old servers reject HEAD while serving GET perfectly well."""
    import requests

    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(responses.HEAD, URL, body=requests.exceptions.ConnectTimeout("nope"))
    responses.add(responses.GET, URL, body=body[:16384], status=206)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)

    assert result.succeeded, result.error
    methods = [a["method"] for a in result.transfer.attempts]
    assert methods[0] == "HEAD"
    assert result.transfer.attempts[0]["error"]


@responses.activate
def test_range_failure_falls_back_to_a_bounded_get(config, fixtures_dir) -> None:
    """The range attempt previously had no fallback: one blip ended the probe."""
    import requests

    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(responses.HEAD, URL, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, body=requests.exceptions.ReadTimeout("slow"))
    responses.add(responses.GET, URL, body=body[:16384], status=200)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)

    assert result.succeeded, result.error
    methods = [a["method"] for a in result.transfer.attempts]
    assert "GET_RANGE" in methods
    assert "GET_BOUNDED" in methods


@responses.activate
def test_404_stops_immediately_without_fallback(config) -> None:
    """A 404 is an answer. Retrying it would waste a request and confuse the log."""
    responses.add(responses.HEAD, URL, status=404)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL)

    assert result.failure_category is FailureCategory.HTTP_404
    assert len(result.transfer.attempts) == 1


@responses.activate
def test_byte_ceiling_survives_the_fallback_chain(config, fixtures_dir) -> None:
    """Falling back must never lift the bound."""
    import requests

    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(responses.HEAD, URL, body=requests.exceptions.ConnectTimeout("x"))
    responses.add(responses.GET, URL, body=requests.exceptions.ReadTimeout("y"))
    responses.add(responses.GET, URL, body=body, status=200)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)

    assert result.transfer.received_bytes <= 16384


# ======================================================================
# Items 8/9/17/18: provenance of the transfer
# ======================================================================


@responses.activate
def test_redirects_are_recorded(config, fixtures_dir) -> None:
    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    final = "https://cdn.example.org/real/study.tsv.gz"
    responses.add(responses.HEAD, URL, status=302, headers={"Location": final})
    responses.add(responses.HEAD, final, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, status=302, headers={"Location": final})
    responses.add(responses.GET, final, body=body[:16384], status=206)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)

    assert result.transfer.final_url == final
    assert result.transfer.redirect_count >= 1


@responses.activate
def test_content_type_is_recorded(config, fixtures_dir) -> None:
    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(
        responses.HEAD,
        URL,
        headers={"Accept-Ranges": "bytes", "Content-Type": "application/gzip"},
        status=200,
    )
    responses.add(
        responses.GET,
        URL,
        body=body[:16384],
        status=206,
        content_type="application/gzip",
    )

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)
    assert result.transfer.content_type == "application/gzip"
    assert result.to_dict()["transfer"]["content_type"] == "application/gzip"


@responses.activate
def test_every_attempt_is_recorded(config, fixtures_dir) -> None:
    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(responses.HEAD, URL, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, body=body[:16384], status=206)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)
    attempts = result.transfer.attempts

    assert [a["method"] for a in attempts] == ["HEAD", "GET_RANGE"]
    assert all("status" in a and "seconds" in a for a in attempts)
    assert result.transfer.request_count == len(attempts)


@responses.activate
def test_bytes_from_an_abandoned_attempt_still_count(config, fixtures_dir) -> None:
    """TransferStats claims to be "exactly what moved over the network".

    The manuscript's headline claim is transfer volume, so a partial read that
    later failed must not vanish from the total.
    """
    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(responses.HEAD, URL, headers={"Accept-Ranges": "bytes"}, status=200)
    responses.add(responses.GET, URL, body=body[:4096], status=416)
    responses.add(responses.GET, URL, body=body[:8192], status=200)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)

    counted = sum(a["bytes"] for a in result.transfer.attempts)
    assert result.transfer.received_bytes == counted
    assert result.transfer.received_bytes >= 8192


# ======================================================================
# Item 16: post-header sanity guard
# ======================================================================


def test_a_novel_schema_is_still_inspectable(fixtures_dir, tmp_path) -> None:
    """Requiring a mapping hit unconditionally would reject new formats.

    Rows that behave like a table are enough on their own.
    """
    path = tmp_path / "novel.tsv"
    path.write_text(
        "alpha\tbeta_col\tgamma\n1\t2.5\t3\n4\t5.5\t6\n7\t8.5\t9\n",
        encoding="utf-8",
    )
    result = RemoteProber().probe_local(path)
    assert result.succeeded, result.error


def test_prose_does_not_become_a_data_table(tmp_path) -> None:
    path = tmp_path / "prose.txt"
    path.write_text(
        "Access to this dataset requires an application.\n"
        "Please contact the data access committee for details.\n"
        "Applications are reviewed monthly by the committee.\n",
        encoding="utf-8",
    )
    result = RemoteProber().probe_local(path)
    assert not result.succeeded
    assert result.failure_category in (
        FailureCategory.UNSUPPORTED_FORMAT,
        FailureCategory.HEADER_NOT_FOUND,
    )


def test_real_gwas_files_are_unaffected(fixtures_dir) -> None:
    """The guard must not reject anything that previously worked."""
    prober = RemoteProber()
    for name in (
        "ssf_like.tsv.gz",
        "comment_preamble.tsv",
        "keyvalue_preamble.txt",
        "quoted_comma.csv",
        "semicolon.csv",
        "space_delimited.txt",
        "many_metadata_rows.tsv",
        "utf8_bom.tsv",
        "latin1_preamble.tsv",
        "archive.zip",
        "archive.tar.gz",
    ):
        result = prober.probe_local(fixtures_dir / name)
        assert result.succeeded, f"{name}: {result.error}"


@responses.activate
def test_head_content_type_survives_a_get_that_omits_it(config, fixtures_dir) -> None:
    """A GET without Content-Type must not erase what HEAD reported."""
    body = (fixtures_dir / "ssf_like.tsv.gz").read_bytes()
    responses.add(
        responses.HEAD,
        URL,
        headers={"Accept-Ranges": "bytes"},
        content_type="application/gzip",
        status=200,
    )
    responses.add(responses.GET, URL, body=body[:16384], status=206, content_type=None)

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=16384)
    assert result.transfer.content_type == "application/gzip"


@responses.activate
def test_a_terminal_status_still_records_where_it_landed(config) -> None:
    """A 404 reached after redirects is explained by the redirects.

    Metadata used to be captured only on the success branch, so the failure
    path -- the one a diagnosis is written from -- lost the final URL and the
    content type.
    """
    responses.add(
        responses.HEAD, URL, status=302, headers={"Location": "https://cdn.example.org/gone"}
    )
    responses.add(
        responses.HEAD, "https://cdn.example.org/gone", status=404, content_type="text/html"
    )

    result = RemoteProber(config, HttpClient(config)).probe_url(URL, probe_bytes=4096)

    assert not result.succeeded
    assert result.transfer.http_status == 404
    assert result.transfer.final_url == "https://cdn.example.org/gone"
    assert result.transfer.redirect_count == 1
    assert result.transfer.content_type == "text/html"
    assert [a["method"] for a in result.transfer.attempts] == ["HEAD"]


# ======================================================================
# Plain text under a .gz name is read, not refused
# ======================================================================


def test_mislabelled_gz_is_parsed_rather_than_refused(fixtures_dir) -> None:
    """A `.gz` holding ordinary TSV is a naming error, not a broken file.

    Classifying it as CONTENT_MISMATCH was correct but insufficient: the probe
    then stopped, so data GWASPoker can read was discarded over a wrong
    extension.
    """
    result = RemoteProber().probe_local(fixtures_dir / "plaintext_named_gz.tsv.gz")

    assert result.succeeded, result.error
    assert result.payload.kind is PayloadKind.CONTENT_MISMATCH
    assert result.payload.is_recoverable_mismatch
    assert result.compression is Compression.NONE
    assert {"p_value", "beta", "standard_error"} <= set(result.mapping.concepts())


def test_the_mismatch_is_reported_as_a_warning(fixtures_dir) -> None:
    """Recovering from it must not hide it: provenance still records the fact."""
    result = RemoteProber().probe_local(fixtures_dir / "plaintext_named_gz.tsv.gz")

    assert any("named as gzip" in w for w in result.warnings)
    assert result.to_dict()["warnings"]
    assert result.to_dict()["payload"]["is_textual"] is True
    # A warning explains a result; it never becomes one.
    assert result.failure_category is None


def test_binary_under_a_gz_name_is_still_a_failure(tmp_path) -> None:
    """Nothing to parse means nothing to recover."""
    import os

    path = tmp_path / "study.tsv.gz"
    path.write_bytes(b"\x00\x01\x02\x03" + os.urandom(4000))

    result = RemoteProber().probe_local(path)
    assert not result.succeeded
    assert result.failure_category is FailureCategory.CONTENT_MISMATCH
    assert not result.payload.is_recoverable_mismatch


@pytest.mark.parametrize(
    ("data", "textual"),
    [
        (b"CHR\tBP\tP\n1\t2\t0.5\n", True),
        (b"", False),
        (b"\x00\x01\x02\x03" * 100, False),
        ("CHR\tBP\n1\t2\n".encode("utf-16"), True),  # BOM contains NULs
        (b"caf\xc3\xa9 \xe2\x80\x94 notes\n" * 50, True),
        (bytes(range(32)) * 100, False),
    ],
)
def test_textual_detection(data, textual) -> None:
    from gwaspoker.probe.payload import looks_textual

    assert looks_textual(data) is textual


# ======================================================================
# Transfer accounting survives a stream that dies part-way
# ======================================================================


class _DyingResponse:
    """A streaming response that delivers real bytes, then drops the connection.

    `responses` cannot model this: it either returns a body or raises instead
    of returning one, never both. The interesting case is exactly in between.
    """

    status_code = 206
    headers = {"Content-Type": "application/gzip; charset=binary"}
    url = "https://cdn.example.org/study.tsv.gz"
    history = (object(),)

    def __init__(self, chunks: int = 3) -> None:
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=None):
        from requests.exceptions import ConnectionError as RequestsConnectionError

        for _ in range(self.chunks):
            yield b"x" * 32_768
        raise RequestsConnectionError("connection reset by peer")

    def close(self) -> None:
        self.closed = True


class _HeadOk:
    """A HEAD that advertises range support and nothing else."""

    status_code = 200
    headers = {"Accept-Ranges": "bytes"}
    url = URL
    history = ()
    ok = True

    def close(self) -> None:
        pass


def test_bytes_received_before_a_mid_stream_failure_are_counted(config) -> None:
    """96 KiB that arrived before a reset still crossed the network.

    `TransferStats` claims to be "exactly what moved over the network", and
    transfer reduction is the headline result, so an abandoned partial read
    must not be booked as zero. It used to be: the streaming loop and the
    request lived in one `try`, so the buffer was discarded with the exception.
    """
    from gwaspoker.failures import PartialTransferError

    client = HttpClient(config)
    dying = _DyingResponse()
    # `_session` is a thread-local property, so patch the session it hands back
    # rather than the attribute.
    client._session.get = lambda *_a, **_k: dying  # noqa: SLF001

    with pytest.raises(PartialTransferError) as caught:
        client.get_range(URL, start=0, length=1_000_000)

    exc = caught.value
    assert exc.bytes_received == 98_304
    assert exc.elapsed_seconds > 0
    assert exc.status == 206
    assert exc.final_url == "https://cdn.example.org/study.tsv.gz"
    assert exc.redirect_count == 1
    assert dying.closed, "the connection must be released even on the failure path"


def test_the_prober_books_those_bytes_against_the_transfer(config) -> None:
    """End to end: the partial read reaches TransferStats, not a zero."""
    from gwaspoker.probe.remote import TransferStats

    client = HttpClient(config)
    session = client._session  # noqa: SLF001
    session.head = lambda *_a, **_k: _HeadOk()
    session.get = lambda *_a, **_k: _DyingResponse()

    stats = TransferStats()
    prober = RemoteProber(config, client)
    # Both GETs die by design; the accounting they leave behind is under test.
    with contextlib.suppress(Exception):
        prober._fetch_prefix(URL, 1_000_000, stats)  # noqa: SLF001

    assert stats.received_bytes >= 98_304
    assert any(a["bytes"] == 98_304 for a in stats.attempts)


def test_partial_transfer_error_is_recorded_with_its_real_cost(config) -> None:
    """The prober books the failed attempt's bytes and seconds, not zeros."""
    from gwaspoker.failures import FailureCategory as FC
    from gwaspoker.failures import PartialTransferError
    from gwaspoker.probe.remote import TransferStats

    stats = TransferStats()
    RemoteProber._record_failed_attempt(
        stats,
        "GET_RANGE",
        PartialTransferError(
            "Range GET died",
            category=FC.NETWORK_ERROR,
            bytes_received=98_304,
            elapsed_seconds=4.25,
            status=200,
            final_url="https://cdn.example.org/study.tsv.gz",
            redirect_count=1,
            content_type="application/gzip; charset=binary",
        ),
    )

    assert stats.received_bytes == 98_304
    attempt = stats.attempts[-1]
    assert attempt["bytes"] == 98_304
    assert attempt["seconds"] == 4.25
    assert attempt["status"] == 200
    assert stats.final_url == "https://cdn.example.org/study.tsv.gz"
    assert stats.redirect_count == 1
    assert stats.content_type == "application/gzip"


def test_a_plain_remote_access_error_still_records_zero(config) -> None:
    """Only a *partial* transfer has bytes to claim."""
    from gwaspoker.failures import RemoteAccessError
    from gwaspoker.probe.remote import TransferStats

    stats = TransferStats()
    RemoteProber._record_failed_attempt(stats, "HEAD", RemoteAccessError("DNS failure"))

    assert stats.received_bytes == 0
    assert stats.attempts[-1]["bytes"] == 0
