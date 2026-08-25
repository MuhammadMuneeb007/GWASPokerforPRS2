"""File format facts shared by the extraction and scanning code."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

#: Extensions that hold tabular summary statistics. Preserved from the v1
#: ``poker()`` dispatch, which handled these same suffixes.
TABULAR_EXTENSIONS: tuple[str, ...] = (
    ".tsv",
    ".txt",
    ".csv",
    ".tab",
    ".ma",
    ".assoc",
    ".meta",
    ".tbl",
    ".linear",
    ".logistic",
    ".sumstats",
    ".gwas",
    ".regenie",
    ".out",
)

#: Container/compression extensions.
COMPRESSED_EXTENSIONS: tuple[str, ...] = (
    ".gz",
    ".bgz",
    ".zip",
    ".tar",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
)

#: Spreadsheet formats, which need the optional ``[excel]`` extra.
SPREADSHEET_EXTENSIONS: tuple[str, ...] = (".xlsx", ".xls")


def strip_compression_suffix(name: str) -> str:
    """Remove one trailing compression suffix.

    >>> strip_compression_suffix("study.tsv.gz")
    'study.tsv'
    >>> strip_compression_suffix("study.tsv")
    'study.tsv'
    """
    lowered = name.lower()
    for suffix in COMPRESSED_EXTENSIONS:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def inner_extension(name: str) -> str:
    """Extension after any compression suffix is removed.

    >>> inner_extension("GCST123_buildGRCh37.tsv.gz")
    '.tsv'
    >>> inner_extension("archive.tar.gz")
    '.tar'
    """
    return Path(strip_compression_suffix(name)).suffix.lower()


def is_tabular(name: str) -> bool:
    """True if the filename suggests tabular summary statistics."""
    return inner_extension(name) in TABULAR_EXTENSIONS


def is_spreadsheet(name: str) -> bool:
    return Path(name).suffix.lower() in SPREADSHEET_EXTENSIONS


def delimiter_for_extension(name: str) -> Optional[str]:
    """Delimiter implied by the filename, if any.

    Used only as a prior; the detected delimiter always wins, because
    misnamed files are common (``.csv`` files that are tab-separated).
    """
    extension = inner_extension(name)
    if extension in (".tsv", ".tab"):
        return "\t"
    if extension == ".csv":
        return ","
    return None


def human_size(num_bytes: Optional[int]) -> str:
    """Format a byte count for display.

    >>> human_size(1_572_864)
    '1.50 MB'
    >>> human_size(None)
    'unknown'
    """
    if num_bytes is None:
        return "unknown"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"
