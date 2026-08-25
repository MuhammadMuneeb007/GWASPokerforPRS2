"""Extraction and declared normalization of downloaded summary statistics."""

from gwaspoker.processing.extract import ExtractionResult, Extractor
from gwaspoker.processing.formats import (
    human_size,
    inner_extension,
    is_tabular,
    strip_compression_suffix,
)
from gwaspoker.processing.normalize import NormalizationReport, Transformation

__all__ = [
    "ExtractionResult",
    "Extractor",
    "NormalizationReport",
    "Transformation",
    "human_size",
    "inner_extension",
    "is_tabular",
    "strip_compression_suffix",
]
