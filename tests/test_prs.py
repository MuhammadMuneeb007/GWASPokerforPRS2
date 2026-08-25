"""PRS-readiness rules.

Each requirement is checked in isolation, and the scientifically dangerous cases
(odds ratios not log-transformed, -log10(p) mistaken for p, MAF substituted for
EAF) are checked for the warnings they must produce.
"""

from __future__ import annotations

import pytest

from gwaspoker.catalog.sumstats_api import SSF_MANDATORY_FIELDS
from gwaspoker.mapping.mapper import get_mapper
from gwaspoker.readiness.models import ReadinessVerdict, RequirementStatus
from gwaspoker.readiness.prs import (
    PRS_RECOMMENDED,
    PRS_REQUIRED,
    assess_from_declared_fields,
    assess_from_mapping,
)


def assess(header):
    return assess_from_mapping(get_mapper().map_header(header))


def requirement(result, key):
    for item in (*result.required, *result.recommended):
        if item.key == key:
            return item
    raise KeyError(key)


# ----------------------------------------------------------------------
# Verdicts
# ----------------------------------------------------------------------


def test_complete_plink_style_header_is_ready() -> None:
    result = assess(("CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P", "N"))
    assert result.verdict is ReadinessVerdict.READY
    assert result.required_satisfied == len(PRS_REQUIRED)
    assert "Suitable for downstream PRS preparation" in result.decision


def test_gwas_ssf_mandatory_fields_are_ready() -> None:
    result = assess(SSF_MANDATORY_FIELDS)
    assert result.verdict is ReadinessVerdict.READY


def test_harmonised_catalog_header_is_ready() -> None:
    header = (
        "hm_variant_id",
        "hm_rsid",
        "hm_chrom",
        "hm_pos",
        "hm_other_allele",
        "hm_effect_allele",
        "hm_beta",
        "hm_odds_ratio",
        "hm_effect_allele_frequency",
        "variant_id",
        "chromosome",
        "base_pair_location",
        "effect_allele",
        "other_allele",
        "effect_allele_frequency",
        "beta",
        "standard_error",
        "p_value",
    )
    result = assess(header)
    assert result.verdict is ReadinessVerdict.READY


def test_missing_other_allele_is_partial() -> None:
    result = assess(("SNP", "A1", "OR", "P"))
    assert result.verdict is ReadinessVerdict.PARTIAL
    assert "other allele" in result.missing_required


def test_missing_most_requirements_is_not_ready() -> None:
    result = assess(("rsid", "beta"))
    assert result.verdict is ReadinessVerdict.NOT_READY
    assert "effect allele" in result.missing_required
    assert "p-value" in result.missing_required


def test_completely_unrecognised_header_is_not_ready() -> None:
    result = assess(("col_a", "col_b", "col_c"))
    assert result.verdict is ReadinessVerdict.NOT_READY
    assert len(result.unmapped_columns) == 3


# ----------------------------------------------------------------------
# Individual requirements
# ----------------------------------------------------------------------


def test_chromosome_plus_position_satisfies_identification() -> None:
    """An rsID is not required if the locus is fully specified."""
    result = assess(
        ("chromosome", "base_pair_location", "effect_allele", "other_allele", "beta", "p_value")
    )
    item = requirement(result, "variant_identification")
    assert item.is_satisfied
    assert set(item.canonical_concepts) == {"chromosome", "position"}


def test_variant_id_alone_satisfies_identification() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P"))
    assert requirement(result, "variant_identification").is_satisfied


def test_chromosome_without_position_does_not_satisfy_identification() -> None:
    result = assess(("CHR", "A1", "A2", "BETA", "P"))
    assert requirement(result, "variant_identification").status is RequirementStatus.MISSING


def test_combined_chrpos_field_satisfies_identification() -> None:
    result = assess(("chr_pos", "A1", "A2", "BETA", "P"))
    assert requirement(result, "variant_identification").is_satisfied


@pytest.mark.parametrize("effect", ["BETA", "OR", "hazard_ratio"])
def test_directly_usable_effect_measures_satisfy_the_requirement(effect) -> None:
    """Beta is usable as-is; OR and HR after a deterministic log transform."""
    result = assess(("SNP", "A1", "A2", effect, "P"))
    assert requirement(result, "effect_size").is_satisfied


def test_z_score_alone_does_not_satisfy_the_effect_requirement() -> None:
    """A z-score is a test statistic (beta / se), not an effect size.

    Recovering a weight from it needs the per-variant sample size and an allele
    frequency: se ~= 1 / sqrt(2 * N * f * (1 - f)), beta ~= Z * se. Without
    those the column cannot yield PRS weights at all, so it must not be
    reported as satisfying the effect-size requirement on its own.
    """
    result = assess(("SNP", "A1", "A2", "Z", "P"))
    item = requirement(result, "effect_size")
    assert not item.is_satisfied
    assert result.verdict is not ReadinessVerdict.READY
    assert "test statistic" in (item.note or "")


