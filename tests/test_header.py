"""Header and delimiter detection.

Each test names the v1 failure mode it covers. v1 relied on pandas' inference
with ``comment='#'``, which breaks on blank lines, non-``#`` preambles,
unexpected delimiters and unusual encodings, and then discarded header order by
converting to a ``set``.
"""

from __future__ import annotations

import pytest

from gwaspoker.failures import HeaderDetectionError
from gwaspoker.probe.encoding import detect_encoding, split_complete_lines
from gwaspoker.probe.header import (
    detect_delimiter,
    detect_header,
    is_comment_line,
    looks_like_key_value,
    split_line,
)


def _lines(fixtures_dir, name: str) -> list[str]:
    """Decode a fixture the way the probe pipeline does."""
    data = (fixtures_dir / name).read_bytes()
    result = detect_encoding(data)
    complete, _partial = split_complete_lines(result.text)
    return complete


# ----------------------------------------------------------------------
# Line splitting
# ----------------------------------------------------------------------


def test_split_line_tab() -> None:
    assert split_line("a\tb\tc", "\t") == ("a", "b", "c")


def test_split_line_collapses_whitespace_runs() -> None:
    """v1 split on a single space, so aligned columns produced empty fields."""
    assert split_line("1   12345    rs1", " ") == ("1", "12345", "rs1")


def test_split_line_honours_quoting() -> None:
    assert split_line('"a,b",c,d', ",") == ("a,b", "c", "d")


def test_is_comment_line() -> None:
    assert is_comment_line("# comment")
    assert is_comment_line("## comment")
    assert not is_comment_line("CHR\tPOS")
    assert not is_comment_line("")


def test_looks_like_key_value() -> None:
    """Preamble lines with no comment marker, which comment='#' does not skip."""
    assert looks_like_key_value("study=ABC")
    assert looks_like_key_value("author: Jane Doe")
    assert not looks_like_key_value("CHR\tPOS\tSNP")
    assert not looks_like_key_value("MarkerName Allele1 Allele2")


# ----------------------------------------------------------------------
# Delimiter detection
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("comment_preamble.tsv", "\t"),
        ("quoted_comma.csv", ","),
        ("space_delimited.txt", " "),
        ("semicolon.csv", ";"),
        ("many_metadata_rows.tsv", "\t"),
    ],
)
def test_detect_delimiter(fixtures_dir, name, expected) -> None:
    delimiter, confidence = detect_delimiter(_lines(fixtures_dir, name))
    assert delimiter == expected
    assert confidence > 0


def test_detect_delimiter_on_empty_input_defaults_to_tab() -> None:
    delimiter, confidence = detect_delimiter([])
    assert delimiter == "\t"
    assert confidence == 0.0


# ----------------------------------------------------------------------
# Header detection
# ----------------------------------------------------------------------


def test_hash_comment_preamble(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "comment_preamble.tsv"))
    assert result.header_row_index == 2
    assert result.raw_header == ("CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P")
    assert result.delimiter == "\t"
    assert len(result.preamble_lines) == 2
    assert result.confidence > 0.5


def test_key_value_preamble_with_blank_line(fixtures_dir) -> None:
    """v1 read 'study=ABC' as the header: comment='#' does not skip it."""
    result = detect_header(_lines(fixtures_dir, "keyvalue_preamble.txt"))
    assert result.raw_header == (
        "MarkerName",
        "Allele1",
        "Allele2",
        "Effect",
        "StdErr",
        "P-value",
        "N",
    )
    assert result.delimiter == " "
    assert "study=ABC" not in result.raw_header


def test_blank_lines_are_not_headers(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "many_metadata_rows.tsv"))
    assert result.header_row_index == 26
    assert result.raw_header[0] == "chromosome"
    assert len(result.raw_header) == 8


def test_quoted_comma_header(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "quoted_comma.csv"))
    assert result.delimiter == ","
    assert result.raw_header[0] == "variant_id"
    assert '"' not in result.raw_header[0]


def test_latin1_preamble(fixtures_dir) -> None:
    """Decoding as UTF-8 raises on this file; detection must still work."""
    data = (fixtures_dir / "latin1_preamble.tsv").read_bytes()
    encoding_result = detect_encoding(data)
    assert encoding_result.encoding.lower() not in ("ascii",)
    complete, _ = split_complete_lines(encoding_result.text)
    result = detect_header(complete, encoding=encoding_result.encoding)
    assert result.raw_header == ("CHR", "BP", "SNP", "EA", "NEA", "OR", "SE", "PVAL")
    assert result.header_row_index == 2


def test_utf8_bom_is_stripped_from_first_column(fixtures_dir) -> None:
    """Without BOM handling the first column never matches an alias."""
    result = detect_header(_lines(fixtures_dir, "utf8_bom.tsv"))
    assert result.raw_header[0].lstrip("﻿") == "chromosome"

    from gwaspoker.mapping.mapper import get_mapper

    mapping = get_mapper().map_header(result.raw_header)
    assert mapping.columns[0].canonical_name == "chromosome"


def test_semicolon_delimiter(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "semicolon.csv"))
    assert result.delimiter == ";"
    assert result.raw_header == ("chr", "pos", "snp", "a1", "a2", "or", "se", "pval")


def test_space_delimiter(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "space_delimited.txt"))
    assert result.delimiter == " "
    assert result.raw_header[0] == "SNP"
    assert len(result.raw_header) == 10


def test_header_order_is_preserved_exactly(fixtures_dir) -> None:
    """v1's set() conversion destroyed order and collapsed duplicates."""
    result = detect_header(_lines(fixtures_dir, "comment_preamble.tsv"))
    assert isinstance(result.raw_header, tuple)
    assert list(result.raw_header) == ["CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P"]


def test_headerless_file_scores_low(fixtures_dir) -> None:
    """A file with no header must not yield a confident one."""
    result = detect_header(_lines(fixtures_dir, "headerless.tsv"))
    # The first data row is the best available candidate, but it is numeric-heavy
    # so confidence must stay low enough for a reader to notice.
    assert result.confidence < 0.5


def test_empty_input_raises() -> None:
    with pytest.raises(HeaderDetectionError):
        detect_header([])


def test_no_splittable_line_raises() -> None:
    with pytest.raises(HeaderDetectionError):
        detect_header(["single", "column", "only"])


def test_result_serialises(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "comment_preamble.tsv"))
    payload = result.to_dict()
    assert payload["header_row_index"] == 2
    assert payload["delimiter_label"] == "tab"
    assert payload["field_count"] == 8
    assert isinstance(payload["raw_header"], list)


def test_sample_rows_are_captured(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "comment_preamble.tsv"))
    assert result.data_rows_seen == 3
    assert result.sample_rows[0][0] == "1"


def test_explicit_delimiter_is_honoured(fixtures_dir) -> None:
    result = detect_header(_lines(fixtures_dir, "comment_preamble.tsv"), delimiter="\t")
    assert result.delimiter == "\t"
