"""Normalized internal data model.

Nothing outside :mod:`gwaspoker.catalog.rest_api` and
:mod:`gwaspoker.catalog.sumstats_api` should ever touch raw catalogue JSON.
Those adapters produce the objects defined here; everything downstream consumes
them. When the upstream schema changes, only the adapters change.

Absent information is ``None`` or :data:`UNKNOWN`. It is never invented, and it
is never a plausible-looking default such as ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

#: The string used wherever a value could not be established.
UNKNOWN = "unknown"


class ValueSource(str, Enum):
    """Where a field's value came from. Recorded for every derived value."""

    STRUCTURED_API = "structured_api"
    SSF_METADATA = "ssf_metadata"
    SUMSTATS_API = "sumstats_api"
    SOLR_INDEX = "solr_index"
    REGEX = "regex"
    LLM = "llm"
    FILE_PROBE = "file_probe"
    LOCAL_FILE = "local_file"
    GWASLAB = "gwaslab"
    DERIVED = "derived"
    USER = "user"
    UNKNOWN = "unknown"


class ApiAvailability(str, Enum):
    """Why an API route did or did not yield data.

    ``DEPRECATED`` and ``NOT_REPRESENTED`` are deliberately distinct from
    ``SERVER_ERROR`` and ``TIMEOUT``. A single failed request must never be
    reported as "the API does not have this study".
    """

    AVAILABLE = "available"
    NOT_REPRESENTED = "not_represented"
    DEPRECATED = "deprecated"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    INVALID_ACCESSION = "invalid_accession"
    SCHEMA_ERROR = "schema_error"
    NOT_QUERIED = "not_queried"


@dataclass
class Provenance:
    """Where one field's value came from, and how much to trust it."""

    source: ValueSource = ValueSource.UNKNOWN
    detail: Optional[str] = None
    confidence: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "detail": self.detail,
            "confidence": self.confidence,
        }


@dataclass
class SampleCounts:
    """Total N, cases and controls, each with independent provenance.

    The original implementation returned these as bare numbers with a ``"-"``
    sentinel mixed into the same column, so an unparsed value and a real value
    were indistinguishable downstream. Here each number carries its own source.
    """

    total: Optional[int] = None
    cases: Optional[int] = None
    controls: Optional[int] = None
    total_source: ValueSource = ValueSource.UNKNOWN
    cases_source: ValueSource = ValueSource.UNKNOWN
    controls_source: ValueSource = ValueSource.UNKNOWN
    total_confidence: Optional[float] = None
    cases_confidence: Optional[float] = None
    controls_confidence: Optional[float] = None

    @property
    def is_case_control(self) -> bool:
        return self.cases is not None and self.controls is not None

    @property
    def resolved(self) -> bool:
        """True once at least a total or a case/control pair is known."""
        return self.total is not None or self.is_case_control

    def implied_total(self) -> Optional[int]:
        """Total N, falling back to cases + controls when only those are known."""
        if self.total is not None:
            return self.total
        if self.is_case_control:
            return (self.cases or 0) + (self.controls or 0)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_size": self.total,
            "sample_size_source": self.total_source.value,
            "sample_size_confidence": self.total_confidence,
            "cases": self.cases,
            "cases_source": self.cases_source.value,
            "cases_confidence": self.cases_confidence,
            "controls": self.controls,
            "controls_source": self.controls_source.value,
            "controls_confidence": self.controls_confidence,
        }


@dataclass
class EfoTrait:
    """An EFO / MONDO / OBA ontology term as the Catalog reports it."""

    label: str
    efo_id: Optional[str] = None
    uri: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "efo_id": self.efo_id, "uri": self.uri}


@dataclass
class Ancestry:
    """One ancestry block from the Catalog's structured ancestry records."""

    stage: str  # "initial" | "replication" | UNKNOWN
    number_of_individuals: Optional[int] = None
    ancestral_groups: tuple[str, ...] = ()
    countries_of_recruitment: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "number_of_individuals": self.number_of_individuals,
            "ancestral_groups": list(self.ancestral_groups),
            "countries_of_recruitment": list(self.countries_of_recruitment),
        }


