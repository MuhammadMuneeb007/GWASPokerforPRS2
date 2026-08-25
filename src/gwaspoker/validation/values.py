"""Value-domain validation of sampled data rows.

The mapper decides what a column *means* from its **name**. That is not
scientifically sufficient. A column headed ``CHR`` whose values are
``1:12345`` is a chromosome-position field wearing a chromosome's label, and a
tool that maps it to ``chromosome`` on the strength of the header alone will
hand a downstream PRS pipeline something it cannot use.

This module supplies the second, independent line of evidence: are the values
in that column compatible with the concept the header claimed?

Scope, deliberately narrow
--------------------------
These are **structural sanity checks on a sample of rows**, not GWAS quality
control. GWASPoker is pre-download triage. Specifically it does *not*:

* filter variants on INFO, MAF, p-value or anything else;
* harmonise alleles against a reference, or lift over coordinates;
* transform values -- no ``log(OR)``, no ``10**-x`` on a -log10 p-value, no
  splitting of ``chr:pos``. Where a transformation *would* be needed it is
  reported as ``requires_transformation`` and left for the user and the
  downstream tool.

Two rules govern everything here
--------------------------------
1. **Report, never rewrite.** A contradiction produces a FAIL and a
   ``suggested_concept``. The mapping itself is not silently changed, because
   an automatic correction is an automatic opportunity to be wrong.
2. **Header evidence and value evidence stay separate.** The output carries
   both, so the manuscript can measure header-mapping accuracy and value-domain
   consistency independently rather than collapsing them into one number.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

#: Rows sampled by default. Bounded because these come from the probe prefix
#: that is already in memory -- validation never triggers another request.
DEFAULT_MAX_ROWS = 50

#: Examples of each kind retained in the output. Enough to see what went wrong,
#: few enough that a JSON report stays small.
MAX_EXAMPLES = 3

#: Fraction of testable values that must be valid for a PASS.
PASS_THRESHOLD = 0.95
#: Below this the column is judged to contradict its header.
FAIL_THRESHOLD = 0.80

#: Missing-value markers, including the ``#NA`` that GWAS-SSF specifies.
_MISSING = frozenset({"", ".", "-", "na", "nan", "n/a", "#na", "null", "none", "nd", "<na>", "?"})


class ValueStatus(str, Enum):
    """Outcome of validating one column's values."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    NOT_TESTED = "NOT_TESTED"

    @property
    def symbol(self) -> str:
        return {"PASS": "✓", "WARN": "!", "FAIL": "✗", "NOT_TESTED": "–"}[self.value]


def is_missing(value: Any) -> bool:
    """True for the missing markers used in summary statistics."""
    return str(value).strip().casefold() in _MISSING


def _to_float(value: str) -> Optional[float]:
    """Parse a finite float, or ``None``. Infinity and NaN are not finite."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ----------------------------------------------------------------------
# Patterns
# ----------------------------------------------------------------------

#: A bare chromosome: 1-22, X, Y, MT/M, optionally "chr"-prefixed.
_CHROMOSOME_RE = re.compile(r"^(?:chr)?(?:[1-9]|1\d|2[0-5]|0?[1-9]|X|Y|XY|M|MT)$", re.IGNORECASE)

#: A combined locus: 1:12345, chr1:12345, 1_12345 -- optionally with alleles.
_CHR_POS_RE = re.compile(
    r"^(?:chr)?(?:[0-9]{1,2}|X|Y|XY|MT?)[:_\-]\d+(?:[:_\-][ACGTN]+[:_\-][ACGTN]+)?$",
    re.IGNORECASE,
)

_INTEGER_RE = re.compile(r"^[+-]?\d+$")

#: An rsID.
_RSID_RE = re.compile(r"^rs\d+$", re.IGNORECASE)

#: A structured variant identifier: 1:12345:A:G, 1_12345_A_G, chr1-12345-A-G.
_STRUCTURED_ID_RE = re.compile(
    r"^(?:chr)?(?:[0-9]{1,2}|X|Y|XY|MT?)[:_\-]\d+[:_\-][ACGTN\-]+[:_\-][ACGTN\-]+$",
    re.IGNORECASE,
)

#: Nucleotide allele, single base or an indel string. ``-``/``I``/``D`` are
#: common indel encodings, but ``-`` is also a missing marker, so it is only
#: reached for values that survived the missing-value filter.
_ALLELE_RE = re.compile(r"^[ACGTN]+$", re.IGNORECASE)
_INDEL_CODE_RE = re.compile(r"^(?:I|D|R|INS|DEL)$", re.IGNORECASE)


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------


@dataclass
class ColumnValueValidation:
    """Validation of one mapped column against its claimed concept."""

    raw_column: str
    canonical_concept: str
    rows_checked: int = 0
    non_missing_values: int = 0
    valid_values: int = 0
    invalid_values: int = 0
    status: ValueStatus = ValueStatus.NOT_TESTED
    examples_valid: tuple[str, ...] = ()
    examples_invalid: tuple[str, ...] = ()
    warning: Optional[str] = None
    suggested_concept: Optional[str] = None
    requires_transformation: Optional[str] = None
    notes: tuple[str, ...] = ()

    @property
    def valid_fraction(self) -> Optional[float]:
        """Fraction of non-missing values that were valid, or ``None``.

        ``None`` when nothing testable was seen -- distinct from ``0.0``.
        """
        if not self.non_missing_values:
            return None
        return self.valid_values / self.non_missing_values

    @property
    def contradicts_header(self) -> bool:
        """True when the values argue against the header's claim."""
        return self.status is ValueStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        fraction = self.valid_fraction
        return {
            "raw_column": self.raw_column,
            "canonical_concept": self.canonical_concept,
            "rows_checked": self.rows_checked,
            "non_missing_values": self.non_missing_values,
            "valid_values": self.valid_values,
            "invalid_values": self.invalid_values,
            "valid_fraction": round(fraction, 4) if fraction is not None else None,
            "status": self.status.value,
            "examples_valid": list(self.examples_valid),
            "examples_invalid": list(self.examples_invalid),
            "warning": self.warning,
            "suggested_concept": self.suggested_concept,
            "requires_transformation": self.requires_transformation,
            "notes": list(self.notes),
        }


