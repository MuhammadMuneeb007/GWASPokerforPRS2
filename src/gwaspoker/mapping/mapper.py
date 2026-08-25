"""Layered mapping from raw column names to canonical GWAS concepts.

Three layers, tried in order, each less certain than the last:

Layer 1 -- **exact canonical name**. The column already *is* the canonical name
(``p_value``, ``effect_allele``). Confidence 1.0.

Layer 2 -- **curated alias**. The normalized name appears in ``aliases.yaml``.
Confidence 0.95.

Layer 3 -- **heuristic**. A ``hm_`` prefix, a ``_pval`` suffix, or an
unambiguous substring. Confidence 0.6-0.9, always reported as such.

A column that no layer resolves is mapped to ``unknown``. That is the whole
point: a forced mapping is a scientific error, an ``unknown`` is a prompt for a
human to look. v1 instead reported the same column under two concepts at once
(``a1`` was in both allele lists) and never signalled uncertainty at all.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gwaspoker.mapping.normalize import normalize_column_name

logger = logging.getLogger(__name__)

#: Value used when no layer produced a confident mapping.
UNKNOWN_CONCEPT = "unknown"

ALIASES_PATH = Path(__file__).with_name("aliases.yaml")


class MappingMethod(str):
    """How a mapping was established."""

    CANONICAL = "canonical"
    ALIAS = "alias"
    #: A curated alias whose header name is genuinely ambiguous ("ID", "ALT").
    #: It maps, but at reduced confidence, and the value validator is expected
    #: to confirm or challenge it.
    AMBIGUOUS_ALIAS = "ambiguous_alias"
    HEURISTIC = "heuristic"
    NONE = "unknown"


#: Confidence for a curated alias whose name does not, on its own, establish
#: the concept. High enough to be useful, low enough that
#: `readiness` treats it as uncertain without supporting value evidence.
AMBIGUOUS_ALIAS_CONFIDENCE = 0.75


@dataclass(frozen=True)
class ColumnConcept:
    """One canonical concept from ``aliases.yaml``."""

    name: str
    description: str
    prs_tool_symbol: str
    category: str
    aliases: tuple[str, ...]
    #: Aliases that map here but whose header name is weak evidence.
    ambiguous_aliases: tuple[str, ...] = ()
    #: True when the concept itself carries a convention caveat (ALT/REF).
    ambiguous: bool = False
    ambiguity_note: str = ""


@dataclass
class ColumnMapping:
    """The result of mapping one raw column name."""

    raw_name: str
    normalized_name: str
    canonical_name: str
    mapping_method: str
    confidence: float
    prs_tool_symbol: Optional[str] = None
    note: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        return self.canonical_name != UNKNOWN_CONCEPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_name": self.raw_name,
            "normalized_name": self.normalized_name,
            "canonical_name": self.canonical_name,
            "mapping_method": self.mapping_method,
            "confidence": round(self.confidence, 3),
            "prs_tool_symbol": self.prs_tool_symbol,
            "note": self.note,
        }


@dataclass
class MappingResult:
    """Mapping of a whole header, with order preserved."""

    columns: tuple[ColumnMapping, ...] = ()
    duplicates: tuple[str, ...] = ()

    @property
    def resolved(self) -> tuple[ColumnMapping, ...]:
        return tuple(c for c in self.columns if c.is_resolved)

    @property
    def unresolved(self) -> tuple[ColumnMapping, ...]:
        return tuple(c for c in self.columns if not c.is_resolved)

    def by_concept(self) -> dict[str, list[ColumnMapping]]:
        """Concept name to the columns that mapped to it, in header order."""
        out: dict[str, list[ColumnMapping]] = {}
        for column in self.resolved:
            out.setdefault(column.canonical_name, []).append(column)
        return out

    def concepts(self) -> set[str]:
        return {c.canonical_name for c in self.resolved}

    def first_for(self, concept: str) -> Optional[ColumnMapping]:
        """Highest-confidence column mapped to ``concept``, ties broken by order."""
        matches = [c for c in self.columns if c.canonical_name == concept]
        if not matches:
            return None
        return max(matches, key=lambda c: c.confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": [c.to_dict() for c in self.columns],
            "resolved_count": len(self.resolved),
            "unresolved_count": len(self.unresolved),
            "unidentified_columns": [c.raw_name for c in self.unresolved],
            "duplicate_columns": list(self.duplicates),
        }


@dataclass
class _Heuristics:
    suffix: tuple[tuple[str, str, float], ...] = ()
    prefix: tuple[tuple[str, bool, float], ...] = ()
    contains: tuple[tuple[str, str, float], ...] = ()


class ColumnMapper:
    """Maps raw header names onto the canonical vocabulary."""

    def __init__(self, aliases_path: Optional[Path] = None) -> None:
        self.concepts, self._alias_index, self._heuristics = _load_vocabulary(
            aliases_path or ALIASES_PATH
        )

    # -- single column ---------------------------------------------------

    def map_column(self, raw_name: str) -> ColumnMapping:
        """Map one raw column name through the three layers."""
        normalized = normalize_column_name(raw_name)
        if not normalized:
            return ColumnMapping(
                raw_name=raw_name,
                normalized_name="",
                canonical_name=UNKNOWN_CONCEPT,
                mapping_method=MappingMethod.NONE,
                confidence=0.0,
                note="empty column name",
            )

        # Layer 1: the column is already canonical.
        if normalized in self.concepts:
            concept = self.concepts[normalized]
            return ColumnMapping(
                raw_name=raw_name,
                normalized_name=normalized,
                canonical_name=concept.name,
                mapping_method=MappingMethod.CANONICAL,
                confidence=1.0,
                prs_tool_symbol=concept.prs_tool_symbol,
            )

        # Layer 2: curated alias.
        concept_name = self._alias_index.get(normalized)
        if concept_name:
            concept = self.concepts[concept_name]
            ambiguous = normalized in concept.ambiguous_aliases or concept.ambiguous
            return ColumnMapping(
                raw_name=raw_name,
                normalized_name=normalized,
                canonical_name=concept.name,
                mapping_method=(
                    MappingMethod.AMBIGUOUS_ALIAS if ambiguous else MappingMethod.ALIAS
                ),
                confidence=AMBIGUOUS_ALIAS_CONFIDENCE if ambiguous else 0.95,
                prs_tool_symbol=concept.prs_tool_symbol,
                note=(
                    (
                        (concept.ambiguity_note or "").strip()
                        or (
                            f"{raw_name!r} is a generic column name; the header alone is "
                            "weak evidence for this concept"
                        )
                    )
                    if ambiguous
                    else None
                ),
            )

        # Layer 3: heuristics.
        heuristic = self._apply_heuristics(normalized)
        if heuristic is not None:
            concept_name, confidence, note = heuristic
            concept = self.concepts[concept_name]
            return ColumnMapping(
                raw_name=raw_name,
                normalized_name=normalized,
                canonical_name=concept.name,
                mapping_method=MappingMethod.HEURISTIC,
                confidence=confidence,
                prs_tool_symbol=concept.prs_tool_symbol,
                note=note,
            )

        return ColumnMapping(
            raw_name=raw_name,
            normalized_name=normalized,
            canonical_name=UNKNOWN_CONCEPT,
            mapping_method=MappingMethod.NONE,
            confidence=0.0,
            note="no canonical concept, curated alias or heuristic matched",
        )

    def _apply_heuristics(self, normalized: str) -> Optional[tuple[str, float, str]]:
        # Harmonised GWAS Catalog columns: strip "hm_" and retry Layers 1-2.
        for prefix, strip, confidence in self._heuristics.prefix:
            if normalized.startswith(prefix) and strip:
                stripped = normalized[len(prefix) :]
                if stripped in self.concepts:
                    return stripped, confidence, f"matched after stripping {prefix!r} prefix"
                target = self._alias_index.get(stripped)
                if target:
                    return target, confidence, f"matched after stripping {prefix!r} prefix"

        for suffix, concept_name, confidence in self._heuristics.suffix:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                return concept_name, confidence, f"matched {suffix!r} suffix pattern"

        for needle, concept_name, confidence in self._heuristics.contains:
            if needle in normalized:
                return concept_name, confidence, f"contains {needle!r}"

        return None

    # -- whole header ----------------------------------------------------

    def map_header(self, header: Iterable[str]) -> MappingResult:
        """Map a header row, preserving order and reporting duplicates."""
        header = list(header)
        mappings = tuple(self.map_column(name) for name in header)

        seen: dict[str, int] = {}
        duplicates: list[str] = []
        for name in header:
            key = normalize_column_name(name)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 2:
                duplicates.append(name)

        return MappingResult(columns=mappings, duplicates=tuple(duplicates))

    # -- introspection ---------------------------------------------------

    def concept_names(self) -> tuple[str, ...]:
        return tuple(self.concepts)

    def aliases_for(self, concept: str) -> tuple[str, ...]:
        entry = self.concepts.get(concept)
        return entry.aliases if entry else ()

    def all_aliases(self) -> dict[str, str]:
        """Every curated alias to its concept. Used by the alias-integrity tests."""
        return dict(self._alias_index)


def _load_vocabulary(path: Path) -> tuple[dict[str, ColumnConcept], dict[str, str], _Heuristics]:
    """Read ``aliases.yaml`` into lookup structures."""
    import yaml

    if not path.is_file():
        raise FileNotFoundError(f"Alias vocabulary not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "concepts" not in data:
        raise ValueError(f"{path} is not a valid GWASPoker alias vocabulary")

    concepts: dict[str, ColumnConcept] = {}
    alias_index: dict[str, str] = {}
    conflicts: list[str] = []

    for name, entry in (data.get("concepts") or {}).items():
        if not isinstance(entry, dict):
            raise ValueError(f"Concept {name!r} in {path} is not a mapping")
        ambiguous_aliases = tuple(
            normalize_column_name(a) for a in (entry.get("ambiguous_aliases") or [])
        )
        aliases = (
            tuple(normalize_column_name(a) for a in (entry.get("aliases") or []))
            + ambiguous_aliases
        )
        concepts[name] = ColumnConcept(
            name=name,
            description=str(entry.get("description", "")).strip(),
            prs_tool_symbol=str(entry.get("prs_tool_symbol", "")).strip(),
            category=str(entry.get("category", "")).strip(),
            aliases=aliases,
            ambiguous_aliases=ambiguous_aliases,
            ambiguous=bool(entry.get("ambiguous", False)),
            ambiguity_note=str(entry.get("ambiguity_note", "")).strip(),
        )
        for alias in aliases:
            if not alias:
                continue
            existing = alias_index.get(alias)
            if existing and existing != name:
                conflicts.append(f"{alias!r} claimed by both {existing!r} and {name!r}")
            alias_index[alias] = name

    if conflicts:
        # A conflict means the same column would map to two concepts, which is
        # exactly the v1 defect this rewrite exists to remove. Fail loudly.
        raise ValueError("Ambiguous aliases in " + str(path) + ": " + "; ".join(sorted(conflicts)))

    raw_heuristics = data.get("heuristics") or {}
    heuristics = _Heuristics(
        suffix=tuple(
            (normalize_column_name(h["suffix"]), h["concept"], float(h.get("confidence", 0.6)))
            for h in raw_heuristics.get("suffix", [])
            if h.get("concept") in concepts
        ),
        prefix=tuple(
            (
                normalize_column_name(h["prefix"]) + "_",
                bool(h.get("strip")),
                float(h.get("confidence", 0.6)),
            )
            for h in raw_heuristics.get("prefix", [])
        ),
        contains=tuple(
            (normalize_column_name(h["contains"]), h["concept"], float(h.get("confidence", 0.6)))
            for h in raw_heuristics.get("contains", [])
            if h.get("concept") in concepts
        ),
    )

    logger.debug(
        "Loaded %d canonical concepts and %d aliases from %s",
        len(concepts),
        len(alias_index),
        path,
    )
    return concepts, alias_index, heuristics


@functools.lru_cache(maxsize=4)
def get_mapper(aliases_path: Optional[str] = None) -> ColumnMapper:
    """Return a shared :class:`ColumnMapper`, parsing the YAML once per process."""
    return ColumnMapper(Path(aliases_path) if aliases_path else None)