def test_neg_log10_p_satisfies_significance() -> None:
    """It is a valid significance measure -- but not a p-value (see the warning)."""
    result = assess(("SNP", "A1", "A2", "BETA", "neg_log_10_p_value"))
    assert requirement(result, "significance").is_satisfied
    assert result.verdict is ReadinessVerdict.READY


def test_heuristic_mapping_yields_uncertain_not_satisfied() -> None:
    """A column mapped by heuristic is weaker evidence, and says so."""
    result = assess(("SNP", "A1", "A2", "BETA", "some_analyte_pval"))
    item = requirement(result, "significance")
    assert item.status is RequirementStatus.UNCERTAIN
    assert result.verdict is ReadinessVerdict.PARTIAL


def test_recommended_fields_do_not_block_ready() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P"))
    assert result.verdict is ReadinessVerdict.READY
    assert result.recommended_satisfied == 0
    assert "rules out methods" in result.decision


def test_all_recommended_fields_satisfied() -> None:
    header = ("SNP", "A1", "A2", "BETA", "P", "SE", "N", "EAF", "INFO")
    result = assess(header)
    assert result.recommended_satisfied == len(PRS_RECOMMENDED)
    assert result.verdict is ReadinessVerdict.READY


# ----------------------------------------------------------------------
# Scientific warnings
# ----------------------------------------------------------------------


def test_odds_ratio_warns_about_the_log_transform() -> None:
    result = assess(("SNP", "A1", "A2", "OR", "SE", "P"))
    assert any("natural log" in w for w in result.warnings)


def test_beta_present_suppresses_the_odds_ratio_warning() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "OR", "SE", "P"))
    assert not any("natural log" in w for w in result.warnings)


def test_z_score_only_warns_about_the_conversion_requirements() -> None:
    result = assess(("SNP", "A1", "A2", "z_score", "P"))
    assert any("z-score" in w and "allele frequency" in w for w in result.warnings)


def test_neg_log10_p_warns_about_the_scale() -> None:
    """Silently treating -log10(p) as p inverts every threshold."""
    result = assess(("SNP", "A1", "A2", "BETA", "neg_log_10_p_value"))
    assert any("-log10(p)" in w for w in result.warnings)


def test_maf_without_eaf_warns() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P", "MAF"))
    assert any("population-dependent" in w for w in result.warnings)


def test_combined_chrpos_warns_it_must_be_split() -> None:
    result = assess(("chr_pos", "A1", "A2", "BETA", "P"))
    assert any("must be split" in w for w in result.warnings)


def test_duplicate_columns_are_warned_about() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P", "P"))
    assert any("Duplicate column names" in w for w in result.warnings)


def test_heuristic_mappings_are_listed_in_warnings() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "ala_pval"))
    assert any("heuristic" in w for w in result.warnings)


# ----------------------------------------------------------------------
# Declared-field assessment (the API-sufficient branch)
# ----------------------------------------------------------------------


def test_assess_from_declared_fields_matches_the_probed_result() -> None:
    """The two routes must produce the same verdict from the same field set.

    This is the invariant behind `assess --force-probe`: a GWAS-SSF declaration
    and an observed header of those same columns must agree.
    """
    declared = assess_from_declared_fields(SSF_MANDATORY_FIELDS)
    observed = assess(SSF_MANDATORY_FIELDS)
    assert declared.verdict is observed.verdict
    assert declared.required_satisfied == observed.required_satisfied


def test_declared_assessment_records_that_no_bytes_were_read() -> None:
    result = assess_from_declared_fields(SSF_MANDATORY_FIELDS, note="the data file was not read")
    assert result.evidence_source == "gwas_ssf_metadata"
    assert any("not read" in note for note in result.notes)


# ----------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------


def test_assessment_serialises() -> None:
    payload = assess(("CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P")).to_dict()
    assert payload["verdict"] == "READY"
    assert payload["required_satisfied"] == "5/5"
    assert len(payload["required"]) == 5
    assert payload["required"][0]["status"] == "satisfied"


def test_requirement_status_symbols() -> None:
    assert RequirementStatus.SATISFIED.symbol == "✓"
    assert RequirementStatus.UNCERTAIN.symbol == "?"
    assert RequirementStatus.MISSING.symbol == "✗"


