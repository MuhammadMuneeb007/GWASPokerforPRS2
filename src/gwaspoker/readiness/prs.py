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
from dataclasses import dataclass, replace
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
class ConditionalOption:
    """A concept that satisfies a requirement only with supporting companions.

    ``any_of_companions`` is a list of alternative groups: at least one group
    must be fully present. This exists so the z-score rule can be stated as
    data rather than as a special case threaded through the evaluator.
    """

    concept: str
    requires: tuple[str, ...] = ()
    any_of_companions: tuple[tuple[str, ...], ...] = ()
    unmet_note: str = ""


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
    #: Concepts that satisfy the requirement only when the companions listed
    #: alongside them are also present. Used for the z-score: a Z on its own
    #: is not an effect size, it is a test statistic.
    conditional: tuple[ConditionalOption, ...] = ()
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
        # Beta is usable directly. OR and HR are usable after a log transform,
        # which is a deterministic per-row operation needing nothing else.
        any_of=("beta", "odds_ratio", "hazard_ratio"),
        # A z-score is NOT an effect size. It is a test statistic: beta / se.
        # Recovering an effect from it needs, at minimum, the per-variant sample
        # size and an allele frequency
        #     se ~= 1 / sqrt(2 * N * f * (1 - f))
        #     beta ~= Z * se
        # Without N and a frequency the Z column cannot yield PRS weights at
        # all, so it must not satisfy this requirement on its own.
        conditional=(
            ConditionalOption(
                concept="z_score",
                requires=("sample_size",),
                any_of_companions=(
                    ("effect_allele_frequency",),
                    ("minor_allele_frequency",),
                    ("allele_frequency",),
                ),
                unmet_note=(
                    "a z-score is a test statistic, not an effect size. Converting it "
                    "to a beta needs the per-variant sample size and an allele "
                    "frequency, which this file does not provide"
                ),
            ),
        ),
        note="beta directly; odds/hazard ratios need a log transform; a z-score "
        "only with sample size and an allele frequency",
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
        # A case count is not a sample size, and neither is a control count.
        # Either total N is stated, or both arms are, in which case the total is
        # derivable as their sum -- and the assessment says it was derived.
        any_of=("sample_size",),
        all_of=("cases", "controls"),
        note="total N directly, or cases and controls together; a case count "
        "alone is not a sample size",
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

    # Concepts that only count when their companions are present.
    unmet_conditional: Optional[str] = None
    for option in requirement.conditional:
        hit = lookup(option.concept)
        if hit is None:
            continue
        required = check_group(option.requires) if option.requires else ([], [], 1.0)
        companion = None
        if option.any_of_companions:
            for group in option.any_of_companions:
                companion = check_group(group)
                if companion is not None:
                    break
        else:
            companion = ([], [], 1.0)

        if required is None or companion is None:
            # The concept is present but unusable. Record why, and do not let
            # it satisfy the requirement.
            unmet_conditional = option.unmet_note or (
                f"{option.concept} is present but lacks the companions needed to use it"
            )
            continue

        columns = [hit[0], *required[0], *companion[0]]
        concepts = [option.concept, *required[1], *companion[1]]
        confidence = min(hit[1], required[2], companion[2])
        if best is None or confidence > best[2]:
            best = (columns, concepts, confidence)

    if best is None:
        return RequirementResult(
            key=requirement.key,
            label=requirement.label,
            status=RequirementStatus.MISSING,
            note=unmet_conditional or requirement.note,
        )

    columns, concepts, confidence = best
    if confidence >= SATISFIED_THRESHOLD:
        status = RequirementStatus.SATISFIED
    elif confidence >= UNCERTAIN_THRESHOLD:
        status = RequirementStatus.UNCERTAIN
    else:
        status = RequirementStatus.MISSING

    note = requirement.note
    if requirement.key == "sample_size" and set(concepts) == {"cases", "controls"}:
        note = (
            "total N was DERIVED as cases + controls; it is not stated directly in "
            "the file. " + (requirement.note or "")
        ).strip()

    return RequirementResult(
        key=requirement.key,
        label=requirement.label,
        status=status,
        satisfied_by=tuple(columns),
        canonical_concepts=tuple(concepts),
        confidence=confidence,
        note=note,
    )


