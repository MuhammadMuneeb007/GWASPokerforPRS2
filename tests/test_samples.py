"""Sample size, case and control extraction, and ancestry matching.

The layering matters: a structured API value must never be overwritten by a
regex guess, and a regex value must never be overwritten by the QA model. Each
number carries its own provenance so a reader can tell them apart -- v1 mixed
extracted numbers and a ``"-"`` sentinel into the same column.
"""

from __future__ import annotations

import pytest

from gwaspoker.catalog.models import Ancestry, SampleCounts, SsfMetadata, Study, ValueSource
from gwaspoker.metadata.ancestry import (
    match_population,
    normalize_ancestry,
    summarize_ancestries,
)
from gwaspoker.metadata.samples import (
    SampleSizeResolver,
    extract_counts_from_text,
    parse_count,
)

# ----------------------------------------------------------------------
# parse_count
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12,345", 12345),
        ("12345", 12345),
        ("about 40 people", 40),
        ("1 000 000", 1000000),
        ("", None),
        ("no numbers here", None),
    ],
)
def test_parse_count(text, expected) -> None:
    assert parse_count(text) == expected


def test_parse_count_rejects_decimals_and_scientific_notation() -> None:
    """v1's extract_number stripped '.' and ',', turning 0.75 into 075."""
    assert parse_count("1.5e-8") is None
    assert parse_count("0.75") is None
    assert parse_count("3.14") is None


def test_parse_count_returns_none_not_zero_on_failure() -> None:
    """v1 returned the integer 0, making a parse error look like a real zero."""
    assert parse_count("n/a") is None
    assert parse_count(None) is None


# ----------------------------------------------------------------------
# Deterministic text extraction
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cases", "controls", "total"),
    [
        ("12,345 cases and 45,678 controls", 12345, 45678, 58023),
        ("5,100 cases; 11,400 controls", 5100, 11400, 16500),
        ("13,971 cases, 470,627 controls", 13971, 470627, 484598),
        (
            "14,131 European ancestry cases, 317,623 European ancestry controls",
            14131,
            317623,
            331754,
        ),
        ("cases: 200, controls: 800", 200, 800, 1000),
    ],
)
def test_case_control_extraction(text, cases, controls, total) -> None:
    result = extract_counts_from_text(text)
    assert result.cases == cases
    assert result.controls == controls
    assert result.total == total


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("n=62,000", 62000),
        ("N = 62000", 62000),
        ("62,000 participants", 62000),
        ("484,598 individuals", 484598),
        ("sample size: 1234", 1234),
        ("331,754 European ancestry individuals", 331754),
    ],
)
def test_total_extraction(text, expected) -> None:
    assert extract_counts_from_text(text).total == expected


def test_snp_counts_are_not_mistaken_for_sample_size() -> None:
    """A negative-context guard: 7,000,000 SNPs is not a sample size."""
    result = extract_counts_from_text("up to 7,000,000 SNPs in 1,094,154 individuals")
    assert result.total == 1094154


def test_multi_cohort_case_counts_are_summed() -> None:
    text = "127 Danish ancestry chronic migraine cases, 926 Danish ancestry episodic migraine cases"
    result = extract_counts_from_text(text)
    assert result.cases == 127 + 926
    assert result.controls is None


def test_quantitative_trait_description_yields_a_total_only() -> None:
    """v1 required both 'case' and 'control' to be present before looking."""
    result = extract_counts_from_text("69,039 European ancestry individuals")
    assert result.total == 69039
    assert result.cases is None


def test_empty_text() -> None:
    result = extract_counts_from_text("")
    assert result.total is None
    assert result.cases is None
    assert extract_counts_from_text(None).total is None


def test_extraction_reports_confidence() -> None:
    result = extract_counts_from_text("12,345 cases and 45,678 controls")
    assert 0 < result.cases_confidence <= 1.0
    assert 0 < result.total_confidence <= 1.0


# ----------------------------------------------------------------------
# Layered resolution
# ----------------------------------------------------------------------


def _study(**kwargs) -> Study:
    defaults = {"study_accession": "GCST000001"}
    defaults.update(kwargs)
    return Study(**defaults)


def test_structured_api_takes_priority() -> None:
    study = _study(
        ancestries=(
            Ancestry(stage="initial", number_of_individuals=484598, ancestral_groups=("European",)),
        ),
        initial_sample_description="99 cases, 99 controls",
    )
    counts = SampleSizeResolver().resolve(study)
    assert counts.total == 484598
    assert counts.total_source is ValueSource.STRUCTURED_API
    assert counts.total_confidence == 1.0


def test_ssf_metadata_supplies_counts_when_the_api_does_not() -> None:
    study = _study()
    meta = SsfMetadata(url="", sample_size=123456, case_count=12345, control_count=111111)
    counts = SampleSizeResolver().resolve(study, ssf_metadata=meta)
    assert counts.total == 123456
    assert counts.cases == 12345
    assert counts.total_source is ValueSource.SSF_METADATA
    assert counts.cases_source is ValueSource.SSF_METADATA


def test_regex_fallback_when_no_structured_value() -> None:
    study = _study(initial_sample_description="13,971 cases, 470,627 controls")
    counts = SampleSizeResolver().resolve(study)
    assert counts.cases == 13971
    assert counts.controls == 470627
    assert counts.cases_source is ValueSource.REGEX
    assert counts.total_source in (ValueSource.REGEX, ValueSource.DERIVED)