def test_multiple_columns_mapping_to_one_concept_are_named() -> None:
    """BOLT-LMM output carries three p-value columns side by side.

    The real file GCST90038646_buildGRCh37.tsv has P_LINREG, P_BOLT_LMM_INF and
    p_value. Silently picking one hides a scientific choice from the analyst.
    """
    result = assess(("SNP", "A1", "A2", "BETA", "P_LINREG", "P_BOLT_LMM_INF", "p_value"))
    warning = next(w for w in result.warnings if "map to p_value" in w)
    assert "3 columns" in warning
    assert "P_LINREG" in warning
    assert "'p_value' was used" in warning
    assert result.verdict is ReadinessVerdict.READY


def test_single_column_per_concept_produces_no_such_warning() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P"))
    assert not any("map to p_value" in w for w in result.warnings)


def test_bolt_lmm_p_values_map_to_p_value() -> None:
    """v1 put p_bolt_lmm_inf in beta_list; both are p-values."""
    from gwaspoker.mapping.mapper import get_mapper

    mapper = get_mapper()
    for column in ("P_LINREG", "P_BOLT_LMM_INF", "p_bolt_lmm"):
        assert mapper.map_column(column).canonical_name == "p_value"


# ----------------------------------------------------------------------
# Z-score: a test statistic, not an effect size
# ----------------------------------------------------------------------


def test_z_with_sample_size_and_frequency_satisfies_the_effect_requirement() -> None:
    """se ~= 1/sqrt(2*N*f*(1-f)), beta ~= Z*se -- both companions are needed."""
    result = assess(("SNP", "A1", "A2", "Z", "P", "N", "EAF"))
    item = requirement(result, "effect_size")
    assert item.is_satisfied
    assert set(item.canonical_concepts) == {"z_score", "sample_size", "effect_allele_frequency"}
    assert result.verdict is ReadinessVerdict.READY


@pytest.mark.parametrize("frequency", ["EAF", "MAF", "freq"])
def test_any_allele_frequency_concept_can_support_a_z_score(frequency) -> None:
    result = assess(("SNP", "A1", "A2", "Z", "P", "N", frequency))
    assert requirement(result, "effect_size").is_satisfied


def test_z_with_sample_size_but_no_frequency_is_not_ready() -> None:
    result = assess(("SNP", "A1", "A2", "Z", "P", "N"))
    assert not requirement(result, "effect_size").is_satisfied
    assert result.verdict is not ReadinessVerdict.READY


def test_z_with_frequency_but_no_sample_size_is_not_ready() -> None:
    result = assess(("SNP", "A1", "A2", "Z", "P", "EAF"))
    assert not requirement(result, "effect_size").is_satisfied
    assert result.verdict is not ReadinessVerdict.READY


def test_beta_alongside_z_is_used_directly() -> None:
    """A usable beta makes the z-score's companions irrelevant."""
    result = assess(("SNP", "A1", "A2", "BETA", "Z", "P"))
    item = requirement(result, "effect_size")
    assert item.is_satisfied
    assert "beta" in item.canonical_concepts


def test_z_only_explains_why_it_is_insufficient() -> None:
    result = assess(("SNP", "A1", "A2", "Z", "P"))
    note = requirement(result, "effect_size").note or ""
    assert "sample size" in note
    assert "allele frequency" in note


# ----------------------------------------------------------------------
# Sample size: one arm is not a total
# ----------------------------------------------------------------------


def test_cases_alone_does_not_satisfy_sample_size() -> None:
    """A case count is not a sample size."""
    result = assess(("SNP", "A1", "A2", "BETA", "P", "n_cases"))
    assert not requirement(result, "sample_size").is_satisfied


def test_controls_alone_does_not_satisfy_sample_size() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P", "n_controls"))
    assert not requirement(result, "sample_size").is_satisfied


def test_cases_and_controls_together_satisfy_sample_size_as_derived() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P", "n_cases", "n_controls"))
    item = requirement(result, "sample_size")
    assert item.is_satisfied
    assert set(item.canonical_concepts) == {"cases", "controls"}
    # The report must say the total was derived, not observed.
    assert "DERIVED" in (item.note or "")


def test_explicit_n_is_not_reported_as_derived() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P", "N"))
    item = requirement(result, "sample_size")
    assert item.is_satisfied
    assert item.canonical_concepts == ("sample_size",)
    assert "DERIVED" not in (item.note or "")


def test_explicit_n_is_preferred_over_the_derived_total() -> None:
    result = assess(("SNP", "A1", "A2", "BETA", "P", "N", "n_cases", "n_controls"))
    item = requirement(result, "sample_size")
    assert item.canonical_concepts == ("sample_size",)
