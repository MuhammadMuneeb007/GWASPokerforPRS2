"""Tests for the canonical column vocabulary and the layered mapper.

The alias-integrity tests exist because the v1 alias lists shipped five aliases
that had been silently corrupted by missing commas between adjacent string
literals, and several aliases that appeared under two different concepts at
once. Both classes of defect are now impossible to reintroduce without a test
failing.
"""

from __future__ import annotations

import pytest
import yaml

from gwaspoker.mapping.mapper import ALIASES_PATH, UNKNOWN_CONCEPT, ColumnMapper, get_mapper
from gwaspoker.mapping.normalize import (
    is_probably_data_row,
    looks_like_missing,
    normalize_column_name,
    normalize_header,
)

# ----------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("P-Value", "p_value"),
        ("p.value", "p_value"),
        ("P_VALUE", "p_value"),
        ("  P Value  ", "p_value"),
        ('"OR(A1)"', "or_a1"),
        ("effect-allele", "effect_allele"),
        ("#CHROM", "chrom"),
        ("﻿chromosome", "chromosome"),
        ("base__pair___location", "base_pair_location"),
        ("", ""),
    ],
)
def test_normalize_column_name(raw: str, expected: str) -> None:
    assert normalize_column_name(raw) == expected


def test_normalize_header_preserves_order_and_length() -> None:
    header = ("CHR", "POS", "SNP", "CHR")
    normalized = normalize_header(header)
    assert normalized == ("chr", "pos", "snp", "chr")
    assert len(normalized) == len(header)


def test_is_probably_data_row() -> None:
    assert is_probably_data_row(["1", "12345", "rs1", "A", "G", "0.12", "0.03", "1e-6"])
    assert not is_probably_data_row(["CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P"])
    assert not is_probably_data_row([])


def test_looks_like_missing_covers_ssf_na_marker() -> None:
    # GWAS-SSF specifies '#NA' as the omitted-value marker.
    for token in ("#NA", "NA", "nan", ".", "-", ""):
        assert looks_like_missing(token)
    assert not looks_like_missing("0")


# ----------------------------------------------------------------------
# Alias vocabulary integrity
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def vocabulary() -> dict:
    return yaml.safe_load(ALIASES_PATH.read_text(encoding="utf-8"))


def test_vocabulary_loads_and_has_required_concepts(mapper: ColumnMapper) -> None:
    required = {
        "chromosome",
        "position",
        "variant_id",
        "effect_allele",
        "other_allele",
        "effect_allele_frequency",
        "minor_allele_frequency",
        "beta",
        "odds_ratio",
        "standard_error",
        "p_value",
        "sample_size",
        "cases",
        "controls",
        "info_score",
        "z_score",
        "direction",
    }
    missing = required - set(mapper.concept_names())
    assert not missing, f"aliases.yaml is missing required concepts: {sorted(missing)}"


def test_every_concept_is_fully_specified(vocabulary: dict) -> None:
    for name, entry in vocabulary["concepts"].items():
        assert entry.get("description"), f"{name} has no description"
        assert entry.get("category"), f"{name} has no category"
        assert entry.get("prs_tool_symbol"), f"{name} has no prs_tool_symbol"
        assert entry.get("aliases"), f"{name} has no aliases"


def test_aliases_are_unique_across_concepts(mapper: ColumnMapper) -> None:
    """An alias must belong to exactly one concept.

    v1 listed ``a1``, ``allele1`` and ``allele_1`` in both the effect-allele and
    the alternative-allele lists, so the same column was reported as both.
    :func:`_load_vocabulary` raises on a conflict, so reaching this assertion at
    all means the file loaded cleanly; the assertion pins the invariant.
    """
    aliases = mapper.all_aliases()
    for concept in mapper.concept_names():
        for alias in mapper.aliases_for(concept):
            assert aliases[alias] == concept


def test_aliases_are_unique_within_a_concept(vocabulary: dict) -> None:
    for name, entry in vocabulary["concepts"].items():
        normalized = [normalize_column_name(a) for a in entry["aliases"]]
        duplicates = {a for a in normalized if normalized.count(a) > 1}
        assert not duplicates, f"{name} repeats alias(es): {sorted(duplicates)}"


