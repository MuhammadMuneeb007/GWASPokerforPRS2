"""Column-name normalization.

Normalization is what lets ``aliases.yaml`` list ``p_value`` once instead of
listing ``P-Value``, ``p.value``, ``"P_VALUE"`` and ``P Value`` separately.

The transformation is deliberately conservative and, importantly, *reversible in
reporting*: the raw name is always carried alongside the normalized one, so a
report never shows the user a name that does not appear in their file.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters folded to a single underscore.
_PUNCTUATION = re.compile(r"[\s\-\.:;,/\\|()\[\]{}'\"`+*#@!?%^&=<>~$]+")
_UNDERSCORES = re.compile(r"_+")


def normalize_column_name(name: str) -> str:
    """Fold a raw column name to its canonical comparison form.

    Steps: strip a UTF-8 BOM; NFKD-normalize and drop combining marks; case-fold;
    replace punctuation and whitespace runs with ``_``; collapse repeated ``_``;
    trim leading and trailing ``_``.

    >>> normalize_column_name("P-Value")
    'p_value'
    >>> normalize_column_name("  Effect Allele  ")
    'effect_allele'
    >>> normalize_column_name('"OR(A1)"')
    'or_a1'
    """
    if name is None:
        return ""
    text = str(name).replace("﻿", "").strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _PUNCTUATION.sub("_", text)
    text = _UNDERSCORES.sub("_", text)
    return text.strip("_")


def normalize_header(header) -> tuple[str, ...]:
    """Normalize a whole header row, preserving order and length.

    Order is preserved because it matters: positional readers depend on it, and
    the benchmark scores exact ordered header match. v1 collapsed headers into a
    ``set``, losing both order and duplicate columns.
    """
    return tuple(normalize_column_name(c) for c in header)


def is_probably_data_row(values, *, min_numeric_fraction: float = 0.4) -> bool:
    """Heuristic: does this row look like data rather than a header?

    Used by the header detector to confirm that a candidate header is followed
    by data. Genomic data rows are numeric-heavy (position, beta, p-value) even
    when they carry string columns for alleles and rsIDs.
    """
    values = [v for v in values if str(v).strip() != ""]
    if not values:
        return False
    numeric = sum(1 for v in values if _looks_numeric(v))
    return numeric / len(values) >= min_numeric_fraction


_NUMERIC_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
#: Missing-value markers seen in real summary statistics, including the "#NA"
#: that the GWAS-SSF standard specifies.
_NA_TOKENS = {"na", "nan", "n/a", "#na", ".", "-", "null", "none", ""}


def _looks_numeric(value) -> bool:
    text = str(value).strip()
    if text.casefold() in _NA_TOKENS:
        return False
    return bool(_NUMERIC_RE.match(text))


def looks_like_missing(value) -> bool:
    """True for the missing-value markers used in summary statistics files."""
    return str(value).strip().casefold() in _NA_TOKENS
