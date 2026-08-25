"""Layered extraction of sample size, case count and control count.

Priority 1 -- **structured API**. The Catalog's ancestry records carry
``numberOfIndividuals`` per stage; a GWAS-SSF sidecar carries ``sample_size``
and sometimes ``case_count``/``control_count``. Provenance: ``structured_api``
or ``ssf_metadata``.

Priority 2 -- **deterministic text extraction**. Sample descriptions follow a
small number of recurring shapes. Provenance: ``regex``.

Priority 3 -- **optional local QA model** (``--llm``). ELECTRA question
answering over the description. Off by default, never trusted as truth, always
recorded as ``llm`` with the model's own confidence attached.

Each of the three numbers is resolved independently, so a description that
states cases and controls but not a total does not block the total from being
derived, and a value from the API is never overwritten by a regex guess.

v1 ran priority 3 only, three times per study, rebuilding a 335 M-parameter
model on every call, and gated case/control extraction on the description
containing both the words "case" and "control".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from gwaspoker.catalog.models import SampleCounts, SsfMetadata, Study, ValueSource

logger = logging.getLogger(__name__)

#: An integer with optional thousands separators: 12345, 12,345, 12 345.
_NUMBER = r"(\d{1,3}(?:[,   ]\d{3})+|\d+)"


def parse_count(text: str) -> Optional[int]:
    """Parse an integer that may carry thousands separators.

    Unlike v1's ``extract_number``, this does **not** strip decimal points
    (which turned ``0.75`` into ``075``), does not fall back to the first digits
    it can find anywhere in the string, and returns ``None`` rather than ``0``
    when it cannot parse -- so a failure is never mistaken for a real zero.

    >>> parse_count("12,345")
    12345
    >>> parse_count("about 40")
    40
    >>> parse_count("1.5e-8") is None
    True
    """
    if text is None:
        return None
    candidate = str(text).strip()
    if not candidate:
        return None
    # Reject anything that is really a decimal or scientific-notation value.
    if re.search(r"\d[.eE]\d|\d[eE][+-]\d", candidate):
        return None
    match = re.search(_NUMBER, candidate)
    if not match:
        return None
    digits = re.sub(r"[,   ]", "", match.group(1))
    return int(digits) if digits.isdigit() else None


# --------------------------------------------------------------------------
# Priority 2: deterministic patterns
# --------------------------------------------------------------------------

#: Words the Catalog uses between a count and the word "cases"/"controls",
#: e.g. "14,131 European ancestry cases".
_QUALIFIER = r"(?:[A-Za-z][A-Za-z\-']*\s+){0,6}"

_CASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_NUMBER}\s+{_QUALIFIER}cases?\b", re.IGNORECASE),
    re.compile(rf"\bcases?\s*[:=]\s*{_NUMBER}", re.IGNORECASE),
    re.compile(rf"\bn[\s_]*cases?\s*[:=]?\s*{_NUMBER}", re.IGNORECASE),
    re.compile(
        rf"{_NUMBER}\s+{_QUALIFIER}(?:patients|affected individuals|probands)\b", re.IGNORECASE
    ),
)

_CONTROL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_NUMBER}\s+{_QUALIFIER}controls?\b", re.IGNORECASE),
    re.compile(rf"\bcontrols?\s*[:=]\s*{_NUMBER}", re.IGNORECASE),
    re.compile(rf"\bn[\s_]*controls?\s*[:=]?\s*{_NUMBER}", re.IGNORECASE),
    re.compile(
        rf"{_NUMBER}\s+{_QUALIFIER}(?:unaffected|healthy)\s+(?:individuals|participants|subjects)\b",
        re.IGNORECASE,
    ),
)

_TOTAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\bn\s*[:=]\s*{_NUMBER}", re.IGNORECASE),
    re.compile(rf"\b(?:total\s+)?sample\s+size\s*[:=]?\s*{_NUMBER}", re.IGNORECASE),
    re.compile(
        rf"{_NUMBER}\s+{_QUALIFIER}"
        r"(?:individuals|participants|subjects|samples|people|men|women|"
        r"males|females|children|adults|twins|sibling pairs)\b",
        re.IGNORECASE,
    ),
)

#: Patterns that mean "this number is NOT a sample count".
_NEGATIVE_CONTEXT = re.compile(
    r"\b(?:snps?|variants?|markers?|loci|genes?|probes?|associations?|"
    r"years?|percent|%|kb|mb|chromosomes?)\b",
    re.IGNORECASE,
)


def _search_all(patterns: tuple[re.Pattern[str], ...], text: str) -> list[int]:
    """Every count matched by ``patterns``, skipping negative contexts."""
    found: list[int] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 30) : match.end() + 30]
            if _NEGATIVE_CONTEXT.search(window[match.start() - max(0, match.start() - 30) :]):
                continue
            value = parse_count(match.group(1))
            if value is not None and value > 0:
                found.append(value)
    return found


@dataclass
class TextExtraction:
    """Counts recovered from free text, with a confidence for each."""

    total: Optional[int] = None
    cases: Optional[int] = None
    controls: Optional[int] = None
    total_confidence: float = 0.0
    cases_confidence: float = 0.0
    controls_confidence: float = 0.0


def extract_counts_from_text(text: Optional[str]) -> TextExtraction:
    """Extract N, cases and controls from a sample description.

    Handles the shapes that actually occur in the Catalog:

    >>> e = extract_counts_from_text("12,345 cases and 45,678 controls")
    >>> e.cases, e.controls
    (12345, 45678)
    >>> extract_counts_from_text("n=62,000").total
    62000
    >>> extract_counts_from_text("62,000 participants").total
    62000
    >>> e = extract_counts_from_text("5,100 cases; 11,400 controls")
    >>> e.cases, e.controls
    (5100, 11400)
    >>> e = extract_counts_from_text("14,131 European ancestry cases, 317,623 European ancestry controls")
    >>> e.cases, e.controls
    (14131, 317623)
    """
    result = TextExtraction()
    if not text:
        return result
    text = str(text)

    cases = _search_all(_CASE_PATTERNS, text)
    controls = _search_all(_CONTROL_PATTERNS, text)
    totals = _search_all(_TOTAL_PATTERNS, text)

    if cases:
        # Sum the parts of a multi-cohort description ("1,000 UK cases,
        # 2,000 Finnish cases"), but only when the parts are distinct values.
        result.cases = sum(dict.fromkeys(cases)) if len(set(cases)) > 1 else cases[0]
        result.cases_confidence = 0.9 if len(set(cases)) == 1 else 0.7
    if controls:
        result.controls = sum(dict.fromkeys(controls)) if len(set(controls)) > 1 else controls[0]
        result.controls_confidence = 0.9 if len(set(controls)) == 1 else 0.7

    if totals:
        # Prefer an explicit total over one implied by summing cohorts.
        result.total = max(totals)
        result.total_confidence = 0.85
    elif result.cases is not None and result.controls is not None:
        result.total = result.cases + result.controls
        result.total_confidence = min(result.cases_confidence, result.controls_confidence) * 0.95

    return result


# --------------------------------------------------------------------------
# The layered resolver
# --------------------------------------------------------------------------


class SampleSizeResolver:
    """Resolves N / cases / controls through the three priority layers."""

    def __init__(self, *, enable_llm: bool = False, llm_model: Optional[str] = None) -> None:
        self.enable_llm = enable_llm
        self.llm_model = llm_model

    def resolve(
        self,
        study: Study,
        *,
        ssf_metadata: Optional[SsfMetadata] = None,
    ) -> SampleCounts:
        """Fill and return ``study.samples``, resolving each number independently."""
        counts = study.samples or SampleCounts()

        self._from_structured(counts, study, ssf_metadata)
        if not self._complete(counts, study):
            self._from_text(counts, study)
        if self.enable_llm and not self._complete(counts, study):
            self._from_llm(counts, study)

        study.samples = counts
        return counts

    # -- Priority 1 ------------------------------------------------------

    def _from_structured(
        self,
        counts: SampleCounts,
        study: Study,
        ssf: Optional[SsfMetadata],
    ) -> None:
        """Structured fields from the REST API and the GWAS-SSF sidecar."""
        if counts.total is None:
            discovery = study.discovery_sample_size
            if discovery is not None and discovery > 0:
                counts.total = discovery
                counts.total_source = ValueSource.STRUCTURED_API
                counts.total_confidence = 1.0

        if ssf is not None:
            if counts.total is None and ssf.sample_size:
                counts.total = ssf.sample_size
                counts.total_source = ValueSource.SSF_METADATA
                counts.total_confidence = 1.0
            if counts.cases is None and ssf.case_count:
                counts.cases = ssf.case_count
                counts.cases_source = ValueSource.SSF_METADATA
                counts.cases_confidence = 1.0
            if counts.controls is None and ssf.control_count:
                counts.controls = ssf.control_count
                counts.controls_source = ValueSource.SSF_METADATA
                counts.controls_confidence = 1.0

    # -- Priority 2 ------------------------------------------------------

    def _from_text(self, counts: SampleCounts, study: Study) -> None:
        """Deterministic regex extraction over the description fields."""
        sources = [
            study.initial_sample_description,
            study.replication_sample_description,
            study.publication_title,
        ]
        for text in sources:
            if not text:
                continue
            extracted = extract_counts_from_text(text)
            if counts.cases is None and extracted.cases is not None:
                counts.cases = extracted.cases
                counts.cases_source = ValueSource.REGEX
                counts.cases_confidence = extracted.cases_confidence
            if counts.controls is None and extracted.controls is not None:
                counts.controls = extracted.controls
                counts.controls_source = ValueSource.REGEX
                counts.controls_confidence = extracted.controls_confidence
            if counts.total is None and extracted.total is not None:
                counts.total = extracted.total
                counts.total_source = ValueSource.REGEX
                counts.total_confidence = extracted.total_confidence
            if self._complete(counts, study):
                return

        # Derive a total from a case/control pair if still missing.
        if counts.total is None and counts.is_case_control:
            counts.total = (counts.cases or 0) + (counts.controls or 0)
            counts.total_source = ValueSource.DERIVED
            counts.total_confidence = min(
                counts.cases_confidence or 0.0, counts.controls_confidence or 0.0
            )

    # -- Priority 3 ------------------------------------------------------

    def _from_llm(self, counts: SampleCounts, study: Study) -> None:
        """Optional ELECTRA question-answering fallback."""
        from gwaspoker.metadata.llm_extractor import extract_counts_with_qa

        context = " ".join(
            part
            for part in (
                study.initial_sample_description,
                study.replication_sample_description,
            )
            if part
        ).strip()
        if not context:
            return

        answers = extract_counts_with_qa(context, model_name=self.llm_model)
        if answers is None:
            return

        if counts.cases is None and answers.cases is not None:
            counts.cases = answers.cases
            counts.cases_source = ValueSource.LLM
            counts.cases_confidence = answers.cases_confidence
        if counts.controls is None and answers.controls is not None:
            counts.controls = answers.controls
            counts.controls_source = ValueSource.LLM
            counts.controls_confidence = answers.controls_confidence
        if counts.total is None and answers.total is not None:
            counts.total = answers.total
            counts.total_source = ValueSource.LLM
            counts.total_confidence = answers.total_confidence

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _complete(counts: SampleCounts, study: Study) -> bool:
        """Have we learned everything this study can offer?

        A study that is not case/control is complete once the total is known;
        v1 instead required the words "case" and "control" to be present before
        it would look for either, so quantitative-trait studies were never
        examined and case/control studies were never checked for a total.
        """
        if counts.total is None:
            return False
        description = (study.initial_sample_description or "").lower()
        mentions_case_control = "case" in description or "control" in description
        if not mentions_case_control:
            return True
        return counts.is_case_control
