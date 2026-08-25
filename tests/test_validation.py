"""Value-domain validation.

Every test here answers one question: when the header says X but the values say
Y, does GWASPoker notice, and does it *report* rather than *rewrite*?

The two invariants under test throughout:

* a contradiction produces FAIL plus a ``suggested_concept``, and the mapping is
  left alone;
* header evidence and value evidence stay separately inspectable.
"""

from __future__ import annotations

import pytest

from gwaspoker.mapping.mapper import get_mapper
from gwaspoker.readiness.models import ReadinessVerdict, RequirementStatus
from gwaspoker.readiness.prs import assess_from_mapping
from gwaspoker.validation.values import (
    CONCEPT_RULES,
    ColumnValueValidation,
    ValueStatus,
    ValueValidationResult,
    is_missing,
    validate_values,
)


def check(header, rows, *, max_rows: int = 50) -> ValueValidationResult:
    """Map ``header`` then validate ``rows`` against it."""
    mapping = get_mapper().map_header(header)
    return validate_values(mapping, rows, max_rows=max_rows)


def column(result: ValueValidationResult, name: str) -> ColumnValueValidation:
    found = result.for_column(name)
    assert found is not None, f"{name} not validated"
    return found


def single(concept_header: str, values) -> ColumnValueValidation:
    """Validate one column in isolation."""
    rows = [[str(v)] for v in values]
    return column(check((concept_header,), rows), concept_header)


# ======================================================================
# chromosome
# ======================================================================


@pytest.mark.parametrize(
    "values",
    [
        ["1", "2", "22"],
        ["X", "Y", "MT"],
        ["chr1", "chr22", "chrX"],
        ["chrM", "chrMT", "chrY"],
        ["1", "X", "M"],
    ],
)
def test_valid_chromosome_values_pass(values) -> None:
    assert single("CHR", values).status is ValueStatus.PASS


def test_chromosome_holding_locus_strings_fails_and_suggests_chrpos() -> None:
    """The motivating case: a CHR header over chromosome:position values."""
    result = single("CHR", ["1:12345", "1:892331", "2:773291", "3:11", "4:22"])

    assert result.status is ValueStatus.FAIL
    assert result.canonical_concept == "chromosome"  # NOT silently remapped
    assert result.suggested_concept == "chromosome_position"
    assert "values disagree" in result.warning
    assert "could introduce a scientific error" in result.warning


def test_chromosome_holding_structured_ids_fails() -> None:
    result = single("CHR", ["1_12345_A_G", "2_88112_C_T", "3_771_G_A"])
    assert result.status is ValueStatus.FAIL


def test_chromosome_holding_chr_prefixed_locus_fails() -> None:
    result = single("CHR", ["chr1:12345", "chr2:88112", "chr3:771"])
    assert result.status is ValueStatus.FAIL
    assert result.suggested_concept == "chromosome_position"


def test_mostly_valid_chromosome_warns_rather_than_failing() -> None:
    values = ["1"] * 9 + ["1:12345"]
    assert single("CHR", values).status is ValueStatus.WARN


# ======================================================================
# position
# ======================================================================


def test_positive_integer_positions_pass() -> None:
    assert single("BP", ["12345", "99821342", "1"]).status is ValueStatus.PASS


@pytest.mark.parametrize(
    "values",
    [
        ["chr1:12345", "chr2:88112", "chr3:9"],
        ["1:12345", "2:88112", "3:9"],
        ["rs12345", "rs99821", "rs7"],
        ["A", "C", "G"],
        ["-100", "-200", "-300"],
    ],
)
def test_invalid_positions_fail(values) -> None:
    assert single("BP", values).status is ValueStatus.FAIL


def test_position_holding_locus_suggests_chromosome_position() -> None:
    result = single("BP", ["1:12345", "2:88112", "3:9912"])
    assert result.suggested_concept == "chromosome_position"