#: Aliases that legitimately read as two joined aliases because the source
#: column name simply omits separators. Every entry here has been checked by
#: hand against real summary-statistics files. Anything NOT on this list that
#: decomposes into two known aliases is treated as a missing-comma artefact.
LEGITIMATE_COMPOUND_ALIASES = frozenset(
    {
        "effectallelefrequency",
        "effectallelefreq",
        "imputationinfo",
        "minorallelefrequency",
        "oddsratiominorallele",
    }
)


def test_no_suspicious_concatenated_aliases(mapper: ColumnMapper) -> None:
    """No alias may be an unvetted concatenation of two other known aliases.

    This is the exact signature of v1's missing-comma defect, which produced
    ``neaalt_allele``, ``namechromosome_position_reference_allele_other_allele_b37``,
    ``effect_allele_allminorallele``, ``noncoded_allelenoneffect_allele`` and
    ``neg_log_10_p_valuep``.

    Genuine separator-free compounds do occur (``effectallelefrequency``), so
    they are allow-listed by name in :data:`LEGITIMATE_COMPOUND_ALIASES`. Adding
    an entry there is the review moment: it forces someone to confirm that the
    string really is a column name and not two aliases that lost their comma.
    """
    aliases = set(mapper.all_aliases())
    substantial = {a for a in aliases if len(a) >= 4}
    offenders = []
    for alias in aliases:
        if len(alias) < 12 or alias in LEGITIMATE_COMPOUND_ALIASES:
            continue
        for left in substantial:
            if not alias.startswith(left) or left == alias:
                continue
            if alias[len(left) :] in substantial:
                offenders.append(f"{alias!r} = {left!r} + {alias[len(left):]!r}")
    assert not offenders, (
        "Aliases that look like missing-comma concatenations (add to "
        "LEGITIMATE_COMPOUND_ALIASES only after verifying each against a real file): "
        + "; ".join(sorted(offenders))
    )


def test_compound_allowlist_has_no_stale_entries(mapper: ColumnMapper) -> None:
    """Every allow-listed compound must still be a live alias."""
    aliases = set(mapper.all_aliases())
    stale = LEGITIMATE_COMPOUND_ALIASES - aliases
    assert (
        not stale
    ), f"LEGITIMATE_COMPOUND_ALIASES lists aliases that no longer exist: {sorted(stale)}"


def test_known_v1_corruptions_are_absent(mapper: ColumnMapper) -> None:
    """The five corrupted aliases from v1 must not have been copied across."""
    aliases = set(mapper.all_aliases())
    for corrupt in (
        "namechromosome_position_reference_allele_other_allele_b37",
        "effect_allele_allminorallele",
        "neaalt_allele",
        "noncoded_allelenoneffect_allele",
        "neg_log_10_p_valuep",
    ):
        assert corrupt not in aliases


def test_dangerous_fragment_aliases_are_absent(mapper: ColumnMapper) -> None:
    """Single-token fragments from v1 that match unrelated columns."""
    aliases = set(mapper.all_aliases())
    for fragment in ("e", "_value", "pair", "base", "standard", "_error", "odd", "odds"):
        assert fragment not in aliases, f"{fragment!r} is too ambiguous to be an alias"


def test_no_alias_is_a_concept_of_another_name(mapper: ColumnMapper) -> None:
    """An alias must not shadow a different concept's canonical name."""
    concepts = set(mapper.concept_names())
    for alias, concept in mapper.all_aliases().items():
        if alias in concepts:
            assert alias == concept, f"alias {alias!r} shadows the concept of the same name"


def test_aliases_normalize_reproducibly(mapper: ColumnMapper) -> None:
    """Normalizing an already-normalized alias is a no-op."""
    for alias in mapper.all_aliases():
        assert normalize_column_name(alias) == alias


# ----------------------------------------------------------------------
# Layered mapping
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "concept", "method"),
    [
        ("p_value", "p_value", "canonical"),
        ("chromosome", "chromosome", "canonical"),
        ("CHR", "chromosome", "alias"),
        ("BP", "position", "alias"),
        ("A1", "effect_allele", "alias"),
        ("A2", "other_allele", "alias"),
        ("MarkerName", "variant_id", "alias"),
        ("SE", "standard_error", "alias"),
        ("StdErr", "standard_error", "alias"),
        ("N", "sample_size", "alias"),
        ("hm_beta", "beta", "alias"),
        ("Effect", "beta", "alias"),
        ("OR", "odds_ratio", "alias"),
        ("z-score", "z_score", "canonical"),
    ],
)
def test_layer_1_and_2_mappings(mapper: ColumnMapper, raw, concept, method) -> None:
    result = mapper.map_column(raw)
    assert result.canonical_name == concept
    assert result.mapping_method == method
    assert result.confidence >= 0.95