def test_structured_total_coexists_with_regex_case_counts() -> None:
    """Each number resolves independently."""
    study = _study(
        ancestries=(Ancestry(stage="initial", number_of_individuals=484598),),
        initial_sample_description="13,971 cases, 470,627 controls",
    )
    counts = SampleSizeResolver().resolve(study)
    assert counts.total_source is ValueSource.STRUCTURED_API
    assert counts.cases_source is ValueSource.REGEX
    assert counts.total == 484598
    assert counts.cases == 13971


def test_unresolvable_counts_stay_unknown() -> None:
    """No value is invented, and no plausible-looking zero is returned."""
    study = _study(initial_sample_description="A study of an interesting phenotype")
    counts = SampleSizeResolver().resolve(study)
    assert counts.total is None
    assert counts.total_source is ValueSource.UNKNOWN
    assert not counts.resolved


def test_llm_is_off_by_default(monkeypatch) -> None:
    """The QA model must not load unless explicitly enabled."""
    called = []

    def _boom(*args, **kwargs):
        called.append(True)
        raise AssertionError("the LLM fallback ran without being enabled")

    monkeypatch.setattr("gwaspoker.metadata.llm_extractor.extract_counts_with_qa", _boom)
    study = _study(initial_sample_description="something with no numbers at all")
    SampleSizeResolver(enable_llm=False).resolve(study)
    assert not called


def test_llm_result_is_marked_as_llm(monkeypatch) -> None:
    from gwaspoker.metadata.llm_extractor import QaCounts

    def _fake(context, **kwargs):
        return QaCounts(total=5000, total_confidence=0.61)

    monkeypatch.setattr("gwaspoker.metadata.llm_extractor.extract_counts_with_qa", _fake)
    study = _study(initial_sample_description="a cohort of unspecified magnitude")
    counts = SampleSizeResolver(enable_llm=True).resolve(study)
    assert counts.total == 5000
    assert counts.total_source is ValueSource.LLM
    assert counts.total_confidence == 0.61


def test_llm_never_overwrites_a_structured_value(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("the LLM ran even though the API had already answered")

    monkeypatch.setattr("gwaspoker.metadata.llm_extractor.extract_counts_with_qa", _boom)
    study = _study(
        ancestries=(Ancestry(stage="initial", number_of_individuals=1000),),
        initial_sample_description="one thousand people",
    )
    counts = SampleSizeResolver(enable_llm=True).resolve(study)
    assert counts.total == 1000
    assert counts.total_source is ValueSource.STRUCTURED_API


# ----------------------------------------------------------------------
# SampleCounts model
# ----------------------------------------------------------------------


def test_implied_total_from_cases_and_controls() -> None:
    counts = SampleCounts(cases=100, controls=900)
    assert counts.implied_total() == 1000
    assert counts.is_case_control


def test_implied_total_is_none_when_nothing_is_known() -> None:
    assert SampleCounts().implied_total() is None


def test_sample_counts_serialise_with_provenance() -> None:
    payload = SampleCounts(total=10, total_source=ValueSource.REGEX).to_dict()
    assert payload["sample_size"] == 10
    assert payload["sample_size_source"] == "regex"
    assert payload["cases"] is None
    assert payload["cases_source"] == "unknown"


# ----------------------------------------------------------------------
# Ancestry
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("European", "European"),
        ("european", "European"),
        ("Caucasian", "European"),
        ("EAS", "East Asian"),
        ("East Asian", "East Asian"),
        ("African American", "African American or Afro-Caribbean"),
        ("Hispanic", "Hispanic or Latin American"),
        ("NR", "NR"),
        ("", None),
        ("not a population at all", None),
    ],
)
def test_normalize_ancestry(text, expected) -> None:
    assert normalize_ancestry(text) == expected


def test_longest_matching_term_wins() -> None:
    """'African American' must not be swallowed by 'African'."""
    assert normalize_ancestry("African American or Afro-Caribbean") == (
        "African American or Afro-Caribbean"
    )


def test_match_population_exact() -> None:
    result = match_population("European", ["European"])
    assert result.matched
    assert result.score == 1.0


def test_match_population_multi_ancestry_scores_lower() -> None:
    result = match_population("European", ["European", "East Asian"])
    assert result.matched
    assert result.score == 0.75


def test_match_population_mismatch() -> None:
    result = match_population("European", ["East Asian"])
    assert not result.matched
    assert result.score == 0.0


def test_unreported_ancestry_is_not_excluded_outright() -> None:
    """'Not reported' is not the same as 'does not match'."""
    result = match_population("European", ["NR"])
    assert not result.matched
    assert result.score == 0.4
    assert "not reported" in result.reason


def test_no_filter_matches_everything() -> None:
    result = match_population(None, ["East Asian"])
    assert result.matched
    assert result.score == 1.0


def test_summarize_ancestries() -> None:
    assert summarize_ancestries(["European"]) == "European"
    assert summarize_ancestries([]) == "unknown"
    assert summarize_ancestries(["European", "East Asian"]) == "European, East Asian"
    # Three or more collapse to a count so the table cell stays readable.
    assert summarize_ancestries(["European", "East Asian", "African"]) == "European +2 more"
    # Duplicates that normalize to the same category are counted once.
    assert summarize_ancestries(["European", "european", "Caucasian"]) == "European"