def test_zero_position_is_invalid() -> None:
    """Genomic coordinates are 1-based; 0 is not a coordinate."""
    assert single("BP", ["0", "0", "0"]).status is ValueStatus.FAIL


# ======================================================================
# chromosome_position
# ======================================================================


def test_chromosome_position_values_pass_and_flag_the_split() -> None:
    result = single("chr_pos", ["1:12345", "chr1:12345", "2_88112"])
    assert result.status is ValueStatus.PASS
    assert result.requires_transformation
    assert "split" in result.requires_transformation


def test_chromosome_position_split_is_reported_not_performed() -> None:
    """GWASPoker must never split the data itself."""
    rows = [["1:12345"], ["2:88112"]]
    mapping = get_mapper().map_header(("chr_pos",))
    result = validate_values(mapping, rows)

    # The caller's rows are untouched and no column was manufactured.
    assert rows == [["1:12345"], ["2:88112"]]
    assert len(mapping.columns) == 1
    # The split is announced, not done.
    assert "split" in column(result, "chr_pos").requires_transformation


# ======================================================================
# variant_id
# ======================================================================


@pytest.mark.parametrize(
    "values",
    [
        ["rs12345", "rs987654", "rs1"],
        ["1:12345:A:G", "2:88112:C:T", "3:9:G:A"],
        ["1_12345_A_G", "2_88112_C_T", "3_9_G_A"],
        ["chr1:12345:A:G", "chr2:88112:C:T", "chr3:9:G:A"],
    ],
)
def test_plausible_variant_ids_pass(values) -> None:
    assert single("SNP", values).status is ValueStatus.PASS


def test_sequential_integer_ids_are_weakly_supported() -> None:
    """A plain integer is no evidence of a variant identifier."""
    result = single("SNP", ["1", "2", "3", "4", "5"])
    assert result.status is not ValueStatus.PASS
    assert result.warning
    assert "integers" in result.warning or "integers" in " ".join(result.notes)


def test_generic_id_header_maps_at_reduced_confidence() -> None:
    """'ID' is a generic name; the header alone is weak evidence."""
    mapped = get_mapper().map_column("ID")
    assert mapped.canonical_name == "variant_id"
    assert mapped.mapping_method == "ambiguous_alias"
    assert mapped.confidence < 0.95
    assert mapped.note


# ======================================================================
# alleles
# ======================================================================


@pytest.mark.parametrize("values", [["A", "C", "G"], ["T", "A", "G"], ["AT", "GCC", "A"]])
def test_nucleotide_alleles_pass(values) -> None:
    assert single("A1", values).status is ValueStatus.PASS


def test_non_nucleotide_alleles_fail() -> None:
    assert single("A1", ["1", "2", "3"]).status is ValueStatus.FAIL


def test_missing_alleles_are_excluded_from_the_statistics() -> None:
    result = single("A1", ["A", "NA", "C", "#NA", "G"])
    assert result.non_missing_values == 3
    assert result.status is ValueStatus.PASS


def test_identical_alleles_are_reported_across_columns() -> None:
    rows = [["A", "A"], ["C", "C"], ["G", "T"]]
    result = check(("A1", "A2"), rows)
    findings = " ".join(result.cross_column)
    assert "identical" in findings
    assert "2 of 3" in findings
    assert "does not reorient" in findings


def test_distinct_alleles_produce_no_identity_finding() -> None:
    result = check(("A1", "A2"), [["A", "G"], ["C", "T"]])
    assert not any("identical" in f for f in result.cross_column)


# ======================================================================
# effect measures
# ======================================================================


def test_finite_betas_pass() -> None:
    assert single("BETA", ["0.12", "-0.05", "1e-3", "0"]).status is ValueStatus.PASS


def test_non_finite_betas_fail() -> None:
    assert single("BETA", ["inf", "-inf", "nan", "abc"]).status is ValueStatus.FAIL


