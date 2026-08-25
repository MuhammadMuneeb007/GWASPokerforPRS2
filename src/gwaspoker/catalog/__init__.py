"""Adapters for the GWAS Catalog APIs, and the normalized model they produce."""

from gwaspoker.catalog.models import (
    UNKNOWN,
    Ancestry,
    ApiAssessment,
    ApiAvailability,
    EfoTrait,
    FileCandidate,
    Provenance,
    ResolvedFile,
    SampleCounts,
    SsfMetadata,
    Study,
    ValueSource,
)
from gwaspoker.catalog.rest_api import GwasCatalogClient, is_accession
from gwaspoker.catalog.sumstats_api import SummaryStatisticsAssessor, parse_ssf_metadata

__all__ = [
    "UNKNOWN",
    "Ancestry",
    "ApiAssessment",
    "ApiAvailability",
    "EfoTrait",
    "FileCandidate",
    "GwasCatalogClient",
    "Provenance",
    "ResolvedFile",
    "SampleCounts",
    "SsfMetadata",
    "Study",
    "SummaryStatisticsAssessor",
    "ValueSource",
    "is_accession",
    "parse_ssf_metadata",
]
