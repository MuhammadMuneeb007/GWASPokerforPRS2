"""Benchmark metric computation.

Pure functions over predictions and labels -- no network, no I/O -- so the
metric definitions can be unit-tested against worked examples.

None of these functions embeds a result. They compute from whatever a manifest
actually contains, and report ``None`` where there were no cases to compute
from, rather than a placeholder number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Optional


def _safe_divide(numerator: float, denominator: float) -> Optional[float]:
    """Divide, or return ``None`` when the denominator is zero.

    Returning ``None`` rather than ``0.0`` matters: "no positive cases, so recall
    is undefined" and "recall is zero" are different scientific statements.
    """
    return numerator / denominator if denominator else None


@dataclass
class ConfusionMatrix:
    """Binary confusion matrix with the derived rates."""

    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.true_negative + self.false_negative

    @property
    def precision(self) -> Optional[float]:
        return _safe_divide(self.true_positive, self.true_positive + self.false_positive)

    @property
    def recall(self) -> Optional[float]:
        """Also called sensitivity."""
        return _safe_divide(self.true_positive, self.true_positive + self.false_negative)

    @property
    def sensitivity(self) -> Optional[float]:
        return self.recall

    @property
    def specificity(self) -> Optional[float]:
        return _safe_divide(self.true_negative, self.true_negative + self.false_positive)

    @property
    def f1(self) -> Optional[float]:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or (precision + recall) == 0:
            return None
        return 2 * precision * recall / (precision + recall)

    @property
    def accuracy(self) -> Optional[float]:
        return _safe_divide(self.true_positive + self.true_negative, self.total)

    @property
    def false_positive_rate(self) -> Optional[float]:
        return _safe_divide(self.false_positive, self.false_positive + self.true_negative)

    @property
    def false_negative_rate(self) -> Optional[float]:
        return _safe_divide(self.false_negative, self.false_negative + self.true_positive)

    def add(self, predicted_positive: bool, actual_positive: bool) -> None:
        if predicted_positive and actual_positive:
            self.true_positive += 1
        elif predicted_positive and not actual_positive:
            self.false_positive += 1
        elif not predicted_positive and actual_positive:
            self.false_negative += 1
        else:
            self.true_negative += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
            "n": self.total,
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "sensitivity": _round(self.sensitivity),
            "specificity": _round(self.specificity),
            "f1": _round(self.f1),
            "accuracy": _round(self.accuracy),
            "false_positive_rate": _round(self.false_positive_rate),
            "false_negative_rate": _round(self.false_negative_rate),
        }


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return round(value, digits) if value is not None else None


@dataclass
class ColumnSetMetrics:
    """Micro-averaged column-level precision, recall and F1."""

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    per_file: list[dict[str, Any]] = field(default_factory=list)

    @property
    def precision(self) -> Optional[float]:
        return _safe_divide(self.true_positive, self.true_positive + self.false_positive)

    @property
    def recall(self) -> Optional[float]:
        return _safe_divide(self.true_positive, self.true_positive + self.false_negative)

    @property
    def f1(self) -> Optional[float]:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or (precision + recall) == 0:
            return None
        return 2 * precision * recall / (precision + recall)

    def add(self, predicted: Sequence[str], actual: Sequence[str], *, label: str = "") -> None:
        """Compare two column sets, counting duplicates correctly.

        Multiset semantics: a header with two columns named ``P`` and a truth
        with one is a false positive, which set arithmetic would hide.
        """
        from collections import Counter

        predicted_counts = Counter(predicted)
        actual_counts = Counter(actual)
        overlap = predicted_counts & actual_counts

        tp = sum(overlap.values())
        fp = sum((predicted_counts - overlap).values())
        fn = sum((actual_counts - overlap).values())

        self.true_positive += tp
        self.false_positive += fp
        self.false_negative += fn
        self.per_file.append(
            {
                "label": label,
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": _round(_safe_divide(tp, tp + fp)),
                "recall": _round(_safe_divide(tp, tp + fn)),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "f1": _round(self.f1),
            "files_compared": len(self.per_file),
        }


@dataclass
class TransferMetrics:
    """Bytes moved, and how much of the file that avoided."""

    probe_bytes: list[int] = field(default_factory=list)
    api_bytes: list[int] = field(default_factory=list)
    full_sizes: list[int] = field(default_factory=list)
    probe_latencies: list[float] = field(default_factory=list)
    api_latencies: list[float] = field(default_factory=list)

    def add(
        self,
        *,
        probe_bytes: Optional[int] = None,
        api_bytes: Optional[int] = None,
        full_size: Optional[int] = None,
        probe_latency: Optional[float] = None,
        api_latency: Optional[float] = None,
    ) -> None:
        if probe_bytes is not None:
            self.probe_bytes.append(int(probe_bytes))
        if api_bytes is not None:
            self.api_bytes.append(int(api_bytes))
        if full_size is not None:
            self.full_sizes.append(int(full_size))
        if probe_latency is not None:
            self.probe_latencies.append(float(probe_latency))
        if api_latency is not None:
            self.api_latencies.append(float(api_latency))

    def to_dict(self) -> dict[str, Any]:
        transferred = sum(self.probe_bytes) + sum(self.api_bytes)
        full_total = sum(self.full_sizes)
        return {
            "files": len(self.full_sizes),
            "total_bytes_transferred": transferred,
            "total_full_file_bytes": full_total,
            "percentage_transfer_reduction": (
                _round((1 - transferred / full_total) * 100, 6) if full_total else None
            ),
            "mean_probe_bytes": _round(_mean(self.probe_bytes), 1),
            "median_probe_bytes": _round(_median(self.probe_bytes), 1),
            "mean_api_bytes": _round(_mean(self.api_bytes), 1),
            "mean_probe_latency_seconds": _round(_mean(self.probe_latencies)),
            "median_probe_latency_seconds": _round(_median(self.probe_latencies)),
            "mean_api_latency_seconds": _round(_mean(self.api_latencies)),
        }


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


@dataclass
class RateCounter:
    """A simple ``k of n`` rate, reported as ``None`` when ``n`` is zero."""

    hits: int = 0
    total: int = 0

    def add(self, hit: bool) -> None:
        self.total += 1
        self.hits += 1 if hit else 0

    @property
    def rate(self) -> Optional[float]:
        return _safe_divide(self.hits, self.total)

    def to_dict(self) -> dict[str, Any]:
        return {"hits": self.hits, "n": self.total, "rate": _round(self.rate)}


def header_row_accuracy(
    predicted: Sequence[Optional[int]], actual: Sequence[Optional[int]]
) -> dict[str, Any]:
    """Fraction of files whose header *row index* was identified correctly."""
    counter = RateCounter()
    for pred, act in zip(predicted, actual):
        if act is None:
            continue
        counter.add(pred is not None and int(pred) == int(act))
    return counter.to_dict()


def exact_ordered_header_match(
    predicted: Sequence[Optional[tuple[str, ...]]],
    actual: Sequence[Optional[tuple[str, ...]]],
    *,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Fraction of files whose full header matched exactly, *in order*.

    The strictest header metric. Compared as tuples, never as sets: order is
    part of the header's meaning and part of what a positional reader depends
    on.
    """
    counter = RateCounter()
    for pred, act in zip(predicted, actual):
        if act is None:
            continue
        if pred is None:
            counter.add(False)
            continue
        left = pred if case_sensitive else tuple(c.casefold() for c in pred)
        right = act if case_sensitive else tuple(c.casefold() for c in act)
        counter.add(left == right)
    return counter.to_dict()