@dataclass
class Study:
    """One GWAS Catalog study, normalized.

    Field names are GWASPoker's own; the mapping from each upstream schema is
    confined to the adapter that produced it.
    """

    study_accession: str
    reported_trait: Optional[str] = None
    mapped_traits: tuple[EfoTrait, ...] = ()
    background_traits: tuple[EfoTrait, ...] = ()

    pubmed_id: Optional[str] = None
    publication_title: Optional[str] = None
    publication_journal: Optional[str] = None
    publication_date: Optional[str] = None
    first_author: Optional[str] = None
    study_year: Optional[int] = None

    summary_statistics_available: Optional[bool] = None
    summary_statistics_location: Optional[str] = None

    ancestries: tuple[Ancestry, ...] = ()
    initial_sample_description: Optional[str] = None
    replication_sample_description: Optional[str] = None
    samples: SampleCounts = field(default_factory=SampleCounts)

    genome_build: Optional[str] = None
    genotyping_technologies: tuple[str, ...] = ()
    cohorts: tuple[str, ...] = ()
    association_count: Optional[int] = None
    snp_count: Optional[int] = None

    api_source: str = UNKNOWN
    provenance: dict[str, Provenance] = field(default_factory=dict)
    raw: Optional[dict[str, Any]] = field(default=None, repr=False)

    # -- convenience views -------------------------------------------------

    @property
    def mapped_trait_label(self) -> str:
        """Comma-joined mapped-trait labels, or :data:`UNKNOWN`."""
        return ", ".join(t.label for t in self.mapped_traits) or UNKNOWN

    @property
    def discovery_ancestry(self) -> str:
        """Ancestral groups of the initial (discovery) stage."""
        groups: list[str] = []
        for anc in self.ancestries:
            if anc.stage == "initial":
                groups.extend(anc.ancestral_groups)
        if not groups:
            groups = [g for anc in self.ancestries for g in anc.ancestral_groups]
        seen: list[str] = []
        for g in groups:
            if g not in seen:
                seen.append(g)
        return ", ".join(seen) or UNKNOWN

    @property
    def discovery_sample_size(self) -> Optional[int]:
        """Structured individual count for the discovery stage, if published."""
        totals = [
            a.number_of_individuals
            for a in self.ancestries
            if a.stage == "initial" and a.number_of_individuals is not None
        ]
        return sum(totals) if totals else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "study_accession": self.study_accession,
            "reported_trait": self.reported_trait,
            "mapped_trait": self.mapped_trait_label if self.mapped_traits else None,
            "efo_traits": [t.to_dict() for t in self.mapped_traits],
            "pubmed_id": self.pubmed_id,
            "publication_title": self.publication_title,
            "publication_journal": self.publication_journal,
            "publication_date": self.publication_date,
            "first_author": self.first_author,
            "study_year": self.study_year,
            "summary_statistics_available": self.summary_statistics_available,
            "summary_statistics_location": self.summary_statistics_location,
            "ancestries": [a.to_dict() for a in self.ancestries],
            "discovery_ancestry": self.discovery_ancestry,
            "initial_sample_description": self.initial_sample_description,
            "replication_sample_description": self.replication_sample_description,
            "genome_build": self.genome_build,
            "genotyping_technologies": list(self.genotyping_technologies),
            "cohorts": list(self.cohorts),
            "association_count": self.association_count,
            "snp_count": self.snp_count,
            "api_source": self.api_source,
        }
        data.update(self.samples.to_dict())
        data["provenance"] = {k: v.to_dict() for k, v in self.provenance.items()}
        return data