def test_positive_odds_ratios_pass_and_flag_the_log_transform() -> None:
    result = single("OR", ["1.05", "0.94", "2.3"])
    assert result.status is ValueStatus.PASS
    assert "natural log" in result.requires_transformation


def test_non_positive_odds_ratios_fail() -> None:
    assert single("OR", ["0", "-1.2", "-0.5"]).status is ValueStatus.FAIL


def test_positive_hazard_ratios_pass() -> None:
    assert single("hazard_ratio", ["1.1", "0.8", "3.0"]).status is ValueStatus.PASS


def test_non_positive_hazard_ratios_fail() -> None:
    assert single("hazard_ratio", ["0", "-2", "-0.1"]).status is ValueStatus.FAIL


def test_odds_ratio_is_not_log_transformed_by_validation() -> None:
    rows = [["1.05"], ["0.94"]]
    validate_values(get_mapper().map_header(("OR",)), rows)
    assert rows == [["1.05"], ["0.94"]]


# ======================================================================
# standard error
# ======================================================================


def test_non_negative_standard_errors_pass() -> None:
    assert single("SE", ["0.03", "0.0", "1.2"]).status is ValueStatus.PASS


def test_negative_standard_errors_fail() -> None:
    assert single("SE", ["-0.03", "-1", "-2.5"]).status is ValueStatus.FAIL


# ======================================================================
# p-values
# ======================================================================


def test_probability_scale_p_values_pass() -> None:
    assert single("P", ["0.05", "1e-8", "1", "0.9999"]).status is ValueStatus.PASS


@pytest.mark.parametrize("values", [["-0.1", "-0.2", "-0.3"], ["1.2", "3.4", "5.6"]])
def test_out_of_domain_p_values_fail(values) -> None:
    assert single("P", values).status is ValueStatus.FAIL


def test_p_value_of_zero_warns_rather_than_failing() -> None:
    """Exactly 0 is floating-point underflow, not a broken column."""
    result = single("P", ["0", "1e-8", "0.05", "0.2", "0.9"])
    assert result.status is ValueStatus.WARN
    assert result.status is not ValueStatus.FAIL
    assert "underflow" in result.warning


def test_all_zero_p_values_still_only_warn() -> None:
    result = single("P", ["0", "0", "0"])
    assert result.status is ValueStatus.WARN


def test_large_p_values_suggest_neg_log10_without_remapping() -> None:
    result = single("P", ["8.2", "12.5", "3.4", "22.1"])
    assert result.status is ValueStatus.FAIL
    assert result.canonical_concept == "p_value"  # unchanged
    assert result.suggested_concept == "neg_log10_p_value"


def test_neg_log10_p_values_are_not_judged_as_probabilities() -> None:
    result = single("neg_log_10_p_value", ["8.2", "12.5", "0"])
    assert result.canonical_concept == "neg_log10_p_value"
    assert result.status is ValueStatus.PASS
    assert "10**-x" in result.requires_transformation


def test_negative_neg_log10_p_values_fail() -> None:
    assert single("neg_log_10_p_value", ["-1", "-2", "-3"]).status is ValueStatus.FAIL


# ======================================================================
# frequencies
# ======================================================================


def test_effect_allele_frequency_in_unit_interval_passes() -> None:
    assert single("EAF", ["0.31", "0.0", "1.0", "0.75"]).status is ValueStatus.PASS


def test_effect_allele_frequency_above_one_fails() -> None:
    assert single("EAF", ["1.2", "3.4", "5.0"]).status is ValueStatus.FAIL


def test_effect_allele_frequency_below_zero_fails() -> None:
    assert single("EAF", ["-0.1", "-0.5", "-1"]).status is ValueStatus.FAIL


def test_minor_allele_frequency_within_half_passes() -> None:
    assert single("MAF", ["0.01", "0.5", "0.23"]).status is ValueStatus.PASS


