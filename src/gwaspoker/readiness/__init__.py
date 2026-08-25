"""Formal PRS-readiness assessment."""

from gwaspoker.readiness.models import (
    ReadinessAssessment,
    ReadinessVerdict,
    RequirementResult,
    RequirementStatus,
)
from gwaspoker.readiness.prs import (
    PRS_RECOMMENDED,
    PRS_REQUIRED,
    assess_from_declared_fields,
    assess_from_mapping,
)

__all__ = [
    "PRS_RECOMMENDED",
    "PRS_REQUIRED",
    "ReadinessAssessment",
    "ReadinessVerdict",
    "RequirementResult",
    "RequirementStatus",
    "assess_from_declared_fields",
    "assess_from_mapping",
]