def test_layer_3_suffix_heuristic(mapper: ColumnMapper) -> None:
    """The ~250 '<analyte>_pval' columns v1 enumerated are covered by one rule."""
    for column in ("ala_pval", "xxl_vldl_tg_pval", "acace_pval"):
        result = mapper.map_column(column)
        assert result.canonical_name == "p_value"
        assert result.mapping_method == "heuristic"
        assert 0.5 < result.confidence < 0.95


def test_layer_3_harmonised_prefix(mapper: ColumnMapper) -> None:
    result = mapper.map_column("hm_effect_allele_frequency")
    assert result.canonical_name == "effect_allele_frequency"


def test_unknown_columns_are_not_forced(mapper: ColumnMapper) -> None:
    for column in ("some_internal_id", "qc_flag_v3", "notes"):
        result = mapper.map_column(column)
        assert result.canonical_name == UNKNOWN_CONCEPT
        assert result.confidence == 0.0
        assert result.note


def test_empty_column_name(mapper: ColumnMapper) -> None:
    result = mapper.map_column("   ")
    assert result.canonical_name == UNKNOWN_CONCEPT
    assert result.note == "empty column name"


# ----------------------------------------------------------------------
# Corrected v1 semantics
# ----------------------------------------------------------------------


def test_neg_log10_p_is_not_mapped_to_p_value(mapper: ColumnMapper) -> None:
    """v1 aliased neg_log_10_p_value to p_value, inverting the significance scale."""
    result = mapper.map_column("neg_log_10_p_value")
    assert result.canonical_name == "neg_log10_p_value"
    assert result.canonical_name != "p_value"


def test_bolt_lmm_p_value_is_not_mapped_to_beta(mapper: ColumnMapper) -> None:
    """v1 listed p_bolt_lmm_inf in beta_list; it is a p-value."""
    assert mapper.map_column("p_bolt_lmm_inf").canonical_name == "p_value"
    assert mapper.map_column("p_bolt_lmm").canonical_name == "p_value"


def test_case_and_control_counts_are_not_sample_size(mapper: ColumnMapper) -> None:
    """v1's N_list mixed total N with case and control counts."""
    assert mapper.map_column("n_cases").canonical_name == "cases"
    assert mapper.map_column("n_controls").canonical_name == "controls"
    assert mapper.map_column("n").canonical_name == "sample_size"


def test_a1_maps_to_effect_allele_only(mapper: ColumnMapper) -> None:
    """v1 had a1/allele1 in both allele lists simultaneously."""
    assert mapper.map_column("a1").canonical_name == "effect_allele"
    assert mapper.map_column("allele1").canonical_name == "effect_allele"
    assert mapper.map_column("a0").canonical_name == "other_allele"
    assert mapper.map_column("allele0").canonical_name == "other_allele"


def test_minor_allele_frequency_is_not_an_allele(mapper: ColumnMapper) -> None:
    """v1 listed minorallelefrequency under effect_allele_list -- it is a frequency."""
    assert mapper.map_column("minorallelefrequency").canonical_name != "effect_allele"


def test_minor_allele_frequency_is_not_effect_allele_frequency(
    mapper: ColumnMapper,
) -> None:
    """MAF and EAF are different quantities and must not share a concept.

    Which allele is *minor* is population-dependent; which allele carries the
    *effect* is a property of the analysis. Conflating them silently swaps one
    for the other in any downstream step that needs the effect allele's
    frequency. LDSC's own alias table maps both MAF and EAF onto a single FRQ
    column, which is exactly the conflation to avoid here.
    """
    for spelling in (
        "minorallelefrequency",
        "minor_allele_frequency",
        "minor_allele_freq",
        "minorallelefreq",
        "MAF",
    ):
        assert mapper.map_column(spelling).canonical_name == "minor_allele_frequency"

    for spelling in ("effect_allele_frequency", "EAF", "effect_allele_freq"):
        assert mapper.map_column(spelling).canonical_name == "effect_allele_frequency"


