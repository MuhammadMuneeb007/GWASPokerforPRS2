"""Resolving and downloading complete summary-statistics files."""

from gwaspoker.download.downloader import DownloadResult, SummaryStatisticsDownloader
from gwaspoker.download.resolver import (
    SummaryStatisticsResolver,
    accession_block,
    classify_file,
    parse_size_label,
    score_candidate,
)

__all__ = [
    "DownloadResult",
    "SummaryStatisticsDownloader",
    "SummaryStatisticsResolver",
    "accession_block",
    "classify_file",
    "parse_size_label",
    "score_candidate",
]