def mapping_accuracy(
    predicted: Sequence[Optional[dict[str, str]]],
    actual: Sequence[Optional[dict[str, str]]],
) -> dict[str, Any]:
    """Per-column canonical mapping accuracy, micro-averaged over all columns."""
    correct = 0
    total = 0
    unknown_predicted = 0
    per_concept: dict[str, dict[str, int]] = {}

    for pred, act in zip(predicted, actual):
        if not act:
            continue
        pred = pred or {}
        for raw, truth in act.items():
            total += 1
            guess = pred.get(raw, "unknown")
            bucket = per_concept.setdefault(truth, {"correct": 0, "total": 0})
            bucket["total"] += 1
            if guess == "unknown":
                unknown_predicted += 1
            if guess == truth:
                correct += 1
                bucket["correct"] += 1

    return {
        "columns_compared": total,
        "correct": correct,
        "accuracy": _round(_safe_divide(correct, total)),
        "predicted_unknown": unknown_predicted,
        "unknown_rate": _round(_safe_divide(unknown_predicted, total)),
        "per_concept": {
            concept: {
                "correct": stats["correct"],
                "n": stats["total"],
                "accuracy": _round(_safe_divide(stats["correct"], stats["total"])),
            }
            for concept, stats in sorted(per_concept.items())
        },
    }


def readiness_confusion(
    predicted: Sequence[Optional[str]],
    actual: Sequence[Optional[str]],
    *,
    positive_labels: frozenset[str] = frozenset({"READY"}),
) -> ConfusionMatrix:
    """Binary confusion matrix for PRS readiness.

    ``READY`` is the positive class by default. A false positive here is the
    costly error: GWASPoker said a file was usable, the user downloaded several
    gigabytes, and it was not.
    """
    matrix = ConfusionMatrix()
    for pred, act in zip(predicted, actual):
        if act is None:
            continue
        matrix.add(pred in positive_labels, act in positive_labels)
    return matrix


def multiclass_agreement(
    predicted: Sequence[Optional[str]], actual: Sequence[Optional[str]]
) -> dict[str, Any]:
    """Full READY/PARTIAL/NOT_READY agreement, with the confusion counts."""
    counter = RateCounter()
    confusion: dict[str, dict[str, int]] = {}
    for pred, act in zip(predicted, actual):
        if act is None:
            continue
        guess = pred or "UNKNOWN"
        counter.add(guess == act)
        confusion.setdefault(act, {}).setdefault(guess, 0)
        confusion[act][guess] += 1
    return {"agreement": counter.to_dict(), "confusion": confusion}
