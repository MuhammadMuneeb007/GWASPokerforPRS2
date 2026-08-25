"""Benchmark evaluation and the probe-size experiment.

Two entry points:

:func:`run_predictions`
    Executes GWASPoker over the manifest's studies and fills the *prediction*
    columns. It never touches the ground-truth columns.

:func:`evaluate_manifest`
    Scores predictions against externally curated labels, with optional
    stratification.

:func:`probe_size_experiment`
    Repeats the probe at 64 KB, 128 KB, 256 KB, 512 KB and 1 MB, recording
    whether the header was recovered at each size. This is what answers "how
    many bytes are actually needed?" -- 256 KB is GWASPoker's default, not a
    validated optimum, and the experiment exists to test it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gwaspoker.benchmark.manifest import (
    ManifestRow,
    format_header,
    format_mapping,
    validate_manifest,
)
from gwaspoker.benchmark.metrics import (
    ColumnSetMetrics,
    RateCounter,
    TransferMetrics,
    exact_ordered_header_match,
    header_row_accuracy,
    mapping_accuracy,
    multiclass_agreement,
    readiness_confusion,
)
from gwaspoker.config import PROBE_SIZE_LADDER, GWASPokerConfig, get_config
from gwaspoker.failures import GWASPokerError

logger = logging.getLogger(__name__)


@dataclass
class EvaluationReport:
    """Everything the benchmark computed, ready to serialise."""

    rows_total: int = 0
    rows_with_ground_truth: int = 0
    validation_problems: list[str] = field(default_factory=list)
    header_detection: dict[str, Any] = field(default_factory=dict)
    column_level: dict[str, Any] = field(default_factory=dict)
    canonical_mapping: dict[str, Any] = field(default_factory=dict)
    prs_readiness: dict[str, Any] = field(default_factory=dict)
    transfer: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    strata: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_total": self.rows_total,
            "rows_with_ground_truth": self.rows_with_ground_truth,
            "validation_problems": self.validation_problems,
            "header_detection": self.header_detection,
            "column_level": self.column_level,
            "canonical_mapping": self.canonical_mapping,
            "prs_readiness": self.prs_readiness,
            "transfer": self.transfer,
            "coverage": self.coverage,
            "failures": self.failures,
            "strata": self.strata,
        }


#: Stratification keys and how to derive each from a row.
STRATIFICATIONS: dict[str, Callable[[ManifestRow], Optional[str]]] = {
    "ssf_status": lambda row: (
        (str(row.get("ssf_status")).strip() or None) if row.get("ssf_status") else None
    ),
    "api_coverage": lambda row: (
        ("api_covered" if row.boolean("summary_statistics_api_available") else "api_uncovered")
        if row.get("summary_statistics_api_available") is not None
        else None
    ),
    "file_format": lambda row: (
        (str(row.get("file_format")).strip().upper() or None) if row.get("file_format") else None
    ),
    "compression": lambda row: (
        (str(row.get("compression")).strip().lower() or None) if row.get("compression") else None
    ),
    "source": lambda row: (str(row.get("source")).strip() or None) if row.get("source") else None,
}


def evaluate_manifest(
    rows: list[ManifestRow],
    *,
    stratify_by: Sequence[str] = (),
) -> EvaluationReport:
    """Score predictions against externally curated ground truth."""
    report = EvaluationReport(rows_total=len(rows))
    report.validation_problems = validate_manifest(rows)

    labelled = [row for row in rows if row.has_ground_truth]
    report.rows_with_ground_truth = len(labelled)

    report.header_detection = _header_metrics(labelled)
    report.column_level = _column_metrics(labelled)
    report.canonical_mapping = _mapping_metrics(labelled)
    report.prs_readiness = _readiness_metrics(labelled)
    report.transfer = _transfer_metrics(rows)
    report.coverage = _coverage_metrics(rows)
    report.failures = _failure_counts(rows)

    for key in stratify_by:
        deriver = STRATIFICATIONS.get(key)
        if deriver is None:
            logger.warning("Unknown stratification %r; skipping", key)
            continue
        buckets: dict[str, list[ManifestRow]] = {}
        for row in rows:
            value = deriver(row)
            if value:
                buckets.setdefault(value, []).append(row)
        report.strata[key] = {
            name: _stratum_summary(bucket) for name, bucket in sorted(buckets.items())
        }

    return report


def _stratum_summary(rows: list[ManifestRow]) -> dict[str, Any]:
    labelled = [row for row in rows if row.has_ground_truth]
    return {
        "n": len(rows),
        "n_with_ground_truth": len(labelled),
        "header_detection": _header_metrics(labelled),
        "prs_readiness": _readiness_metrics(labelled),
        "transfer": _transfer_metrics(rows),
        "coverage": _coverage_metrics(rows),
    }


def _header_metrics(rows: list[ManifestRow]) -> dict[str, Any]:
    predicted_index = [row.integer("predicted_header_row_index") for row in rows]
    actual_index = [row.integer("ground_truth_header_row_index") for row in rows]
    predicted_header = [row.header("predicted_header") for row in rows]
    actual_header = [row.header("ground_truth_header") for row in rows]

    detected = RateCounter()
    for header in predicted_header:
        detected.add(header is not None)

    return {
        "header_row_accuracy": header_row_accuracy(predicted_index, actual_index),
        "exact_ordered_header_match": exact_ordered_header_match(predicted_header, actual_header),
        "exact_ordered_header_match_case_sensitive": exact_ordered_header_match(
            predicted_header, actual_header, case_sensitive=True
        ),
        "header_detected_rate": detected.to_dict(),
    }


def _column_metrics(rows: list[ManifestRow]) -> dict[str, Any]:
    metrics = ColumnSetMetrics()
    for row in rows:
        actual = row.header("ground_truth_header")
        if actual is None:
            continue
        predicted = row.header("predicted_header") or ()
        metrics.add(
            [c.casefold() for c in predicted],
            [c.casefold() for c in actual],
            label=row.accession or "",
        )
    return metrics.to_dict()


def _mapping_metrics(rows: list[ManifestRow]) -> dict[str, Any]:
    predicted = [row.mapping("predicted_mapping") for row in rows]
    actual = [row.mapping("ground_truth_mapping") for row in rows]
    return mapping_accuracy(predicted, actual)


def _readiness_metrics(rows: list[ManifestRow]) -> dict[str, Any]:
    predicted = [row.verdict("predicted_prs_ready") for row in rows]
    actual = [row.verdict("ground_truth_prs_ready") for row in rows]
    matrix = readiness_confusion(predicted, actual)
    lenient = readiness_confusion(
        predicted, actual, positive_labels=frozenset({"READY", "PARTIAL"})
    )
    return {
        "strict_ready_positive": matrix.to_dict(),
        "lenient_ready_or_partial_positive": lenient.to_dict(),
        "multiclass": multiclass_agreement(predicted, actual),
    }


def _transfer_metrics(rows: list[ManifestRow]) -> dict[str, Any]:
    metrics = TransferMetrics()
    for row in rows:
        metrics.add(
            probe_bytes=row.integer("probe_bytes"),
            api_bytes=row.integer("api_bytes"),
            full_size=row.integer("full_file_size"),
            probe_latency=row.number("probe_latency"),
            api_latency=row.number("api_latency"),
        )
    return metrics.to_dict()


def _coverage_metrics(rows: list[ManifestRow]) -> dict[str, Any]:
    api_available = RateCounter()
    api_sufficient = RateCounter()
    failure = RateCounter()

    for row in rows:
        available = row.boolean("summary_statistics_api_available")
        if available is not None:
            api_available.add(available)
        sufficient = row.boolean("api_sufficient")
        if sufficient is not None:
            api_sufficient.add(sufficient)
        failure.add(bool(row.get("failure_category")))

    return {
        "api_coverage": api_available.to_dict(),
        "api_sufficiency_rate": api_sufficient.to_dict(),
        "failure_rate": failure.to_dict(),
    }


def _failure_counts(rows: list[ManifestRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        category = row.get("failure_category")
        if category:
            key = str(category).strip()
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


# ----------------------------------------------------------------------
# Running GWASPoker to fill predictions
# ----------------------------------------------------------------------


def run_predictions(
    rows: list[ManifestRow],
    *,
    config: Optional[GWASPokerConfig] = None,
    probe_bytes: Optional[int] = None,
    force_probe: bool = True,
    harmonised: str = "auto",
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[ManifestRow]:
    """Fill the prediction columns by running GWASPoker over each study.

    ``force_probe`` defaults to ``True`` here, unlike in ``assess``: the
    benchmark needs both routes measured on every study so the API-sufficient
    and probe branches can be compared on the same files.

    The ground-truth columns are never written.
    """
    from gwaspoker.catalog.discovery import DiscoveryService

    config = config or get_config()
    total = len(rows)

    with DiscoveryService(config) as service:
        for index, row in enumerate(rows):
            target = row.accession or row.get("remote_file_url")
            if not target:
                logger.warning("Row %d has neither an accession nor a URL; skipped", index)
                continue
            if progress is not None:
                progress(index + 1, total, str(target))

            try:
                result = service.assess(
                    str(target),
                    harmonised=harmonised,
                    force_probe=force_probe,
                    probe_bytes=probe_bytes,
                )
            except GWASPokerError as exc:
                row.data["failure_category"] = exc.category.value
                row.data["notes"] = str(exc)
                logger.warning("Assessment failed for %s: %s", target, exc)
                continue

            _apply_result_to_row(row, result)

    return rows


def _apply_result_to_row(row: ManifestRow, result: Any) -> None:
    """Copy an assessment into the prediction half of a manifest row."""
    study = result.study
    api = result.api_assessment
    resolved = result.resolved_file
    probe = result.probe
    readiness = result.readiness
    ssf = api.ssf_metadata if api else None

    data = row.data
    if study is not None:
        data.setdefault("trait", study.reported_trait or "")
        if not data.get("trait"):
            data["trait"] = study.reported_trait or ""
        if not data.get("publication_year"):
            data["publication_year"] = study.study_year or ""
        if not data.get("source"):
            data["source"] = "GWAS Catalog"
    if ssf is not None:
        data["ssf_status"] = ssf.ssf_status

    if api is not None:
        data["summary_statistics_api_available"] = str(api.available).lower()
        data["api_sufficient"] = str(api.sufficient_for_prs_assessment).lower()
        data["api_bytes"] = api.bytes_received
        data["api_latency"] = round(api.latency_seconds, 4)

    if resolved is not None:
        data["remote_file_url"] = resolved.url
        data["full_file_size"] = resolved.size_bytes or ""

    if probe is not None:
        data["file_format"] = probe.format_label
        data["compression"] = probe.compression.value
        data["probe_bytes"] = probe.transfer.received_bytes
        data["probe_latency"] = round(probe.transfer.transfer_time_seconds, 4)
        if probe.header is not None:
            data["predicted_header_row_index"] = probe.header.header_row_index
            data["predicted_header"] = format_header(probe.header.raw_header)
            data["predicted_delimiter"] = repr(probe.header.delimiter)
        if probe.mapping is not None:
            data["predicted_mapping"] = format_mapping(
                {c.raw_name: c.canonical_name for c in probe.mapping.columns}
            )

    if readiness is not None:
        data["predicted_prs_ready"] = readiness.verdict.value

    if result.failure_category is not None:
        data["failure_category"] = result.failure_category.value
    if result.error:
        data["notes"] = result.error


# ----------------------------------------------------------------------
# Probe-size experiment
# ----------------------------------------------------------------------


@dataclass
class ProbeSizeOutcome:
    """One (file, probe size) trial."""

    target: str
    probe_bytes: int
    received_bytes: int
    header_detected: bool
    header_row_index: Optional[int] = None
    header: Optional[tuple[str, ...]] = None
    confidence: Optional[float] = None
    latency_seconds: float = 0.0
    error: Optional[str] = None
    failure_category: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "probe_bytes_requested": self.probe_bytes,
            "probe_bytes_received": self.received_bytes,
            "header_detected": self.header_detected,
            "header_row_index": self.header_row_index,
            "header": list(self.header) if self.header else None,
            "header_confidence": round(self.confidence, 3) if self.confidence else None,
            "latency_seconds": round(self.latency_seconds, 4),
            "error": self.error,
            "failure_category": self.failure_category,
        }


def probe_size_experiment(
    targets: Sequence[str],
    *,
    sizes: Sequence[int] = PROBE_SIZE_LADDER,
    config: Optional[GWASPokerConfig] = None,
    harmonised: str = "auto",
    progress: Optional[Callable[[str, int], None]] = None,
) -> list[ProbeSizeOutcome]:
    """Probe each target at every size in ``sizes``.

    Answers: how many bytes does reliable header detection actually need? A
    result at the smallest size that agrees with the largest is evidence that
    the default can be lowered; disagreement is evidence that it cannot.
    """
    from gwaspoker.catalog.discovery import DiscoveryService

    config = config or get_config()
    outcomes: list[ProbeSizeOutcome] = []

    with DiscoveryService(config) as service:
        for target in targets:
            for size in sizes:
                if progress is not None:
                    progress(target, size)
                outcome = ProbeSizeOutcome(
                    target=target, probe_bytes=size, received_bytes=0, header_detected=False
                )
                try:
                    probe, _, _ = service.probe(target, harmonised=harmonised, probe_bytes=size)
                except GWASPokerError as exc:
                    outcome.error = str(exc)
                    outcome.failure_category = exc.category.value
                    outcomes.append(outcome)
                    continue

                outcome.received_bytes = probe.transfer.received_bytes
                outcome.latency_seconds = probe.transfer.transfer_time_seconds
                if probe.header is not None:
                    outcome.header_detected = True
                    outcome.header_row_index = probe.header.header_row_index
                    outcome.header = probe.header.raw_header
                    outcome.confidence = probe.header.confidence
                else:
                    outcome.error = probe.error
                    outcome.failure_category = (
                        probe.failure_category.value if probe.failure_category else None
                    )
                outcomes.append(outcome)

    return outcomes


def summarize_probe_sizes(outcomes: list[ProbeSizeOutcome]) -> dict[str, Any]:
    """Per-size detection rate, plus agreement with the largest size probed.

    Agreement is measured against each file's own largest successful probe,
    which is the best available proxy for the true header without hand-curating
    one for every file.
    """
    by_size: dict[int, RateCounter] = {}
    for outcome in outcomes:
        by_size.setdefault(outcome.probe_bytes, RateCounter()).add(outcome.header_detected)

    reference: dict[str, tuple[str, ...]] = {}
    for outcome in sorted(outcomes, key=lambda o: o.probe_bytes):
        if outcome.header is not None:
            reference[outcome.target] = outcome.header

    agreement: dict[int, RateCounter] = {}
    for outcome in outcomes:
        truth = reference.get(outcome.target)
        if truth is None:
            continue
        agreement.setdefault(outcome.probe_bytes, RateCounter()).add(outcome.header == truth)

    return {
        "targets": len({o.target for o in outcomes}),
        "sizes": sorted(by_size),
        "detection_rate_by_size": {
            str(size): counter.to_dict() for size, counter in sorted(by_size.items())
        },
        "agreement_with_largest_probe_by_size": {
            str(size): counter.to_dict() for size, counter in sorted(agreement.items())
        },
        "mean_bytes_by_size": {
            str(size): round(
                sum(o.received_bytes for o in outcomes if o.probe_bytes == size)
                / max(1, sum(1 for o in outcomes if o.probe_bytes == size)),
                1,
            )
            for size in sorted(by_size)
        },
        "note": (
            "Agreement is measured against each file's own largest successful probe, "
            "not against externally curated ground truth. Use the manifest's "
            "ground_truth_header columns for a non-circular header accuracy figure."
        ),
    }
