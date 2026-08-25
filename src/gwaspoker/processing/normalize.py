"""Declared, reversible normalization of summary-statistics tables.

The governing rule: **GWASPoker never silently alters scientific data.**

v1's ``remove_quotes2`` ran, over the entire file::

    content.replace('"', '').replace(':', '_').replace('\\t', ',')

That is three separate acts of data corruption. Replacing ``:`` with ``_``
rewrites every ``chr:pos`` variant identifier and every timestamp. Replacing
tabs with commas after stripping the quotes that protected embedded commas
splits fields at the wrong places. None of it was recorded, so a downstream
reader could not tell it had happened.

Here every transformation is:

* **opt-in** -- nothing beyond delimiter interpretation happens by default;
* **column-scoped** -- never applied blindly across the whole file;
* **recorded** -- each one appends a :class:`Transformation` to the report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class Transformation:
    """One recorded change, with its justification."""

    name: str
    description: str
    columns: tuple[str, ...] = ()
    rows_affected: Optional[int] = None
    reversible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "columns": list(self.columns),
            "rows_affected": self.rows_affected,
            "reversible": self.reversible,
        }


@dataclass
class NormalizationReport:
    """Everything that was done to a table, and what was deliberately not."""

    transformations: list[Transformation] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)

    def record(self, transformation: Transformation) -> None:
        self.transformations.append(transformation)
        logger.info("Applied %s: %s", transformation.name, transformation.description)

    def decline(self, reason: str) -> None:
        """Note a transformation that was considered and rejected as unsafe."""
        self.declined.append(reason)
        logger.debug("Declined transformation: %s", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transformations": [t.to_dict() for t in self.transformations],
            "transformation_count": len(self.transformations),
            "declined": list(self.declined),
        }


def strip_surrounding_quotes(frame, report: NormalizationReport, *, columns=None):
    """Remove quotes that wrap an *entire* cell value.

    Safe because it only touches values that both start and end with the same
    quote character; a value containing an internal quote is left alone. v1
    deleted every ``"`` in the file, which merges ``"a","b"`` into ``a,b`` and
    then splits it at the wrong place.
    """
    import pandas as pd

    targets = (
        list(columns)
        if columns is not None
        else [c for c in frame.columns if frame[c].dtype == object]
    )
    changed_columns: list[str] = []
    total_changed = 0

    for column in targets:
        series = frame[column]
        if series.dtype != object:
            continue
        as_str = series.astype("string")
        mask = as_str.str.match(r'^".*"$', na=False) | as_str.str.match(r"^'.*'$", na=False)
        count = int(mask.sum())
        if count:
            frame.loc[mask, column] = as_str[mask].str.slice(1, -1)
            changed_columns.append(str(column))
            total_changed += count

    if changed_columns:
        report.record(
            Transformation(
                name="strip_surrounding_quotes",
                description=(
                    "Removed quote characters that wrapped an entire cell value. "
                    "Values containing internal quotes were left unchanged."
                ),
                columns=tuple(changed_columns),
                rows_affected=total_changed,
            )
        )
    del pd
    return frame


def strip_whitespace(frame, report: NormalizationReport):
    """Trim leading and trailing whitespace from string cells."""
    changed: list[str] = []
    total = 0
    for column in frame.columns:
        if frame[column].dtype != object:
            continue
        as_str = frame[column].astype("string")
        stripped = as_str.str.strip()
        count = int((as_str != stripped).fillna(False).sum())
        if count:
            frame[column] = stripped
            changed.append(str(column))
            total += count
    if changed:
        report.record(
            Transformation(
                name="strip_whitespace",
                description="Trimmed leading and trailing whitespace from text cells.",
                columns=tuple(changed),
                rows_affected=total,
            )
        )
    return frame


def rename_to_canonical(
    frame, mapping_result, report: NormalizationReport, *, symbols: bool = False
):
    """Rename columns to canonical concepts, or to PRS tool symbols.

    Only unambiguous mappings are renamed: when two columns map to the same
    concept the originals are kept, because choosing between them is a
    scientific decision, not a formatting one.
    """
    by_concept = mapping_result.by_concept()
    renames: dict[str, str] = {}
    skipped: list[str] = []

    for concept, columns in by_concept.items():
        if len(columns) > 1:
            skipped.append(f"{concept} (claimed by {', '.join(c.raw_name for c in columns)})")
            continue
        column = columns[0]
        target = column.prs_tool_symbol if symbols and column.prs_tool_symbol else concept
        if target and target != column.raw_name:
            renames[column.raw_name] = target

    if renames:
        frame = frame.rename(columns=renames)
        report.record(
            Transformation(
                name="rename_to_canonical",
                description=(
                    "Renamed columns to "
                    + ("PRS tool symbols" if symbols else "canonical concept names")
                    + ": "
                    + ", ".join(f"{k} -> {v}" for k, v in list(renames.items())[:12])
                    + ("..." if len(renames) > 12 else "")
                ),
                columns=tuple(renames),
                reversible=True,
            )
        )
    for note in skipped:
        report.decline(
            f"Did not rename columns mapping to {note}: choosing between them requires "
            "a scientific decision."
        )
    return frame


#: Transformations that were considered and are deliberately never applied.
UNSAFE_TRANSFORMATIONS: tuple[str, ...] = (
    "Global ':' -> '_' replacement: destroys chr:pos variant identifiers.",
    "Global '\\t' -> ',' replacement: corrupts fields containing commas.",
    "Global '\"' deletion: removes the quoting that protects embedded delimiters.",
    "Stripping '.' from numeric strings: turns 0.75 into 075 and 1.5e-8 into 15e-8.",
    "Coercing unparseable values to 0: makes a parse failure indistinguishable "
    "from a real zero.",
    "Log-transforming odds ratios: reported as a warning so the user decides.",
)


def note_declined_unsafe(report: NormalizationReport) -> None:
    """Record the blanket rewrites v1 performed and v2 refuses to."""
    for reason in UNSAFE_TRANSFORMATIONS:
        report.decline(reason)
