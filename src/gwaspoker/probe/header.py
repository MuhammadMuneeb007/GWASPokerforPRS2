"""Robust header detection.

v1 assumed the header was whatever ``pandas`` decided after ``comment='#'``, and
broke on blank lines, non-``#`` metadata preambles (``study=ABC``), multi-line
metadata, unusual encodings and unexpected delimiters. It then discarded header
order by converting to a ``set``.

This detector does not assume. It generates every line in the probe as a
*candidate* header and scores each on evidence:

=================================  =======================================
Feature                            Why it discriminates
=================================  =======================================
GWAS vocabulary hits               A header says ``chromosome``; data says ``1``
Field count agreement with data    A header has the same arity as its rows
Following rows look like data      Numeric-heavy rows follow a header
Non-numeric cells                  Header cells are words, not numbers
Column-name uniqueness             Real headers rarely repeat a name
Field count plausibility           2-200 fields
Position in the file               Earlier lines are likelier, mildly
=================================  =======================================

The delimiter is chosen jointly with the header, because the two decisions are
not independent: a line's field count is meaningless until a delimiter is fixed.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

from gwaspoker.failures import HeaderDetectionError
from gwaspoker.mapping.mapper import UNKNOWN_CONCEPT, ColumnMapper, get_mapper
from gwaspoker.mapping.normalize import is_probably_data_row, normalize_column_name

logger = logging.getLogger(__name__)

#: Delimiters tried, in the order they are preferred on a tie. Tab first: the
#: GWAS-SSF standard mandates TSV, and it dominates the Catalog.
CANDIDATE_DELIMITERS: tuple[str, ...] = ("\t", ",", ";", " ", "|")

#: Prefixes that mark a metadata or comment line.
COMMENT_PREFIXES: tuple[str, ...] = ("#", "##", "!", ";;", "//")

#: Vocabulary fragments that strongly indicate a GWAS header line. Used only as
#: a *scoring* signal -- authoritative naming lives in mapping/aliases.yaml.
_GWAS_VOCABULARY: frozenset[str] = frozenset(
    {
        "chr",
        "chrom",
        "chromosome",
        "pos",
        "position",
        "bp",
        "base_pair_location",
        "snp",
        "rsid",
        "variant_id",
        "markername",
        "marker",
        "a1",
        "a2",
        "allele1",
        "allele2",
        "effect_allele",
        "other_allele",
        "ea",
        "nea",
        "beta",
        "or",
        "odds_ratio",
        "effect",
        "log_odds",
        "z",
        "zscore",
        "se",
        "standard_error",
        "stderr",
        "p",
        "pval",
        "pvalue",
        "p_value",
        "n",
        "sample_size",
        "n_cases",
        "n_controls",
        "eaf",
        "maf",
        "freq",
        "effect_allele_frequency",
        "info",
        "direction",
    }
)


@dataclass
class HeaderCandidate:
    """One scored candidate header line."""

    row_index: int
    raw_line: str
    delimiter: str
    fields: tuple[str, ...]
    score: float = 0.0
    features: dict[str, float] = field(default_factory=dict)

    @property
    def field_count(self) -> int:
        return len(self.fields)


@dataclass
class HeaderDetectionResult:
    """The detected header, plus everything a reviewer needs to check it."""

    header_row_index: int
    raw_header: tuple[str, ...]
    raw_header_line: str
    delimiter: str
    confidence: float
    encoding: Optional[str] = None
    preamble_lines: tuple[str, ...] = ()
    data_rows_seen: int = 0
    sample_rows: tuple[tuple[str, ...], ...] = ()
    runner_up_score: Optional[float] = None
    features: dict[str, float] = field(default_factory=dict)

    @property
    def delimiter_label(self) -> str:
        """Human-readable delimiter name."""
        return {"\t": "tab", ",": "comma", ";": "semicolon", " ": "space", "|": "pipe"}.get(
            self.delimiter, repr(self.delimiter)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "header_row_index": self.header_row_index,
            "raw_header": list(self.raw_header),
            "raw_header_line": self.raw_header_line,
            "delimiter": self.delimiter,
            "delimiter_label": self.delimiter_label,
            "encoding": self.encoding,
            "confidence": round(self.confidence, 3),
            "field_count": len(self.raw_header),
            "preamble_line_count": len(self.preamble_lines),
            "preamble_lines": list(self.preamble_lines),
            "data_rows_seen": self.data_rows_seen,
            "runner_up_score": round(self.runner_up_score, 3) if self.runner_up_score else None,
            "features": {k: round(v, 3) for k, v in self.features.items()},
        }


def split_line(line: str, delimiter: str) -> tuple[str, ...]:
    """Split one line on ``delimiter``, honouring quoting.

    Runs of whitespace collapse for the space delimiter, so
    ``"1  12345   rs1"`` yields three fields rather than five.
    """
    if delimiter == " ":
        return tuple(part for part in line.split() if part != "")
    try:
        reader = csv.reader(io.StringIO(line), delimiter=delimiter, skipinitialspace=True)
        row = next(reader, [])
    except (csv.Error, ValueError):
        row = line.split(delimiter)
    return tuple(cell.strip() for cell in row)


def is_comment_line(line: str) -> bool:
    """True for a comment or metadata-preamble line."""
    stripped = line.strip()
    if not stripped:
        return False
    return stripped.startswith(COMMENT_PREFIXES)


def looks_like_key_value(line: str) -> bool:
    """True for a ``key=value`` or ``key: value`` metadata line.

    Preambles of this shape (``study=ABC``, ``author=XYZ``) carry no comment
    marker, so v1's ``comment='#'`` did not skip them and pandas read the first
    one as the header.
    """
    stripped = line.strip()
    if not stripped or "\t" in stripped:
        return False
    for separator in ("=", ": "):
        if separator in stripped:
            key, _, value = stripped.partition(separator)
            if key.strip() and " " not in key.strip() and value.strip():
                return True
    return False


def detect_delimiter(lines: Iterable[str], *, sample_size: int = 20) -> tuple[str, float]:
    """Choose the delimiter that yields the most consistent field counts.

    Returns ``(delimiter, confidence)``. Consistency matters more than raw field
    count: splitting free text on spaces produces many fields, but a wildly
    varying number of them.

    The sample is the first ``sample_size`` *usable* lines, not the first
    ``sample_size`` lines. A file with a 25-line metadata preamble would
    otherwise offer nothing to measure.
    """
    sample: list[str] = []
    for line in lines:
        if line.strip() and not is_comment_line(line):
            sample.append(line)
            if len(sample) >= sample_size:
                break
    if not sample:
        return "\t", 0.0

    best_delimiter = "\t"
    best_score = -1.0
    for delimiter in CANDIDATE_DELIMITERS:
        counts = [len(split_line(ln, delimiter)) for ln in sample]
        multi = [c for c in counts if c >= 2]
        if not multi:
            continue
        modal = max(set(multi), key=multi.count)
        agreement = multi.count(modal) / len(counts)
        # Reward agreement heavily, field count mildly and with saturation.
        score = agreement * 10.0 + min(modal, 30) * 0.1
        if delimiter == "\t":
            score += 0.35  # tab is the GWAS-SSF mandate; break ties toward it
        if score > best_score:
            best_score, best_delimiter = score, delimiter

    if best_score < 0:
        return "\t", 0.0
    return best_delimiter, min(1.0, best_score / 10.5)


class HeaderDetector:
    """Scores candidate header lines and picks the best."""

    def __init__(self, mapper: Optional[ColumnMapper] = None) -> None:
        self.mapper = mapper or get_mapper()

    def detect(
        self,
        lines: list[str],
        *,
        encoding: Optional[str] = None,
        max_scan_lines: int = 200,
        delimiter: Optional[str] = None,
    ) -> HeaderDetectionResult:
        """Find the header row among ``lines``.

        ``lines`` should be complete lines only; a trailing partial line from a
        bounded probe must be dropped by the caller (see
        :func:`gwaspoker.probe.encoding.split_complete_lines`).
        """
        if not lines:
            raise HeaderDetectionError("No lines were available to inspect")

        scan = lines[:max_scan_lines]
        delimiters = (delimiter,) if delimiter else CANDIDATE_DELIMITERS

        candidates: list[HeaderCandidate] = []
        for delim in delimiters:
            candidates.extend(self._candidates_for_delimiter(scan, delim))

        if not candidates:
            raise HeaderDetectionError(
                "No line in the inspected bytes could be split into two or more "
                "fields by any supported delimiter"
            )

        candidates.sort(key=lambda c: c.score, reverse=True)
        best = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else None

        if best.score <= 0:
            raise HeaderDetectionError(
                "No candidate line scored as a plausible GWAS header; the file may "
                "be headerless or in an unsupported format"
            )

        preamble = tuple(scan[: best.row_index])
        data_rows = [
            split_line(ln, best.delimiter)
            for ln in scan[best.row_index + 1 :]
            if ln.strip() and not is_comment_line(ln)
        ]

        return HeaderDetectionResult(
            header_row_index=best.row_index,
            raw_header=best.fields,
            raw_header_line=best.raw_line,
            delimiter=best.delimiter,
            confidence=self._confidence(best, runner_up),
            encoding=encoding,
            preamble_lines=preamble,
            data_rows_seen=len(data_rows),
            sample_rows=tuple(data_rows[:5]),
            runner_up_score=runner_up,
            features=best.features,
        )

    # ------------------------------------------------------------------

    def _candidates_for_delimiter(self, lines: list[str], delimiter: str) -> list[HeaderCandidate]:
        candidates: list[HeaderCandidate] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            fields = split_line(line, delimiter)
            if len(fields) < 2:
                continue
            candidate = HeaderCandidate(
                row_index=index,
                raw_line=line,
                delimiter=delimiter,
                fields=fields,
            )
            self._score(candidate, lines, index, delimiter)
            candidates.append(candidate)
        return candidates

    def _score(
        self,
        candidate: HeaderCandidate,
        lines: list[str],
        index: int,
        delimiter: str,
    ) -> None:
        """Assign a score from independently interpretable features."""
        features: dict[str, float] = {}
        fields = candidate.fields
        normalized = [normalize_column_name(f) for f in fields]

        # -- 1. Known GWAS vocabulary, via the canonical mapper -------------
        mapped = [self.mapper.map_column(f) for f in fields]
        recognised = sum(1 for m in mapped if m.canonical_name != UNKNOWN_CONCEPT)
        recognised_fraction = recognised / len(fields)
        features["mapped_fraction"] = recognised_fraction
        features["mapped_score"] = recognised_fraction * 6.0

        vocabulary_hits = sum(1 for n in normalized if n in _GWAS_VOCABULARY)
        features["vocabulary_hits"] = float(vocabulary_hits)
        features["vocabulary_score"] = min(vocabulary_hits, 8) * 0.45

        # -- 2. Header cells should be words, not numbers -------------------
        numeric_cells = sum(1 for f in fields if _is_numeric(f))
        numeric_fraction = numeric_cells / len(fields)
        features["numeric_cell_fraction"] = numeric_fraction
        # A row that is mostly numbers is data, not a header.
        features["non_numeric_score"] = (1.0 - numeric_fraction) * 3.0 - (
            4.0 if numeric_fraction > 0.5 else 0.0
        )

        # -- 3. Column names should be unique -------------------------------
        non_empty = [n for n in normalized if n]
        uniqueness = len(set(non_empty)) / len(non_empty) if non_empty else 0.0
        features["uniqueness"] = uniqueness
        features["uniqueness_score"] = uniqueness * 1.5

        # -- 4. Following lines should look like data with the same arity ----
        following = [
            split_line(ln, delimiter)
            for ln in lines[index + 1 : index + 8]
            if ln.strip() and not is_comment_line(ln)
        ]
        if following:
            same_arity = sum(1 for row in following if len(row) == len(fields))
            arity_agreement = same_arity / len(following)
            data_like = sum(1 for row in following if is_probably_data_row(row))
            data_fraction = data_like / len(following)
            features["arity_agreement"] = arity_agreement
            features["following_data_fraction"] = data_fraction
            features["followed_by_data_score"] = arity_agreement * 3.0 + data_fraction * 3.0
        else:
            features["arity_agreement"] = 0.0
            features["following_data_fraction"] = 0.0
            # Nothing follows: this may be the last line of a truncated probe.
            features["followed_by_data_score"] = -1.0

        # -- 5. Field count plausibility ------------------------------------
        count = len(fields)
        features["field_count"] = float(count)
        if 3 <= count <= 200:
            features["field_count_score"] = 1.0
        elif count == 2:
            features["field_count_score"] = -0.5
        else:
            features["field_count_score"] = -2.0

        # -- 6. Comment / preamble penalties ---------------------------------
        penalty = 0.0
        if is_comment_line(candidate.raw_line):
            # A '#'-prefixed line is usually a comment -- but GWAS files do
            # sometimes mark the header itself with '#' (VCF-style "#CHROM"),
            # so this is a penalty rather than an exclusion.
            penalty -= 2.5
            if recognised_fraction >= 0.5:
                penalty += 2.0
        if looks_like_key_value(candidate.raw_line):
            penalty -= 4.0
        empty_fields = sum(1 for f in fields if not f.strip())
        if empty_fields:
            penalty -= empty_fields * 0.5
        features["penalty"] = penalty

        # -- 7. Earlier lines are mildly preferred ---------------------------
        features["position_score"] = max(0.0, 1.0 - index * 0.02)

        # -- 8. Tab is the GWAS-SSF mandated delimiter -----------------------
        features["delimiter_prior"] = 0.3 if delimiter == "\t" else 0.0

        candidate.features = features
        candidate.score = (
            features["mapped_score"]
            + features["vocabulary_score"]
            + features["non_numeric_score"]
            + features["uniqueness_score"]
            + features["followed_by_data_score"]
            + features["field_count_score"]
            + features["position_score"]
            + features["delimiter_prior"]
            + penalty
        )

    @staticmethod
    def _confidence(best: HeaderCandidate, runner_up: Optional[float]) -> float:
        """Map an unbounded score onto [0, 1].

        Two components: how strong the winner is in absolute terms, and how far
        clear of the runner-up it is. A header that scores well but is nearly
        tied with an alternative reading should not be reported as certain.
        """
        absolute = min(1.0, max(0.0, best.score / 14.0))
        if runner_up is None or best.score <= 0:
            margin = 1.0
        else:
            gap = best.score - runner_up
            margin = min(1.0, max(0.0, gap / 4.0))
        return round(0.7 * absolute + 0.3 * margin, 4)


def detect_header(
    lines: list[str],
    *,
    encoding: Optional[str] = None,
    max_scan_lines: int = 200,
    delimiter: Optional[str] = None,
    mapper: Optional[ColumnMapper] = None,
) -> HeaderDetectionResult:
    """Convenience wrapper around :class:`HeaderDetector`."""
    return HeaderDetector(mapper).detect(
        lines,
        encoding=encoding,
        max_scan_lines=max_scan_lines,
        delimiter=delimiter,
    )


def _is_numeric(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True