def test_minor_allele_frequency_above_half_fails() -> None:
    """0.8 cannot be a MINOR allele frequency."""
    assert single("MAF", ["0.8", "0.9", "0.7"]).status is ValueStatus.FAIL


def test_minor_allele_frequency_tolerates_floating_point_edges() -> None:
    assert single("MAF", ["0.5001", "0.4999", "-0.0001"]).status is ValueStatus.PASS


def test_generic_allele_frequency_uses_the_unit_interval() -> None:
    assert single("freq", ["0.8", "0.2", "1.0"]).status is ValueStatus.PASS


# ======================================================================
# counts
# ======================================================================


def test_positive_sample_sizes_pass() -> None:
    assert single("N", ["150000", "149800", "1"]).status is ValueStatus.PASS


@pytest.mark.parametrize("values", [["-100", "-200", "-1"], ["0", "0", "0"]])
def test_non_positive_sample_sizes_fail(values) -> None:
    assert single("N", values).status is ValueStatus.FAIL


def test_case_and_control_counts_accept_zero() -> None:
    assert single("n_cases", ["0", "100", "200"]).status is ValueStatus.PASS


def test_negative_case_counts_fail() -> None:
    assert single("n_cases", ["-1", "-5", "-9"]).status is ValueStatus.FAIL


def test_cases_plus_controls_matching_n_is_reported_consistent() -> None:
    rows = [["1000", "300", "700"], ["1000", "400", "600"]]
    result = check(("N", "n_cases", "n_controls"), rows)
    findings = " ".join(result.cross_column)
    assert "agrees with the sample size" in findings


def test_cases_plus_controls_mismatching_n_is_reported() -> None:
    rows = [["1000", "300", "100"], ["1000", "200", "50"]]
    result = check(("N", "n_cases", "n_controls"), rows)
    findings = " ".join(result.cross_column)
    assert "differs from the sample size" in findings
    assert "not adjusted" in findings


def test_small_case_control_discrepancy_is_tolerated() -> None:
    """Per-variant N varies slightly with missingness; 1% is allowed."""
    rows = [["1000", "300", "703"], ["1000", "400", "598"]]
    result = check(("N", "n_cases", "n_controls"), rows)
    assert not any("differs" in f for f in result.cross_column)


# ======================================================================
# INFO
# ======================================================================


def test_info_scores_in_the_broad_domain_pass() -> None:
    result = single("INFO", ["0.98", "1.0", "0.4"])
    assert result.status is ValueStatus.PASS
    assert any("usual 0-1 range" in n for n in result.notes)


def test_info_scores_outside_zero_to_two_fail() -> None:
    assert single("INFO", ["5.0", "-1.0", "9.9"]).status is ValueStatus.FAIL


def test_low_info_is_not_filtered() -> None:
    """GWASPoker is triage, not QC: INFO 0.2 is valid, not rejected."""
    result = single("INFO", ["0.2", "0.1", "0.3"])
    assert result.status is ValueStatus.PASS


# ======================================================================
# Result plumbing
# ======================================================================


def test_unmapped_columns_are_not_tested_and_never_forced() -> None:
    result = check(("qc_flag_v3",), [["anything"], ["else"]])
    entry = column(result, "qc_flag_v3")
    assert entry.canonical_concept == "unknown"
    assert entry.status is ValueStatus.NOT_TESTED
    assert entry.suggested_concept is None


def test_all_missing_column_is_not_tested() -> None:
    entry = single("BETA", ["NA", "#NA", ".", ""])
    assert entry.status is ValueStatus.NOT_TESTED
    assert entry.non_missing_values == 0
    assert entry.valid_fraction is None  # distinct from 0.0


def test_no_rows_yields_not_tested_with_a_reason() -> None:
    result = validate_values(get_mapper().map_header(("CHR",)), [])
    assert result.overall_status is ValueStatus.NOT_TESTED
    assert "no data rows" in result.error


