"""Ancestry normalization and population matching.

Ancestry matters for PRS: a score derived in one ancestry group transfers poorly
to another, so a user filtering by ``--population European`` is making a
scientific choice, not a cosmetic one.

The Catalog uses a controlled vocabulary of ancestry categories (the framework
described in Morales et al. 2018, PMC5815218). This module maps user input onto
that vocabulary and scores how well a study matches, rather than running
``fuzz.token_sort_ratio`` at a fixed threshold of 50 as v1 did -- a threshold at
which "European" and "African American or Afro-Caribbean" score high enough to
be indistinguishable from noise.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: GWAS Catalog ancestry categories, each with the user-facing terms that mean it.
ANCESTRY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "European": ("european", "eur", "caucasian", "white", "ceu", "eu"),
    "East Asian": ("east asian", "eas", "chinese", "japanese", "korean", "han", "asian east"),
    "South Asian": ("south asian", "sas", "indian", "pakistani", "bangladeshi", "sri lankan"),
    "South East Asian": ("south east asian", "southeast asian", "sea", "malay", "thai", "filipino"),
    "African unspecified": ("african", "afr", "african unspecified"),
    "Sub-Saharan African": ("sub-saharan african", "sub saharan african", "west african", "yoruba"),
    "African American or Afro-Caribbean": (
        "african american",
        "afro-caribbean",
        "afro caribbean",
        "black american",
        "aa",
    ),
    "Hispanic or Latin American": (
        "hispanic",
        "latino",
        "latin american",
        "latina",
        "amr",
        "admixed american",
    ),
    "Greater Middle Eastern (Middle Eastern, North African or Persian)": (
        "middle eastern",
        "north african",
        "persian",
        "iranian",
        "arab",
        "gme",
    ),
    "Oceanian": ("oceanian", "polynesian", "melanesian", "maori", "pacific islander"),
    "Native American": ("native american", "american indian", "amerindian", "indigenous american"),
    "Aboriginal Australian": ("aboriginal australian", "aboriginal", "australian aboriginal"),
    "Other admixed ancestry": (
        "admixed",
        "mixed",
        "multi-ancestry",
        "multiancestry",
        "trans-ethnic",
    ),
    "Other": ("other",),
    "NR": ("nr", "not reported", "unspecified", "unknown"),
}

#: Reverse index built once at import.
_TERM_TO_CATEGORY: dict[str, str] = {
    term: category for category, terms in ANCESTRY_CATEGORIES.items() for term in terms
}
_TERM_TO_CATEGORY.update({category.lower(): category for category in ANCESTRY_CATEGORIES})


@dataclass
class AncestryMatch:
    """How well a study's ancestries match a requested population."""

    matched: bool
    score: float
    matched_category: Optional[str] = None
    requested_category: Optional[str] = None
    reason: str = ""


def normalize_ancestry(text: Optional[str]) -> Optional[str]:
    """Map free text onto a GWAS Catalog ancestry category.

    >>> normalize_ancestry("European")
    'European'
    >>> normalize_ancestry("caucasian")
    'European'
    >>> normalize_ancestry("EAS")
    'East Asian'
    >>> normalize_ancestry("not a population") is None
    True
    """
    if not text:
        return None
    key = " ".join(str(text).strip().lower().split())
    if not key:
        return None
    if key in _TERM_TO_CATEGORY:
        return _TERM_TO_CATEGORY[key]
    # Longest containing term wins, so "african american" beats "african".
    matches = [term for term in _TERM_TO_CATEGORY if term in key]
    if matches:
        return _TERM_TO_CATEGORY[max(matches, key=len)]
    return None


def match_population(
    requested: Optional[str],
    study_ancestries: Iterable[str],
) -> AncestryMatch:
    """Score how well a study's ancestries satisfy a requested population.

    Scores are interpretable rather than arbitrary:

    ``1.0``   exact category match
    ``0.75``  the study is multi-ancestry and includes the requested category
    ``0.4``   the study's ancestry is unreported, so it *might* match
    ``0.0``   a definite mismatch
    """
    if not requested:
        return AncestryMatch(True, 1.0, reason="no population filter requested")

    requested_category = normalize_ancestry(requested)
    groups = [g for g in study_ancestries if g]
    if not groups:
        return AncestryMatch(
            False,
            0.4,
            requested_category=requested_category,
            reason="study reports no ancestry information",
        )

    normalized = [(g, normalize_ancestry(g)) for g in groups]
    categories = [c for _, c in normalized if c]

    if requested_category is None:
        # The user typed something outside the vocabulary; fall back to a
        # substring test, and say so.
        needle = str(requested).strip().lower()
        for raw, _ in normalized:
            if needle in raw.lower():
                return AncestryMatch(
                    True, 0.6, matched_category=raw, reason=f"substring match on {raw!r}"
                )
        return AncestryMatch(
            False, 0.0, reason=f"{requested!r} is not a recognised ancestry category"
        )

    if requested_category in categories:
        multi = len({c for c in categories if c != "NR"}) > 1
        return AncestryMatch(
            True,
            0.75 if multi else 1.0,
            matched_category=requested_category,
            requested_category=requested_category,
            reason=(
                "multi-ancestry study including the requested category"
                if multi
                else "exact ancestry category match"
            ),
        )

    if categories and all(c == "NR" for c in categories):
        return AncestryMatch(
            False,
            0.4,
            requested_category=requested_category,
            reason="ancestry not reported by the study; a match cannot be excluded",
        )

    return AncestryMatch(
        False,
        0.0,
        requested_category=requested_category,
        reason=f"study ancestry is {', '.join(sorted(set(categories))) or 'unclassified'}",
    )


def summarize_ancestries(study_ancestries: Iterable[str]) -> str:
    """Compact, de-duplicated ancestry label for a table cell."""
    categories: list[str] = []
    for group in study_ancestries:
        category = normalize_ancestry(group) or group
        if category and category not in categories:
            categories.append(category)
    if not categories:
        return "unknown"
    if len(categories) > 2:
        return f"{categories[0]} +{len(categories) - 1} more"
    return ", ".join(categories)