@dataclass
class ValueValidationResult:
    """Validation of every testable column in one probed file."""

    available_rows: int = 0
    rows_checked: int = 0
    columns: tuple[ColumnValueValidation, ...] = ()
    cross_column: tuple[str, ...] = field(default_factory=tuple)
    error: Optional[str] = None

    @property
    def overall_status(self) -> ValueStatus:
        """Worst status across the columns tested."""
        statuses = [c.status for c in self.columns if c.status is not ValueStatus.NOT_TESTED]
        if not statuses:
            return ValueStatus.NOT_TESTED
        if any(s is ValueStatus.FAIL for s in statuses):
            return ValueStatus.FAIL
        if any(s is ValueStatus.WARN for s in statuses):
            return ValueStatus.WARN
        return ValueStatus.PASS

    def for_column(self, raw_column: str) -> Optional[ColumnValueValidation]:
        for column in self.columns:
            if column.raw_column == raw_column:
                return column
        return None

    def status_for_concept(self, concept: str) -> Optional[ValueStatus]:
        """Worst status among columns mapped to ``concept``."""
        statuses = [
            c.status
            for c in self.columns
            if c.canonical_concept == concept and c.status is not ValueStatus.NOT_TESTED
        ]
        if not statuses:
            return None
        for candidate in (ValueStatus.FAIL, ValueStatus.WARN, ValueStatus.PASS):
            if candidate in statuses:
                return candidate
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_rows": self.available_rows,
            "rows_checked": self.rows_checked,
            "overall_status": self.overall_status.value,
            "columns": [c.to_dict() for c in self.columns],
            "cross_column": list(self.cross_column),
            "error": self.error,
        }


# ----------------------------------------------------------------------
# Per-concept predicates
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptRule:
    """How to test one canonical concept, and what to say when it fails."""

    predicate: Callable[[str], bool]
    description: str
    #: Concept the values look like instead, when they systematically fail.
    alternative: Optional[Callable[[Sequence[str]], Optional[str]]] = None
    #: Transformation a downstream tool will need. Reported, never applied.
    requires_transformation: Optional[str] = None


def _valid_chromosome(value: str) -> bool:
    return bool(_CHROMOSOME_RE.match(value.strip()))


def _valid_position(value: str) -> bool:
    text = value.strip()
    if not _INTEGER_RE.match(text):
        return False
    return int(text) > 0


def _valid_chromosome_position(value: str) -> bool:
    return bool(_CHR_POS_RE.match(value.strip()))


def _valid_variant_id(value: str) -> bool:
    text = value.strip()
    return bool(_RSID_RE.match(text) or _STRUCTURED_ID_RE.match(text))