@dataclass
class SsfMetadata:
    """Contents of a GWAS-SSF ``-meta.yaml`` sidecar.

    This file is the structured, authoritative description of a summary
    statistics file, roughly 700 bytes, published alongside it. It is the reason
    GWASPoker can often reach a PRS verdict without reading the data file at
    all: when ``file_type`` is ``GWAS-SSF v1.0`` the mandatory column set is
    fixed by the standard.
    """

    url: str
    data_file_name: Optional[str] = None
    file_type: Optional[str] = None
    genome_assembly: Optional[str] = None
    coordinate_system: Optional[str] = None
    is_harmonised: Optional[bool] = None
    is_sorted: Optional[bool] = None
    md5sum: Optional[str] = None
    sample_size: Optional[int] = None
    case_control_study: Optional[bool] = None
    case_count: Optional[int] = None
    control_count: Optional[int] = None
    ancestry_categories: tuple[str, ...] = ()
    trait_description: tuple[str, ...] = ()
    analysis_software: Optional[str] = None
    bytes_received: int = 0
    latency_seconds: float = 0.0
    raw: Optional[dict[str, Any]] = field(default=None, repr=False)

    @property
    def is_ssf(self) -> bool:
        """True when the file declares conformance to GWAS-SSF v1.x."""
        return bool(self.file_type and self.file_type.strip().upper().startswith("GWAS-SSF"))

    @property
    def ssf_status(self) -> str:
        """``GWAS-SSF`` / ``pre-GWAS-SSF`` / :data:`UNKNOWN` for stratification."""
        if not self.file_type:
            return UNKNOWN
        return "GWAS-SSF" if self.is_ssf else "pre-GWAS-SSF"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "data_file_name": self.data_file_name,
            "file_type": self.file_type,
            "ssf_status": self.ssf_status,
            "genome_assembly": self.genome_assembly,
            "coordinate_system": self.coordinate_system,
            "is_harmonised": self.is_harmonised,
            "is_sorted": self.is_sorted,
            "md5sum": self.md5sum,
            "sample_size": self.sample_size,
            "case_control_study": self.case_control_study,
            "case_count": self.case_count,
            "control_count": self.control_count,
            "ancestry_categories": list(self.ancestry_categories),
            "trait_description": list(self.trait_description),
            "analysis_software": self.analysis_software,
            "bytes_received": self.bytes_received,
            "latency_seconds": round(self.latency_seconds, 4),
        }


@dataclass
class ApiAssessment:
    """Result of trying to settle PRS readiness from structured sources alone.

    ``sufficient_for_prs_assessment`` is the branch point of the whole tool:
    when it is true, ``gwaspoker assess`` returns without transferring a single
    byte of the data file.
    """

    study_accession: Optional[str] = None
    available: bool = False
    availability: ApiAvailability = ApiAvailability.NOT_QUERIED
    sufficient_for_prs_assessment: bool = False
    route: str = UNKNOWN
    fields_available: tuple[str, ...] = ()
    associations_requested: int = 0
    bytes_received: int = 0
    latency_seconds: float = 0.0
    endpoints_tried: tuple[str, ...] = ()
    ssf_metadata: Optional[SsfMetadata] = None
    error: Optional[str] = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_accession": self.study_accession,
            "available": self.available,
            "availability": self.availability.value,
            "sufficient_for_prs_assessment": self.sufficient_for_prs_assessment,
            "route": self.route,
            "fields_available": list(self.fields_available),
            "associations_requested": self.associations_requested,
            "bytes_received": self.bytes_received,
            "latency_seconds": round(self.latency_seconds, 4),
            "endpoints_tried": list(self.endpoints_tried),
            "ssf_metadata": self.ssf_metadata.to_dict() if self.ssf_metadata else None,
            "error": self.error,
            "notes": list(self.notes),
        }


@dataclass
class FileCandidate:
    """One file offered in a study's summary-statistics directory."""

    name: str
    url: str
    size_bytes: Optional[int] = None
    size_label: Optional[str] = None
    last_modified: Optional[str] = None
    is_harmonised: bool = False
    is_directory: bool = False
    kind: str = UNKNOWN  # data | metadata | checksum | readme | auxiliary | directory
    score: float = 0.0
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "size_bytes": self.size_bytes,
            "size_label": self.size_label,
            "last_modified": self.last_modified,
            "is_harmonised": self.is_harmonised,
            "kind": self.kind,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
        }


@dataclass
class ResolvedFile:
    """The file GWASPoker chose, and a defensible reason for choosing it."""

    url: str
    name: str
    size_bytes: Optional[int] = None
    is_harmonised: bool = False
    selection_reason: str = ""
    candidates: tuple[FileCandidate, ...] = ()
    directory_url: Optional[str] = None
    metadata_url: Optional[str] = None
    checksum_url: Optional[str] = None
    expected_md5: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "is_harmonised": self.is_harmonised,
            "selection_reason": self.selection_reason,
            "directory_url": self.directory_url,
            "metadata_url": self.metadata_url,
            "checksum_url": self.checksum_url,
            "expected_md5": self.expected_md5,
            "candidates": [c.to_dict() for c in self.candidates],
        }
