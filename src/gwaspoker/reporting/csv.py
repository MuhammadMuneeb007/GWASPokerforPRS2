"""CSV reports.

These are the successors to v1's ``Output-Module1-*.csv`` and the PRS column
listings, with the same information plus explicit provenance columns.

Column order is fixed and documented so that a downstream script can rely on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from gwaspoker.metadata.ancestry import summarize_ancestries

#: Columns of ``search_results.csv``. Extends v1's Module 1 output with the
#: provenance of every extracted count.
SEARCH_COLUMNS: tuple[str, ...] = (
    "study_accession",
    "reported_trait",
    "mapped_trait",
    "efo_ids",
    "population",
    "sample_size",
    "sample_size_source",
    "cases",
    "cases_source",
    "controls",
    "controls_source",
    "file_available",
    "metadata_available",
    "harmonised_available",
    "ssf_status",
    "prs_from_metadata",
    "probe_needed",
    "file_check_category",
    "summary_statistics_available",
    "summary_statistics_location",
    "resolved_file_name",
    "resolved_file_size",
    "initial_sample_description",
    "replication_sample_description",
    "genome_build",
    "study_year",
    "pubmed_id",
    "first_author",
    "journal",
    "publication_title",
    "association_count",
    "api_source",
    "ancestry_match_score",
)

#: Columns of ``prs_assessment.csv``.
ASSESSMENT_COLUMNS: tuple[str, ...] = (
    "study_accession",
    "reported_trait",
    "ssf_status",
    "api_available",
    "api_sufficient",
    "api_bytes",
    "api_latency_seconds",
    "remote_file_name",
    "remote_file_url",
    "remote_file_size",
    "harmonised",
    "file_selection_reason",
    "probe_required",
    "probe_performed",
    "probe_limit_bytes",
    "probe_bytes_transferred",
    "probe_transfer_reduction",
    "file_format",
    "compression",
    "detected_encoding",
    "detected_delimiter",
    "detected_header_row_index",
    "detected_header",
    "header_confidence",
    "prs_verdict",
    "prs_confidence",
    "readiness_evidence_source",
    "missing_required",
    "unmapped_columns",
    "total_bytes_transferred",
    "elapsed_seconds",
    "error",
    "failure_category",
)

#: Columns of the PRS column-mapping export (successor to Module 5's output).
MAPPING_COLUMNS: tuple[str, ...] = (
    "source",
    "column_index",
    "raw_name",
    "normalized_name",
    "canonical_name",
    "prs_tool_symbol",
    "mapping_method",
    "confidence",
    "note",
)


def _write(rows: list[dict[str, Any]], columns: tuple[str, ...], path: Path) -> Path:
    import pandas as pd

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=list(columns))
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


def search_rows(results: Iterable[Any]) -> list[dict[str, Any]]:
    """Build the ``search_results.csv`` rows without writing anything.

    Split out from :func:`write_search_csv` so the CLI can render CSV to stdout
    without a temporary file.
    """
    rows: list[dict[str, Any]] = []
    for result in results:
        study = result.study
        samples = study.samples
        rows.append(
            {
                "study_accession": study.study_accession,
                "reported_trait": study.reported_trait,
                "mapped_trait": study.mapped_trait_label if study.mapped_traits else None,
                "efo_ids": "|".join(t.efo_id for t in study.mapped_traits if t.efo_id) or None,
                "population": summarize_ancestries(
                    [g for a in study.ancestries for g in a.ancestral_groups]
                ),
                "sample_size": samples.total,
                "sample_size_source": samples.total_source.value,
                "cases": samples.cases,
                "cases_source": samples.cases_source.value,
                "controls": samples.controls,
                "controls_source": samples.controls_source.value,
                "file_available": result.file_available,
                "metadata_available": result.metadata_available,
                "harmonised_available": result.harmonised_available,
                "ssf_status": result.ssf_status,
                "prs_from_metadata": result.prs_from_metadata,
                "probe_needed": result.probe_needed,
                "file_check_category": result.file_check_category,
                "summary_statistics_available": study.summary_statistics_available,
                "summary_statistics_location": study.summary_statistics_location,
                "resolved_file_name": (result.resolved_file.name if result.resolved_file else None),
                "resolved_file_size": (
                    result.resolved_file.size_bytes if result.resolved_file else None
                ),
                "initial_sample_description": study.initial_sample_description,
                "replication_sample_description": study.replication_sample_description,
                "genome_build": study.genome_build,
                "study_year": study.study_year,
                "pubmed_id": study.pubmed_id,
                "first_author": study.first_author,
                "journal": study.publication_journal,
                "publication_title": study.publication_title,
                "association_count": study.association_count,
                "api_source": study.api_source,
                "ancestry_match_score": (
                    round(result.ancestry_match.score, 3) if result.ancestry_match else None
                ),
            }
        )
    return rows


def write_search_csv(results: Iterable[Any], path: Path) -> Path:
    """Write ``search_results.csv``."""
    return _write(search_rows(results), SEARCH_COLUMNS, path)


def render_search_csv(results: Iterable[Any]) -> str:
    """Render ``search_results.csv`` content as a string."""
    import io

    import pandas as pd

    buffer = io.StringIO()
    pd.DataFrame(search_rows(results), columns=list(SEARCH_COLUMNS)).to_csv(buffer, index=False)
    return buffer.getvalue()


def write_assessment_csv(results: Iterable[Any], path: Path) -> Path:
    """Write ``prs_assessment.csv``."""
    from gwaspoker.provenance import assessment_provenance

    rows: list[dict[str, Any]] = []
    for result in results:
        row = assessment_provenance(result)
        readiness = result.readiness
        row["missing_required"] = "|".join(readiness.missing_required) if readiness else None
        row["unmapped_columns"] = "|".join(readiness.unmapped_columns) if readiness else None
        header = row.get("detected_header")
        if isinstance(header, list):
            row["detected_header"] = "\t".join(header)
        rows.append(row)
    return _write(rows, ASSESSMENT_COLUMNS, path)


def write_mapping_csv(mapping: Any, path: Path, *, source: Optional[str] = None) -> Path:
    """Write the canonical column mapping for one file."""
    rows = [
        {
            "source": source,
            "column_index": index,
            "raw_name": column.raw_name,
            "normalized_name": column.normalized_name,
            "canonical_name": column.canonical_name,
            "prs_tool_symbol": column.prs_tool_symbol,
            "mapping_method": column.mapping_method,
            "confidence": round(column.confidence, 3),
            "note": column.note,
        }
        for index, column in enumerate(mapping.columns)
    ]
    return _write(rows, MAPPING_COLUMNS, path)


def write_rows_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    """Write arbitrary rows, using the union of their keys as columns."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return _write(rows, tuple(columns), path)
