"""PRS-readiness rules.

A polygenic risk score is a weighted sum of allele dosages. To compute one from
summary statistics you must be able to answer, for every variant:

1. **Which variant is this?** -- to align it against a genotype file or LD panel.
   Either a variant identifier, or a chromosome *and* a position.
2. **Which allele carries the effect?** -- the effect allele. Without it the sign
   of every weight is undefined.
3. **What is the other allele?** -- needed to resolve strand ambiguity and to
   distinguish multi-allelic variants at the same locus.
4. **How large is the effect?** -- beta, or an odds/hazard ratio (which must be
   log-transformed), or a z-score (usable only with frequency and N).
5. **How certain is it?** -- a p-value, for the thresholding and clumping that
   nearly every PRS method performs.

Those five are *required*. Standard error, sample size, allele frequency and
INFO are *recommended*: their absence rules out particular methods (LDpred2
needs SE and N; frequency filtering needs EAF) but not PRS in general.

The rules are stated once, here, as data. ``docs/MAPPING_SCHEMA.md`` documents
them for readers who are not going to open the source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from gwaspoker.mapping.mapper import MappingResult
from gwaspoker.readiness.models import (
    ReadinessAssessment,
    ReadinessVerdict,
    RequirementResult,
    RequirementStatus,
)

logger = logging.getLogger(__name__)

#: A mapping confidence at or above this counts as satisfied; below it, and
#: above :data:`UNCERTAIN_THRESHOLD`, counts as uncertain.
SATISFIED_THRESHOLD = 0.9
UNCERTAIN_THRESHOLD = 0.5


@dataclass(frozen=True)
class Requirement:
    """One requirement, expressed over canonical concept names."""

    key: str
    label: str
    #: Any one of these concepts satisfies the requirement.
    any_of: tuple[str, ...] = ()
    #: All of these concepts, together, satisfy it (used for chromosome+position).
    all_of: tuple[str, ...] = ()
    #: An alternative all_of group; the requirement is met if either group is.
    alternative_all_of: tuple[str, ...] = ()
    note: Optional[str] = None


#: --- Required for PRS -------------------------------------------------------

PRS_REQUIRED: tuple[Requirement, ...] = (
    Requirement(
        key="variant_identification",
        label="variant identification",
        any_of=("variant_id", "chromosome_position"),
        all_of=("chromosome", "position"),
        note="an rsID or a chromosome/position pair; either is sufficient",
    ),
    Requirement(
        key="effect_allele",
        label="effect allele",
        any_of=("effect_allele",),
        note="without it the direction of every weight is undefined",
    ),
    Requirement(
        key="other_allele",
        label="other allele",
        any_of=("other_allele",),
        note="needed to resolve strand ambiguity and multi-allelic sites",
    ),
    Requirement(
        key="effect_size",
        label="effect size",
        any_of=("beta", "odds_ratio", "hazard_ratio", "z_score"),
        note="beta directly; odds/hazard ratios need a log transform; a z-score "
        "additionally needs allele frequency and sample size",
    ),
    Requirement(
        key="significance",
        label="p-value",
        any_of=("p_value", "neg_log10_p_value"),
        note="required for the p-value thresholding and clumping most PRS methods use",
    ),
)

#: --- Recommended ------------------------------------------------------------

PRS_RECOMMENDED: tuple[Requirement, ...] = (
    Requirement(
        key="standard_error",
        label="standard error",
        any_of=("standard_error",),
        note="required by LDpred2, PRS-CS and other Bayesian methods",
    ),
    Requirement(
        key="sample_size",
        label="sample size",
        any_of=("sample_size", "cases", "controls"),
        note="per-variant N; some methods accept a study-level N instead",
    ),
    Requirement(
        key="allele_frequency",
        label="allele frequency",
        any_of=("effect_allele_frequency", "minor_allele_frequency", "allele_frequency"),
        note="needed for frequency filtering and for z-score to beta conversion",
    ),
    Requirement(
        key="imputation_quality",
        label="imputation quality (INFO)",
        any_of=("info_score",),
        note="lets poorly imputed variants be excluded",
    ),
)

#: Targets other than PRS can be added here without touching the evaluator.
TARGETS: dict[str, tuple[tuple[Requirement, ...], tuple[Requirement, ...]]] = {
    "prs": (PRS_REQUIRED, PRS_RECOMMENDED),
}


def _evaluate_requirement(
    requirement: Requirement,
    lookup: Callable[[str], Optional[tuple[str, float]]],
) -> RequirementResult:
    """Check one requirement against the available concepts."""

    def check_group(concepts: tuple[str, ...]) -> Optional[tuple[list[str], list[str], float]]:
        """All concepts present? Return (columns, concepts, min confidence)."""
        if not concepts:
            return None
        columns: list[str] = []
        found: list[str] = []
        confidence = 1.0
        for concept in concepts:
            hit = lookup(concept)
            if hit is None:
                return None
            columns.append(hit[0])
            found.append(concept)
            confidence = min(confidence, hit[1])
        return columns, found, confidence

    best: Optional[tuple[list[str], list[str], float]] = None

    for concept in requirement.any_of:
        hit = lookup(concept)
        if hit is not None and (best is None or hit[1] > best[2]):
            best = ([hit[0]], [concept], hit[1])

    for group in (requirement.all_of, requirement.alternative_all_of):
        result = check_group(group)
        if result is not None and (best is None or result[2] > best[2]):
            best = result

    if best is None:
        return RequirementResult(
            key=requirement.key,
            label=requirement.label,
            status=RequirementStatus.MISSING,
            note=requirement.note,
        )

    columns, concepts, confidence = best
    if confidence >= SATISFIED_THRESHOLD:
        status = RequirementStatus.SATISFIED
    elif confidence >= UNCERTAIN_THRESHOLD:
        status = RequirementStatus.UNCERTAIN
    else:
        status = RequirementStatus.MISSING

    return RequirementResult(
        key=requirement.key,
        label=requirement.label,
        status=status,
        satisfied_by=tuple(columns),
        canonical_concepts=tuple(concepts),
        confidence=confidence,
        note=requirement.note,
    )


def assess_from_mapping(
    mapping: MappingResult,
    *,
    target: str = "prs",
    evidence_source: str = "file_probe",
    header: tuple[str, ...] = (),
) -> ReadinessAssessment:
    """Assess readiness from a mapped header."""
    required_rules, recommended_rules = TARGETS.get(target, TARGETS["prs"])

    by_concept = mapping.by_concept()

    def lookup(concept: str) -> Optional[tuple[str, float]]:
        columns = by_concept.get(concept)
        if not columns:
            return None
        best = max(columns, key=lambda c: c.confidence)
        return best.raw_name, best.confidence

    required = tuple(_evaluate_requirement(r, lookup) for r in required_rules)
    recommended = tuple(_evaluate_requirement(r, lookup) for r in recommended_rules)

    return _finalize(
        target=target,
        required=required,
        recommended=recommended,
        evidence_source=evidence_source,
        header=header or tuple(c.raw_name for c in mapping.columns),
        unmapped=tuple(c.raw_name for c in mapping.unresolved),
        extra_warnings=_mapping_warnings(mapping, by_concept),
    )


def assess_from_declared_fields(
    fields: tuple[str, ...],
    *,
    target: str = "prs",
    evidence_source: str = "gwas_ssf_metadata",
    note: Optional[str] = None,
) -> ReadinessAssessment:
    """Assess readiness from a *declared* field list rather than an observed one.

    Used for the API-sufficient branch: a file declaring ``GWAS-SSF v1.0``
    guarantees a mandatory column set, so those field names are mapped and
    assessed exactly as an observed header would be. The verdict is identical in
    form, but ``evidence_source`` records that no data bytes were read.
    """
    from gwaspoker.mapping.mapper import get_mapper

    mapping = get_mapper().map_header(fields)
    assessment = assess_from_mapping(
        mapping, target=target, evidence_source=evidence_source, header=fields
    )
    if note:
        assessment.notes = (*assessment.notes, note)
    return assessment


def _mapping_warnings(mapping: MappingResult, by_concept: dict) -> tuple[str, ...]:
    """Scientific caveats that a verdict alone does not convey."""
    warnings: list[str] = []

    if "odds_ratio" in by_concept and "beta" not in by_concept:
        warnings.append(
            "Effect sizes are odds ratios; take the natural log before using them as "
            "PRS weights."
        )
    if "hazard_ratio" in by_concept and "beta" not in by_concept:
        warnings.append(
            "Effect sizes are hazard ratios; take the natural log before using them as "
            "PRS weights."
        )
    if "z_score" in by_concept and not ({"beta", "odds_ratio", "hazard_ratio"} & set(by_concept)):
        warnings.append(
            "The only effect measure is a z-score. Converting it to a beta additionally "
            "requires allele frequency and per-variant sample size."
        )
    if "neg_log10_p_value" in by_concept and "p_value" not in by_concept:
        warnings.append(
            "Significance is reported as -log10(p), not p. Transform it before applying "
            "a p-value threshold."
        )
    if "minor_allele_frequency" in by_concept and "effect_allele_frequency" not in by_concept:
        warnings.append(
            "Only a minor allele frequency is available. Which allele is minor is "
            "population-dependent, so it is not interchangeable with the effect allele "
            "frequency."
        )
    if "chromosome_position" in by_concept and not ({"chromosome", "position"} <= set(by_concept)):
        warnings.append(
            "Locus is a combined chromosome:position field and must be split before most "
            "PRS tools can read it."
        )
    if mapping.duplicates:
        warnings.append("Duplicate column names in the header: " + ", ".join(mapping.duplicates))

    # Several distinct columns can legitimately map to one concept -- BOLT-LMM
    # output carries P_LINREG, P_BOLT_LMM_INF and p_value side by side. The
    # highest-confidence column is used, but which p-value is the intended one
    # is the analyst's decision, so the alternatives are named rather than
    # silently dropped.
    for concept in ("p_value", "beta", "odds_ratio", "standard_error", "sample_size"):
        columns = by_concept.get(concept, [])
        if len(columns) > 1:
            chosen = max(columns, key=lambda c: c.confidence)
            others = [c.raw_name for c in columns if c.raw_name != chosen.raw_name]
            warnings.append(
                f"{len(columns)} columns map to {concept}: "
                f"{', '.join(c.raw_name for c in columns)}. "
                f"{chosen.raw_name!r} was used; confirm that is the intended one "
                f"rather than {', '.join(repr(o) for o in others)}."
            )
    uncertain = [c for c in mapping.columns if 0 < c.confidence < SATISFIED_THRESHOLD]
    if uncertain:
        warnings.append(
            f"{len(uncertain)} column(s) mapped by heuristic rather than by a curated "
            "alias: " + ", ".join(f"{c.raw_name}->{c.canonical_name}" for c in uncertain[:5])
        )
    return tuple(warnings)


def _finalize(
    *,
    target: str,
    required: tuple[RequirementResult, ...],
    recommended: tuple[RequirementResult, ...],
    evidence_source: str,
    header: tuple[str, ...],
    unmapped: tuple[str, ...],
    extra_warnings: tuple[str, ...],
) -> ReadinessAssessment:
    """Turn requirement results into a verdict and a plain-language decision."""
    missing = [r for r in required if r.status is RequirementStatus.MISSING]
    uncertain = [r for r in required if r.status is RequirementStatus.UNCERTAIN]

    if not required:
        verdict = ReadinessVerdict.UNKNOWN
    elif not missing and not uncertain:
        verdict = ReadinessVerdict.READY
    elif not missing:
        verdict = ReadinessVerdict.PARTIAL
    elif len(missing) < len(required):
        verdict = ReadinessVerdict.PARTIAL if len(missing) <= 2 else ReadinessVerdict.NOT_READY
    else:
        verdict = ReadinessVerdict.NOT_READY

    if verdict is ReadinessVerdict.READY:
        weak = [r for r in recommended if not r.is_satisfied]
        decision = "Suitable for downstream PRS preparation."
        if weak:
            decision += (
                " Note that "
                + ", ".join(r.label for r in weak)
                + " "
                + ("is" if len(weak) == 1 else "are")
                + " absent, which rules out methods that need "
                + ("it" if len(weak) == 1 else "them")
                + "."
            )
    elif verdict is ReadinessVerdict.PARTIAL:
        problems = [r.label for r in missing] + [f"{r.label} (uncertain)" for r in uncertain]
        decision = (
            "Usable for PRS only after resolving: " + ", ".join(problems) + ". "
            "Check the file's own documentation, or run a full download and inspect it."
        )
    elif verdict is ReadinessVerdict.NOT_READY:
        decision = (
            "Not usable for PRS as published: "
            + ", ".join(r.label for r in missing)
            + " could not be identified."
        )
    else:
        decision = "Insufficient information to reach a verdict."

    confidences = [r.confidence for r in required if r.confidence > 0]
    confidence = min(confidences) if confidences else 0.0
    if verdict is ReadinessVerdict.NOT_READY and not confidences:
        confidence = 0.0

    return ReadinessAssessment(
        target=target,
        verdict=verdict,
        required=required,
        recommended=recommended,
        evidence_source=evidence_source,
        decision=decision,
        warnings=extra_warnings,
        header=header,
        unmapped_columns=unmapped,
        confidence=confidence,
    )