def _valid_allele(value: str) -> bool:
    text = value.strip()
    return bool(_ALLELE_RE.match(text) or _INDEL_CODE_RE.match(text))


def _finite(value: str) -> bool:
    return _to_float(value) is not None


def _positive(value: str) -> bool:
    number = _to_float(value)
    return number is not None and number > 0


def _non_negative(value: str) -> bool:
    number = _to_float(value)
    return number is not None and number >= 0


def _in_unit_interval(value: str) -> bool:
    number = _to_float(value)
    return number is not None and 0.0 <= number <= 1.0


def _probability(value: str) -> bool:
    """A p-value on the probability scale.

    ``0`` is accepted here and reported separately: it occurs legitimately
    through floating-point underflow at very small p-values, and calling the
    whole column invalid because of it would be wrong.
    """
    return _in_unit_interval(value)


def _maf_domain(value: str, tolerance: float = 0.001) -> bool:
    number = _to_float(value)
    return number is not None and -tolerance <= number <= 0.5 + tolerance


def _info_domain(value: str) -> bool:
    """INFO/r-squared. The broad 0-2 window matches LDSC's own sanity check.

    LDSC's ``filter_info`` treats values outside ``[0, 2]`` as evidence that the
    column is mislabelled, which is exactly the structural question asked here.
    How many fall in the usual 0-1 range is reported as a note; no filtering is
    applied.
    """
    number = _to_float(value)
    return number is not None and 0.0 <= number <= 2.0


def _positive_count(value: str) -> bool:
    number = _to_float(value)
    return number is not None and number > 0


def _non_negative_count(value: str) -> bool:
    number = _to_float(value)
    return number is not None and number >= 0


def _looks_like_locus(values: Sequence[str]) -> Optional[str]:
    """Do these values look like combined chromosome:position instead?"""
    testable = [v for v in values if not is_missing(v)]
    if not testable:
        return None
    hits = sum(1 for v in testable if _valid_chromosome_position(v))
    return "chromosome_position" if hits / len(testable) >= FAIL_THRESHOLD else None


def _looks_like_variant_id(values: Sequence[str]) -> Optional[str]:
    testable = [v for v in values if not is_missing(v)]
    if not testable:
        return None
    hits = sum(1 for v in testable if _valid_variant_id(v))
    return "variant_id" if hits / len(testable) >= FAIL_THRESHOLD else None


#: The validation rules. A concept absent from this table is NOT_TESTED rather
#: than assumed valid -- silence is not evidence.
CONCEPT_RULES: dict[str, ConceptRule] = {
    "chromosome": ConceptRule(
        _valid_chromosome,
        "1-25, X, Y, MT/M, optionally 'chr'-prefixed",
        alternative=_looks_like_locus,
    ),
    "position": ConceptRule(
        _valid_position,
        "a positive integer genomic coordinate",
        alternative=_looks_like_locus,
    ),
    "chromosome_position": ConceptRule(
        _valid_chromosome_position,
        "a combined locus such as 1:12345 or chr1_12345",
        requires_transformation="split into separate chromosome and position columns",
    ),
    "variant_id": ConceptRule(
        _valid_variant_id,
        "an rsID or a structured identifier such as 1:12345:A:G",
    ),
    "effect_allele": ConceptRule(_valid_allele, "a nucleotide or indel allele"),
    "other_allele": ConceptRule(_valid_allele, "a nucleotide or indel allele"),
    "beta": ConceptRule(_finite, "a finite number"),
    "odds_ratio": ConceptRule(
        _positive,
        "a finite number greater than zero",
        requires_transformation="natural log before use as a PRS weight",
    ),
    "hazard_ratio": ConceptRule(
        _positive,
        "a finite number greater than zero",
        requires_transformation="natural log before use as a PRS weight",
    ),
    "z_score": ConceptRule(_finite, "a finite number"),
    "standard_error": ConceptRule(_non_negative, "a finite number at or above zero"),
    "p_value": ConceptRule(_probability, "a probability in [0, 1]"),
    "neg_log10_p_value": ConceptRule(
        _non_negative,
        "a finite number at or above zero",
        requires_transformation="10**-x to recover the p-value scale",
    ),
    "effect_allele_frequency": ConceptRule(_in_unit_interval, "a frequency in [0, 1]"),
    "minor_allele_frequency": ConceptRule(_maf_domain, "a frequency in [0, 0.5]"),
    "allele_frequency": ConceptRule(_in_unit_interval, "a frequency in [0, 1]"),
    "sample_size": ConceptRule(_positive_count, "a positive count"),
    "cases": ConceptRule(_non_negative_count, "a count at or above zero"),
    "controls": ConceptRule(_non_negative_count, "a count at or above zero"),
    "info_score": ConceptRule(_info_domain, "an imputation quality score in [0, 2]"),
    "confidence_interval_lower": ConceptRule(_finite, "a finite number"),
    "confidence_interval_upper": ConceptRule(_finite, "a finite number"),
    "n_studies": ConceptRule(_positive_count, "a positive count"),
}