def test_rows_are_capped_at_max_rows() -> None:
    rows = [["1"] for _ in range(500)]
    result = check(("CHR",), rows, max_rows=20)
    assert result.available_rows == 500
    assert result.rows_checked == 20
    assert column(result, "CHR").non_missing_values == 20


def test_examples_are_capped() -> None:
    entry = single("CHR", ["1:1", "2:2", "3:3", "4:4", "5:5", "6:6", "7:7"])
    assert len(entry.examples_invalid) <= 3


def test_overall_status_is_the_worst_column() -> None:
    result = check(("CHR", "BP"), [["1", "-5"], ["2", "-9"], ["X", "-1"]])
    assert column(result, "CHR").status is ValueStatus.PASS
    assert column(result, "BP").status is ValueStatus.FAIL
    assert result.overall_status is ValueStatus.FAIL


def test_result_serialises() -> None:
    import json

    result = check(("CHR", "BP", "P"), [["1", "123", "0.05"], ["2", "456", "1e-8"]])
    payload = result.to_dict()

    assert payload["rows_checked"] == 2
    assert payload["overall_status"] == "PASS"
    assert len(payload["columns"]) == 3
    entry = payload["columns"][0]
    assert entry["raw_column"] == "CHR"
    assert entry["canonical_concept"] == "chromosome"
    assert entry["valid_fraction"] == 1.0
    json.dumps(payload)


def test_serialisation_stays_small() -> None:
    """A report must not carry thousands of values."""
    import json

    rows = [[str(i % 22 + 1)] for i in range(500)]
    payload = check(("CHR",), rows).to_dict()
    assert len(json.dumps(payload)) < 2000


def test_status_for_concept() -> None:
    result = check(("CHR", "BP"), [["1:1", "123"], ["2:2", "456"]])
    assert result.status_for_concept("chromosome") is ValueStatus.FAIL
    assert result.status_for_concept("position") is ValueStatus.PASS
    assert result.status_for_concept("beta") is None


def test_is_missing_covers_the_usual_markers() -> None:
    for token in ("", ".", "-", "NA", "nan", "#NA", "N/A", "null", "None"):
        assert is_missing(token)
    assert not is_missing("0")
    assert not is_missing("A")


def test_every_required_prs_concept_has_a_rule() -> None:
    """A concept with no rule is NOT_TESTED, so gaps must be deliberate."""
    from gwaspoker.readiness.prs import PRS_RECOMMENDED, PRS_REQUIRED

    concepts = set()
    for requirement_spec in (*PRS_REQUIRED, *PRS_RECOMMENDED):
        concepts.update(requirement_spec.any_of)
        concepts.update(requirement_spec.all_of)
        for option in requirement_spec.conditional:
            concepts.add(option.concept)
    missing = concepts - set(CONCEPT_RULES)
    assert not missing, f"no value-domain rule for {sorted(missing)}"


# ======================================================================
# Integration with readiness
# ======================================================================


def test_failing_values_downgrade_a_required_field() -> None:
    """A strong header mapping that the data contradicts is not 'satisfied'."""
    header = ("CHR", "BP", "A1", "A2", "BETA", "P")
    rows = [
        ["1:12345", "12345", "A", "G", "0.1", "0.05"],
        ["2:88112", "88112", "C", "T", "-0.2", "1e-8"],
        ["3:99", "99", "G", "A", "0.3", "0.02"],
    ]
    mapping = get_mapper().map_header(header)
    validation = validate_values(mapping, rows)
    result = assess_from_mapping(mapping, validation=validation)

    identification = next(r for r in result.required if r.key == "variant_identification")
    assert identification.status is not RequirementStatus.SATISFIED
    assert identification.value_status == "FAIL"
    # Both numbers survive: the header still supported it, the values did not.
    assert identification.header_confidence is not None
    assert identification.confidence < identification.header_confidence