def assess_from_mapping(
    mapping: MappingResult,
    *,
    target: str = "prs",
    evidence_source: str = "file_probe",
    header: tuple[str, ...] = (),
    validation=None,
) -> ReadinessAssessment:
    """Assess readiness from a mapped header, optionally with value evidence.

    ``validation`` is a
    :class:`~gwaspoker.validation.values.ValueValidationResult`. When supplied,
    a column whose sampled values contradict its header mapping cannot count as
    confidently satisfied:

    ===================================  ====================================
    Header mapping + value evidence      Outcome
    ===================================  ====================================
    strong mapping + PASS                satisfied
    strong mapping + WARN                satisfied, with the warning surfaced
    strong mapping + FAIL                downgraded, not confidently satisfied
    ambiguous/heuristic + WARN or FAIL   uncertain
    unknown mapping                      stays unknown; never forced
    ===================================  ====================================

    Both numbers are kept: ``header_confidence`` records what the name alone
    supported, ``confidence`` records the effective figure after the values had
    their say. Collapsing them would lose a distinction the manuscript needs.
    """
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

    if validation is not None:
        required = tuple(_apply_value_evidence(r, validation) for r in required)
        recommended = tuple(_apply_value_evidence(r, validation) for r in recommended)

    return _finalize(
        target=target,
        required=required,
        recommended=recommended,
        evidence_source=evidence_source,
        header=header or tuple(c.raw_name for c in mapping.columns),
        unmapped=tuple(c.raw_name for c in mapping.unresolved),
        extra_warnings=_mapping_warnings(mapping, by_concept) + _validation_warnings(validation),
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


# ----------------------------------------------------------------------
# Value evidence
# ----------------------------------------------------------------------

#: How a value-domain status modifies the header-derived confidence. FAIL is
#: deliberately severe: it drops the requirement below the "satisfied"
#: threshold, so a mapping the data contradicts cannot be reported as
#: confidently met.
_VALUE_CONFIDENCE_FACTOR = {
    "PASS": 1.0,
    "WARN": 0.85,
    "FAIL": 0.4,
    "NOT_TESTED": 1.0,
}


def _apply_value_evidence(result: RequirementResult, validation) -> RequirementResult:
    """Fold sampled-value evidence into one requirement result.

    The header-derived confidence is preserved in ``header_confidence``; the
    adjusted figure goes to ``confidence``. Nothing is remapped -- a
    contradiction lowers certainty and adds a note, it does not rewrite the
    column's concept.
    """
    if result.status is RequirementStatus.MISSING or not result.satisfied_by:
        return result

    statuses: list[str] = []
    notes: list[str] = []
    for raw_name in result.satisfied_by:
        column = validation.for_column(raw_name)
        if column is None:
            continue
        statuses.append(column.status.value)
        if column.warning:
            notes.append(f"{raw_name}: {column.warning}")

    if not statuses:
        return result

    # The weakest column governs: a requirement is only as sound as its
    # least-supported input.
    worst = next(
        candidate for candidate in ("FAIL", "WARN", "NOT_TESTED", "PASS") if candidate in statuses
    )

    header_confidence = result.confidence
    adjusted = header_confidence * _VALUE_CONFIDENCE_FACTOR[worst]

    if adjusted >= SATISFIED_THRESHOLD:
        status = RequirementStatus.SATISFIED
    elif adjusted >= UNCERTAIN_THRESHOLD:
        status = RequirementStatus.UNCERTAIN
    else:
        status = RequirementStatus.MISSING

    note = result.note
    if notes:
        note = "; ".join(notes) + (f". {note}" if note else "")

    return replace(
        result,
        status=status,
        confidence=adjusted,
        header_confidence=header_confidence,
        value_status=worst,
        note=note,
    )


def _validation_warnings(validation) -> tuple[str, ...]:
    """Surface value-domain contradictions and cross-column findings."""
    if validation is None:
        return ()

    warnings: list[str] = []
    for column in validation.columns:
        if column.suggested_concept:
            warnings.append(
                f"{column.raw_column!r} maps to {column.canonical_concept!r} by header "
                f"name, but its sampled values look like {column.suggested_concept!r}. "
                "GWASPoker reports this rather than remapping the column."
            )
        elif column.contradicts_header:
            warnings.append(f"{column.raw_column!r}: {column.warning}")
        if column.requires_transformation and column.status.value != "NOT_TESTED":
            warnings.append(
                f"{column.raw_column!r} needs a downstream transformation: "
                f"{column.requires_transformation}. GWASPoker does not apply it."
            )
    warnings.extend(validation.cross_column)
    return tuple(warnings)
