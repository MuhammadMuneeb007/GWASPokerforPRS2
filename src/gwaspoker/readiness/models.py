"""Data model for PRS-readiness assessment."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ReadinessVerdict(str, Enum):
    """Overall verdict for a target workflow."""

    READY = "READY"
    PARTIAL = "PARTIAL"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


class RequirementStatus(str, Enum):
    """Status of one requirement.

    ``UNCERTAIN`` exists because a column mapped by a Layer 3 heuristic is not
    the same evidence as a column named ``p_value``, and the difference must be
    visible to the reader.
    """

    SATISFIED = "satisfied"
    UNCERTAIN = "uncertain"
    MISSING = "missing"

    @property
    def symbol(self) -> str:
        return {"satisfied": "✓", "uncertain": "?", "missing": "✗"}[self.value]


@dataclass
class RequirementResult:
    """Whether one requirement is met, and by which column."""

    key: str
    label: str
    status: RequirementStatus
    satisfied_by: tuple[str, ...] = ()
    canonical_concepts: tuple[str, ...] = ()
    confidence: float = 0.0
    note: Optional[str] = None
    #: Confidence of the header-derived mapping alone, before value evidence.
    #: Kept alongside `confidence` so the two can be reported separately.
    header_confidence: Optional[float] = None
    #: Value-domain status of the columns satisfying this requirement:
    #: PASS / WARN / FAIL / NOT_TESTED, or None when no validation ran.
    value_status: Optional[str] = None

    @property
    def is_satisfied(self) -> bool:
        return self.status is RequirementStatus.SATISFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status.value,
            "satisfied_by": list(self.satisfied_by),
            "canonical_concepts": list(self.canonical_concepts),
            "confidence": round(self.confidence, 3),
            "header_confidence": (
                round(self.header_confidence, 3) if self.header_confidence is not None else None
            ),
            "value_status": self.value_status,
            "note": self.note,
        }


@dataclass
class ReadinessAssessment:
    """The full readiness result for one file."""

    target: str
    verdict: ReadinessVerdict
    required: tuple[RequirementResult, ...] = ()
    recommended: tuple[RequirementResult, ...] = ()
    evidence_source: str = "unknown"
    decision: str = ""
    warnings: tuple[str, ...] = ()
    header: tuple[str, ...] = ()
    unmapped_columns: tuple[str, ...] = ()
    confidence: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required_satisfied(self) -> int:
        return sum(1 for r in self.required if r.is_satisfied)

    @property
    def recommended_satisfied(self) -> int:
        return sum(1 for r in self.recommended if r.is_satisfied)

    @property
    def missing_required(self) -> tuple[str, ...]:
        return tuple(r.label for r in self.required if r.status is RequirementStatus.MISSING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "verdict": self.verdict.value,
            "evidence_source": self.evidence_source,
            "confidence": round(self.confidence, 3),
            "required": [r.to_dict() for r in self.required],
            "recommended": [r.to_dict() for r in self.recommended],
            "required_satisfied": f"{self.required_satisfied}/{len(self.required)}",
            "recommended_satisfied": f"{self.recommended_satisfied}/{len(self.recommended)}",
            "missing_required": list(self.missing_required),
            "decision": self.decision,
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "header": list(self.header),
            "unmapped_columns": list(self.unmapped_columns),
        }
