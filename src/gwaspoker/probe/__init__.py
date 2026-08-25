"""Bounded inspection of remote and local summary-statistics files."""

from gwaspoker.probe.compression import (
    Compression,
    DecompressionResult,
    decompress_prefix,
    detect_compression,
)
from gwaspoker.probe.encoding import EncodingResult, detect_encoding, split_complete_lines
from gwaspoker.probe.header import (
    HeaderDetectionResult,
    HeaderDetector,
    detect_delimiter,
    detect_header,
)
from gwaspoker.probe.remote import ProbeResult, RemoteProber, TransferStats

__all__ = [
    "Compression",
    "DecompressionResult",
    "EncodingResult",
    "HeaderDetectionResult",
    "HeaderDetector",
    "ProbeResult",
    "RemoteProber",
    "TransferStats",
    "decompress_prefix",
    "detect_compression",
    "detect_delimiter",
    "detect_encoding",
    "detect_header",
    "split_complete_lines",
]