def test_weight_is_not_a_sample_size(mapper: ColumnMapper) -> None:
    """v1's N_list contained 'weight', a meta-analysis weight."""
    assert mapper.map_column("weight").canonical_name == UNKNOWN_CONCEPT


# ----------------------------------------------------------------------
# Header mapping
# ----------------------------------------------------------------------


def test_map_header_preserves_order(mapper: ColumnMapper) -> None:
    header = ("P", "CHR", "BP", "A1")
    result = mapper.map_header(header)
    assert tuple(c.raw_name for c in result.columns) == header
    assert [c.canonical_name for c in result.columns] == [
        "p_value",
        "chromosome",
        "position",
        "effect_allele",
    ]


def test_map_header_reports_duplicates(mapper: ColumnMapper) -> None:
    result = mapper.map_header(("CHR", "BP", "P", "P"))
    assert result.duplicates == ("P",)
    assert len(result.columns) == 4  # duplicates are kept, not collapsed


def test_map_header_reports_unresolved(mapper: ColumnMapper) -> None:
    result = mapper.map_header(("CHR", "BP", "internal_flag"))
    assert [c.raw_name for c in result.unresolved] == ["internal_flag"]
    assert result.to_dict()["unidentified_columns"] == ["internal_flag"]


def test_first_for_prefers_higher_confidence(mapper: ColumnMapper) -> None:
    # 'chromosome' is canonical (1.0); 'CHR' is a curated alias (0.95).
    result = mapper.map_header(("CHR", "chromosome"))
    best = result.first_for("chromosome")
    assert best.raw_name == "chromosome"
    assert best.confidence == 1.0


def test_mapper_is_cached_per_process() -> None:
    assert get_mapper() is get_mapper()


# ----------------------------------------------------------------------
# Ambiguous-alias audit
# ----------------------------------------------------------------------


def test_alt_and_ref_do_not_assert_effect_orientation(mapper: ColumnMapper) -> None:
    """ALT/REF are VCF coordinate conventions, not effect statements.

    ALT is simply the non-reference allele at the site. Many GWAS files do use
    it as the effect allele, but that is a per-source convention. LDSC maps
    REFERENCE_ALLELE to A1 (the *effect* allele) while VCF semantics would put
    REF on the other side -- the disagreement is exactly why GWASPoker refuses
    to resolve it from the header alone.
    """
    alt = mapper.map_column("ALT")
    ref = mapper.map_column("REF")

    assert alt.canonical_name == "alternate_allele"
    assert ref.canonical_name == "reference_allele"
    assert alt.canonical_name != "effect_allele"
    assert ref.canonical_name != "other_allele"
    for mapping in (alt, ref):
        assert mapping.mapping_method == "ambiguous_alias"
        assert mapping.confidence < 0.95
        assert mapping.note


def test_a1_and_a2_keep_full_confidence(mapper: ColumnMapper) -> None:
    """A1/A2 are near-universal in PRS tooling, so they stay authoritative."""
    assert mapper.map_column("A1").canonical_name == "effect_allele"
    assert mapper.map_column("A2").canonical_name == "other_allele"
    assert mapper.map_column("A1").confidence == 0.95
    assert mapper.map_column("A2").confidence == 0.95


@pytest.mark.parametrize("generic", ["ID", "NAME", "MARKER"])
def test_generic_identifier_aliases_are_downgraded(mapper: ColumnMapper, generic) -> None:
    """They stay usable, but the header alone is weak evidence."""
    mapping = mapper.map_column(generic)
    assert mapping.canonical_name == "variant_id"
    assert mapping.mapping_method == "ambiguous_alias"
    assert mapping.confidence < 0.95


def test_specific_identifier_aliases_keep_full_confidence(mapper: ColumnMapper) -> None:
    for specific in ("rsid", "variant_id", "MarkerName", "snpid"):
        assert mapper.map_column(specific).confidence >= 0.95


def test_ambiguous_aliases_still_resolve(mapper: ColumnMapper) -> None:
    """Downgrading must not make common columns unusable."""
    for alias in ("ID", "NAME", "ALT", "REF", "MARKER"):
        assert mapper.map_column(alias).canonical_name != UNKNOWN_CONCEPT