# ----------------------------------------------------------------------
# The validator
# ----------------------------------------------------------------------


def validate_values(
    mapping,
    sample_rows: Sequence[Sequence[str]],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ValueValidationResult:
    """Check sampled values against the concepts the header claimed.

    ``mapping`` is a :class:`~gwaspoker.mapping.mapper.MappingResult`;
    ``sample_rows`` are data rows already recovered by the bounded probe. No
    additional bytes are requested: if the probe returned no usable rows the
    result is ``NOT_TESTED``, which is an honest answer.
    """
    rows = [r for r in sample_rows if r]
    result = ValueValidationResult(available_rows=len(rows))

    if mapping is None or not mapping.columns:
        result.error = "no column mapping was available to validate"
        return result
    if not rows:
        result.error = "the probe recovered no data rows, so values were not tested"
        return result

    checked = rows[:max_rows]
    result.rows_checked = len(checked)

    columns: list[ColumnValueValidation] = []
    for index, column in enumerate(mapping.columns):
        values = [row[index] for row in checked if index < len(row)]
        columns.append(_validate_column(column, values, len(checked)))

    result.columns = tuple(columns)
    result.cross_column = tuple(_cross_column_checks(mapping, checked))
    return result


def _validate_column(column, values: Sequence[str], rows_checked: int) -> ColumnValueValidation:
    """Validate one column's sampled values."""
    outcome = ColumnValueValidation(
        raw_column=column.raw_name,
        canonical_concept=column.canonical_name,
        rows_checked=rows_checked,
    )

    rule = CONCEPT_RULES.get(column.canonical_name)
    if rule is None:
        # Unmapped columns, and concepts with no defined domain, are not
        # judged. NOT_TESTED never becomes a negative finding.
        outcome.status = ValueStatus.NOT_TESTED
        outcome.notes = (f"no value-domain rule defined for {column.canonical_name!r}; not tested",)
        return outcome

    testable = [v for v in values if not is_missing(v)]
    outcome.non_missing_values = len(testable)
    if not testable:
        outcome.status = ValueStatus.NOT_TESTED
        outcome.notes = ("every sampled value was missing; not tested",)
        return outcome

    valid: list[str] = []
    invalid: list[str] = []
    for value in testable:
        (valid if rule.predicate(value) else invalid).append(str(value).strip())

    outcome.valid_values = len(valid)
    outcome.invalid_values = len(invalid)
    outcome.examples_valid = tuple(valid[:MAX_EXAMPLES])
    outcome.examples_invalid = tuple(invalid[:MAX_EXAMPLES])
    outcome.requires_transformation = rule.requires_transformation

    fraction = outcome.valid_fraction or 0.0
    if fraction >= PASS_THRESHOLD:
        outcome.status = ValueStatus.PASS
    elif fraction >= FAIL_THRESHOLD:
        outcome.status = ValueStatus.WARN
        outcome.warning = (
            f"{outcome.invalid_values} of {outcome.non_missing_values} sampled values "
            f"are not {rule.description}"
        )
    else:
        outcome.status = ValueStatus.FAIL
        outcome.warning = (
            f"only {fraction:.0%} of sampled values are {rule.description}; the header "
            f"maps this column to {column.canonical_name!r} but the values disagree"
        )

    if outcome.status is not ValueStatus.PASS and rule.alternative is not None:
        suggestion = rule.alternative(testable)
        if suggestion and suggestion != column.canonical_name:
            outcome.suggested_concept = suggestion
            outcome.warning = (
                (outcome.warning or "")
                + f". The values look like {suggestion!r}; GWASPoker reports this "
                "rather than remapping the column, because an automatic correction "
                "could introduce a scientific error"
            )

    _add_concept_notes(outcome, column.canonical_name, testable, valid)
    return outcome


def _add_concept_notes(
    outcome: ColumnValueValidation,
    concept: str,
    testable: Sequence[str],
    valid: Sequence[str],
) -> None:
    """Concept-specific observations that refine, but never override, the status."""
    notes = list(outcome.notes)

    if concept == "p_value":
        zeros = sum(1 for v in testable if _to_float(v) == 0.0)
        if zeros:
            notes.append(
                f"{zeros} sampled p-value(s) are exactly 0; this normally reflects "
                "floating-point underflow at very small p-values, not an invalid column"
            )
            if outcome.status is ValueStatus.PASS:
                outcome.status = ValueStatus.WARN
                outcome.warning = (
                    f"{zeros} p-value(s) are exactly 0 (underflow). The column is "
                    "otherwise on the probability scale."
                )
        above_one = [v for v in testable if (_to_float(v) or 0) > 1.0]
        if above_one and len(above_one) / len(testable) >= FAIL_THRESHOLD:
            notes.append(
                "values exceed 1, so this is not a probability-scale p-value; it may "
                "be -log10(p), but GWASPoker does not remap it on this evidence alone"
            )
            outcome.suggested_concept = outcome.suggested_concept or "neg_log10_p_value"

    elif concept == "variant_id":
        integers = sum(1 for v in testable if _INTEGER_RE.match(str(v).strip()))
        if integers / len(testable) >= FAIL_THRESHOLD:
            notes.append(
                "values are plain integers; these carry no evidence of being variant "
                "identifiers, so the header mapping is weakly supported"
            )
            if outcome.status is ValueStatus.PASS:
                outcome.status = ValueStatus.WARN
            outcome.warning = outcome.warning or (
                "the values are sequential-looking integers rather than rsIDs or "
                "structured identifiers"
            )

    elif concept == "info_score":
        in_unit = sum(1 for v in valid if 0.0 <= (_to_float(v) or -1) <= 1.0)
        notes.append(
            f"{in_unit} of {len(valid)} valid value(s) fall in the usual 0-1 range "
            "(GWASPoker reports this; it applies no INFO filter)"
        )

    elif concept in ("cases", "controls", "sample_size"):
        non_integers = sum(
            1 for v in testable if not _INTEGER_RE.match(str(v).strip().split(".")[0])
        )
        if non_integers:
            notes.append(f"{non_integers} sampled value(s) are not integer-like")

    outcome.notes = tuple(notes)


def _cross_column_checks(mapping, rows: Sequence[Sequence[str]]) -> list[str]:
    """Checks that need more than one column. Reported, never acted on."""
    findings: list[str] = []
    by_concept = mapping.by_concept()

    def column_index(concept: str) -> Optional[int]:
        columns = by_concept.get(concept)
        if not columns:
            return None
        best = max(columns, key=lambda c: c.confidence)
        for index, entry in enumerate(mapping.columns):
            if entry is best:
                return index
        return None

    # --- effect allele vs other allele --------------------------------
    effect_index = column_index("effect_allele")
    other_index = column_index("other_allele")
    if effect_index is not None and other_index is not None:
        compared = 0
        identical = 0
        for row in rows:
            if effect_index >= len(row) or other_index >= len(row):
                continue
            effect, other = row[effect_index], row[other_index]
            if is_missing(effect) or is_missing(other):
                continue
            compared += 1
            if effect.strip().upper() == other.strip().upper():
                identical += 1
        if compared and identical:
            findings.append(
                f"effect and other allele are identical in {identical} of {compared} "
                f"compared row(s) ({identical / compared:.0%}); a variant cannot have "
                "the same allele on both sides. GWASPoker does not reorient alleles."
            )

    # --- cases + controls vs N ----------------------------------------
    n_index = column_index("sample_size")
    cases_index = column_index("cases")
    controls_index = column_index("controls")
    if None not in (n_index, cases_index, controls_index):
        compared = 0
        inconsistent = 0
        for row in rows:
            if max(n_index, cases_index, controls_index) >= len(row):
                continue
            total = _to_float(row[n_index])
            cases = _to_float(row[cases_index])
            controls = _to_float(row[controls_index])
            if None in (total, cases, controls) or total <= 0:
                continue
            compared += 1
            # 1% tolerance: per-variant N legitimately varies slightly with
            # missingness, so an exact match is not expected.
            if abs((cases + controls) - total) / total > 0.01:
                inconsistent += 1
        if compared:
            if inconsistent:
                findings.append(
                    f"cases + controls differs from the sample size by more than 1% in "
                    f"{inconsistent} of {compared} compared row(s). Values are reported "
                    "as published and are not adjusted."
                )
            else:
                findings.append(
                    f"cases + controls agrees with the sample size in all "
                    f"{compared} compared row(s)"
                )

    return findings