def test_passing_values_leave_a_required_field_satisfied() -> None:
    header = ("CHR", "BP", "A1", "A2", "BETA", "P")
    rows = [
        ["1", "12345", "A", "G", "0.1", "0.05"],
        ["2", "88112", "C", "T", "-0.2", "1e-8"],
    ]
    mapping = get_mapper().map_header(header)
    result = assess_from_mapping(mapping, validation=validate_values(mapping, rows))

    identification = next(r for r in result.required if r.key == "variant_identification")
    assert identification.status is RequirementStatus.SATISFIED
    assert identification.value_status == "PASS"
    assert result.verdict is ReadinessVerdict.READY


def test_contradiction_surfaces_as_a_warning() -> None:
    header = ("CHR", "BP", "A1", "A2", "BETA", "P")
    rows = [["1:1", "1", "A", "G", "0.1", "0.05"]] * 3
    mapping = get_mapper().map_header(header)
    result = assess_from_mapping(mapping, validation=validate_values(mapping, rows))

    warnings = " ".join(result.warnings)
    assert "chromosome_position" in warnings
    assert "rather than remapping" in warnings


def test_readiness_without_validation_is_unchanged() -> None:
    """Value validation is additive; omitting it must not alter the verdict."""
    header = ("CHR", "BP", "SNP", "A1", "A2", "BETA", "SE", "P", "N")
    mapping = get_mapper().map_header(header)
    assert assess_from_mapping(mapping).verdict is ReadinessVerdict.READY
    assert assess_from_mapping(mapping, validation=None).verdict is ReadinessVerdict.READY


def test_transformation_requirements_are_reported_not_applied() -> None:
    header = ("SNP", "A1", "A2", "OR", "P")
    rows = [["rs1", "A", "G", "1.05", "0.05"], ["rs2", "C", "T", "0.94", "1e-8"]]
    mapping = get_mapper().map_header(header)
    result = assess_from_mapping(mapping, validation=validate_values(mapping, rows))

    warnings = " ".join(result.warnings)
    assert "natural log" in warnings
    assert "does not apply it" in warnings or "GWASPoker does not" in warnings


def test_probe_attaches_validation(fixtures_dir, config) -> None:
    """The probe runs validation on rows it already has -- no extra bytes."""
    from gwaspoker.http import HttpClient
    from gwaspoker.probe.remote import RemoteProber

    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(fixtures_dir / "ssf_like.tsv.gz", probe_bytes=16384)

    assert result.value_validation is not None
    assert result.value_validation.rows_checked > 0
    assert result.value_status is ValueStatus.PASS
    assert result.to_dict()["value_validation"]["overall_status"] == "PASS"


def test_probe_retains_more_than_five_sample_rows(fixtures_dir, config) -> None:
    """Validation needs a sample; five rows is too few to be convincing."""
    from gwaspoker.http import HttpClient
    from gwaspoker.probe.remote import RemoteProber

    prober = RemoteProber(config, HttpClient(config))
    result = prober.probe_local(fixtures_dir / "ssf_like.tsv.gz", probe_bytes=16384)
    assert len(result.header.sample_rows) > 5


def test_gwaslab_is_absent_from_the_prediction_path() -> None:
    """GWASPoker must decide independently of GWASLab.

    GWASLab is the downstream full-file comparator in the planned evaluation.
    If it were consulted to *make* a GWASPoker prediction, comparing GWASPoker
    against GWASLab would be circular and the benchmark would measure nothing.
    The integration stays quarantined in `integrations/gwaslab.py`, reachable
    only after a download.
    """
    import ast
    from pathlib import Path

    import gwaspoker

    root = Path(gwaspoker.__file__).parent
    prediction_path = ("probe", "mapping", "validation", "readiness", "catalog")
    offenders = []

    for package in prediction_path:
        for source in sorted((root / package).rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if "gwaslab" in name.lower():
                        offenders.append(f"{package}/{source.name}:{node.lineno} -> {name}")

    assert not offenders, "GWASLab reached the prediction path: " + "; ".join(offenders)
