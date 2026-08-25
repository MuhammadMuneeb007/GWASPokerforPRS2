"""Benchmark manifest: the schema, and the code that fills the prediction half.

A manifest row has three parts:

**Study identity** -- accession, trait, source, year, SSF status.

**GWASPoker predictions** -- filled by ``gwaspoker benchmark --run``: which route
answered, how many bytes moved, what header was detected, what mapping was
produced, what verdict followed.

**Ground truth** -- ``ground_truth_header``, ``ground_truth_mapping``,
``ground_truth_prs_ready``. These are **never** written by GWASPoker. They are
curated externally, by reading the file or its documentation by hand.

That separation is the whole point. Scoring a parser against labels the same
parser produced measures nothing. :func:`validate_manifest` refuses to evaluate
a manifest whose ground-truth columns were machine-filled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Columns identifying the study. Filled by the curator or by ``--run``.
IDENTITY_COLUMNS: tuple[str, ...] = (
    "study_accession",
    "trait",
    "source",
    "publication_year",
    "ssf_status",
)

#: Columns describing the structured-API route.
API_COLUMNS: tuple[str, ...] = (
    "summary_statistics_api_available",
    "api_sufficient",
    "api_bytes",
    "api_latency",
)

#: Columns describing the file and the probe.
FILE_COLUMNS: tuple[str, ...] = (
    "remote_file_url",
    "file_format",
    "compression",
    "full_file_size",
    "probe_bytes",
    "probe_latency",
)

#: GWASPoker's predictions.
PREDICTION_COLUMNS: tuple[str, ...] = (
    "predicted_header_row_index",
    "predicted_header",
    "predicted_delimiter",
    "predicted_mapping",
    "predicted_prs_ready",
)

#: Externally curated labels. GWASPoker never writes these.
GROUND_TRUTH_COLUMNS: tuple[str, ...] = (
    "ground_truth_header_row_index",
    "ground_truth_header",
    "ground_truth_mapping",
    "ground_truth_prs_ready",
)

#: Optional downstream columns.
INTEGRATION_COLUMNS: tuple[str, ...] = (
    "gwaslab_detection",
    "gwaslab_success",
)

MANIFEST_COLUMNS: tuple[str, ...] = (
    *IDENTITY_COLUMNS,
    *API_COLUMNS,
    *FILE_COLUMNS,
    *PREDICTION_COLUMNS,
    *GROUND_TRUTH_COLUMNS,
    *INTEGRATION_COLUMNS,
    "failure_category",
    "notes",
)

#: Separator inside the ``*_header`` and ``*_mapping`` cells. Tab is used for
#: headers because it preserves a column name containing a comma or a pipe.
HEADER_SEPARATOR = "\t"
MAPPING_SEPARATOR = "|"
MAPPING_ASSIGNMENT = "="

#: Accepted values for the readiness columns.
VALID_VERDICTS = frozenset({"READY", "PARTIAL", "NOT_READY", "UNKNOWN"})


@dataclass
class ManifestRow:
    """One benchmark row, with typed accessors over the raw CSV cells."""

    data: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key, default)
        if value is None:
            return default
        text = str(value).strip()
        return default if text == "" or text.lower() in {"nan", "none"} else value

    @property
    def accession(self) -> Optional[str]:
        value = self.get("study_accession")
        return str(value).strip().upper() if value else None

    def header(self, column: str) -> Optional[tuple[str, ...]]:
        """Parse a header cell into an ordered tuple.

        Order is preserved deliberately -- ``exact_ordered_header_match`` is one
        of the metrics, and v1 destroyed order by using a ``set``.
        """
        value = self.get(column)
        if value is None:
            return None
        text = str(value)
        separator = HEADER_SEPARATOR if HEADER_SEPARATOR in text else None
        if separator is None:
            for candidate in (",", "|", " "):
                if candidate in text:
                    separator = candidate
                    break
        fields = text.split(separator) if separator else [text]
        return tuple(f.strip() for f in fields if f.strip())

    def mapping(self, column: str) -> Optional[dict[str, str]]:
        """Parse a ``raw=canonical|raw=canonical`` mapping cell."""
        value = self.get(column)
        if value is None:
            return None
        result: dict[str, str] = {}
        for item in str(value).split(MAPPING_SEPARATOR):
            if MAPPING_ASSIGNMENT not in item:
                continue
            raw, _, canonical = item.partition(MAPPING_ASSIGNMENT)
            if raw.strip():
                result[raw.strip()] = canonical.strip() or "unknown"
        return result or None

    def verdict(self, column: str) -> Optional[str]:
        value = self.get(column)
        if value is None:
            return None
        verdict = str(value).strip().upper().replace(" ", "_").replace("-", "_")
        if verdict in {"TRUE", "YES", "1"}:
            return "READY"
        if verdict in {"FALSE", "NO", "0"}:
            return "NOT_READY"
        return verdict if verdict in VALID_VERDICTS else None

    def number(self, column: str) -> Optional[float]:
        value = self.get(column)
        if value is None:
            return None
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            return None

    def integer(self, column: str) -> Optional[int]:
        value = self.number(column)
        return int(value) if value is not None else None

    def boolean(self, column: str) -> Optional[bool]:
        value = self.get(column)
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"true", "yes", "1", "y"}:
            return True
        if text in {"false", "no", "0", "n"}:
            return False
        return None

    @property
    def has_ground_truth(self) -> bool:
        return any(self.get(column) is not None for column in GROUND_TRUTH_COLUMNS)


def format_header(header: tuple[str, ...]) -> str:
    """Serialise a header for a manifest cell."""
    return HEADER_SEPARATOR.join(header)


def format_mapping(mapping: dict[str, str]) -> str:
    """Serialise a raw-to-canonical mapping for a manifest cell."""
    return MAPPING_SEPARATOR.join(
        f"{raw}{MAPPING_ASSIGNMENT}{canonical}" for raw, canonical in mapping.items()
    )


def read_manifest(path: Path) -> list[ManifestRow]:
    """Read a benchmark manifest CSV."""
    import pandas as pd

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Benchmark manifest not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    rows = [ManifestRow(dict(record)) for record in frame.to_dict(orient="records")]
    logger.info("Read %d manifest row(s) from %s", len(rows), path)
    return rows


def write_manifest(rows: list[ManifestRow], path: Path) -> Path:
    """Write manifest rows, keeping the canonical column order."""
    import pandas as pd

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(MANIFEST_COLUMNS)
    for row in rows:
        for key in row.data:
            if key not in columns:
                columns.append(key)
    frame = pd.DataFrame([row.data for row in rows], columns=columns)
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def validate_manifest(rows: list[ManifestRow]) -> list[str]:
    """Check a manifest before scoring, returning human-readable problems.

    The important check is the last one: it refuses a manifest in which the
    ground truth is identical to the prediction on every row, because that is
    the signature of ground truth having been machine-generated by the same
    parser that is being scored.
    """
    problems: list[str] = []

    if not rows:
        return ["The manifest is empty."]

    missing_accession = sum(1 for row in rows if not row.accession)
    if missing_accession:
        problems.append(f"{missing_accession} row(s) have no study_accession.")

    labelled = [row for row in rows if row.has_ground_truth]
    if not labelled:
        problems.append(
            "No row carries ground truth. Add ground_truth_header, "
            "ground_truth_mapping or ground_truth_prs_ready -- curated by hand, "
            "not generated by GWASPoker -- before scoring."
        )
        return problems

    for index, row in enumerate(rows):
        verdict = row.get("ground_truth_prs_ready")
        if verdict is not None and row.verdict("ground_truth_prs_ready") is None:
            problems.append(
                f"Row {index} ({row.accession}): ground_truth_prs_ready={verdict!r} is not "
                f"one of {sorted(VALID_VERDICTS)}."
            )

    comparable = [
        row
        for row in labelled
        if row.header("ground_truth_header") and row.header("predicted_header")
    ]
    if len(comparable) >= 3 and all(
        row.header("ground_truth_header") == row.header("predicted_header") for row in comparable
    ):
        problems.append(
            f"All {len(comparable)} labelled rows have ground_truth_header identical to "
            "predicted_header. If the labels were produced by GWASPoker itself the "
            "evaluation is circular and its metrics are meaningless. Curate the ground "
            "truth independently."
        )

    return problems


def blank_row(accession: str = "", trait: str = "") -> ManifestRow:
    """An empty manifest row with every column present."""
    data = dict.fromkeys(MANIFEST_COLUMNS, "")
    data["study_accession"] = accession
    data["trait"] = trait
    return ManifestRow(data)
