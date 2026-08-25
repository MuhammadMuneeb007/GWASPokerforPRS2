"""Bounded remote probing -- the core operation.

The v1 approach was ``timeout -s KILL 10 wget -q <url>``: kill the transfer
after ten seconds and hope a header arrived. How many bytes that moved depended
on the network, so the experiment was not reproducible, the transfer volume was
unbounded, and the whole thing needed GNU coreutils.

Here the bound is on *bytes*, which is reproducible and is the quantity the
manuscript reports:

1. ``HEAD`` to learn the file size and whether ranges are supported;
2. if ranges are supported, ``Range: bytes=0-N``;
3. if not, stream and close the connection at N bytes;
4. detect compression, decompress the prefix incrementally;
5. detect encoding, split complete lines;
6. detect the header and map the columns.

A local file goes through steps 4-6 unchanged, which is why ``gwaspoker scan``
gives identical results for a local file and its remote original.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import (
    FailureCategory,
    HeaderDetectionError,
    RemoteAccessError,
    http_status_category,
)
from gwaspoker.http import HttpClient, parse_content_length, supports_ranges
from gwaspoker.mapping.mapper import MappingResult, get_mapper
from gwaspoker.probe.compression import (
    Compression,
    DecompressionResult,
    decompress_prefix,
    detect_compression,
)
from gwaspoker.probe.encoding import detect_encoding, split_complete_lines
from gwaspoker.probe.header import HeaderDetectionResult, detect_header

logger = logging.getLogger(__name__)


@dataclass
class TransferStats:
    """Exactly what moved over the network. Reported in every provenance record."""

    requested_bytes: int = 0
    received_bytes: int = 0
    remote_file_size: Optional[int] = None
    range_supported: Optional[bool] = None
    range_used: bool = False
    transfer_time_seconds: float = 0.0
    http_status: Optional[int] = None
    request_count: int = 0

    @property
    def transfer_reduction(self) -> Optional[float]:
        """Fraction of the file that was *not* transferred, in [0, 1]."""
        if not self.remote_file_size or self.remote_file_size <= 0:
            return None
        return max(0.0, 1.0 - self.received_bytes / self.remote_file_size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_bytes": self.requested_bytes,
            "received_bytes": self.received_bytes,
            "remote_file_size": self.remote_file_size,
            "range_supported": self.range_supported,
            "range_used": self.range_used,
            "transfer_time_seconds": round(self.transfer_time_seconds, 4),
            "http_status": self.http_status,
            "request_count": self.request_count,
            "transfer_reduction": (
                round(self.transfer_reduction, 6) if self.transfer_reduction is not None else None
            ),
        }


@dataclass
class ProbeResult:
    """Everything learned about a file without downloading it."""

    source: str
    source_kind: str  # "url" | "local"
    filename: str
    transfer: TransferStats = field(default_factory=TransferStats)
    compression: Compression = Compression.UNKNOWN
    decompression: Optional[DecompressionResult] = None
    encoding: Optional[str] = None
    encoding_confidence: Optional[float] = None
    header: Optional[HeaderDetectionResult] = None
    mapping: Optional[MappingResult] = None
    error: Optional[str] = None
    failure_category: Optional[FailureCategory] = None
    probe_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.header is not None and self.error is None

    @property
    def format_label(self) -> str:
        """Short human label such as ``TSV.GZ`` or ``CSV``."""
        stem = self.filename.lower()
        for suffix in (".gz", ".bgz", ".zip", ".tar", ".bz2", ".xz", ".zst"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        base = "TSV"
        if stem.endswith(".csv"):
            base = "CSV"
        elif stem.endswith(".txt"):
            base = "TXT"
        elif stem.endswith((".tsv", ".tab")):
            base = "TSV"
        elif self.header is not None:
            base = {"\t": "TSV", ",": "CSV", " ": "SSV", ";": "SCSV"}.get(
                self.header.delimiter, "TSV"
            )
        if self.compression in (Compression.NONE, Compression.UNKNOWN):
            return base
        return f"{base}.{self.compression.value.upper()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_kind": self.source_kind,
            "filename": self.filename,
            "format": self.format_label,
            "compression": self.compression.value,
            "encoding": self.encoding,
            "encoding_confidence": (
                round(self.encoding_confidence, 3) if self.encoding_confidence else None
            ),
            "transfer": self.transfer.to_dict(),
            "decompression": self.decompression.to_dict() if self.decompression else None,
            "header": self.header.to_dict() if self.header else None,
            "mapping": self.mapping.to_dict() if self.mapping else None,
            "succeeded": self.succeeded,
            "error": self.error,
            "failure_category": self.failure_category.value if self.failure_category else None,
            "probe_seconds": round(self.probe_seconds, 4),
        }


class RemoteProber:
    """Fetches a bounded prefix of a file and works out what is in it."""

    def __init__(
        self,
        config: Optional[GWASPokerConfig] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.config = config or get_config()
        self.http = http or HttpClient(self.config)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> RemoteProber:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    def probe_url(
        self,
        url: str,
        *,
        probe_bytes: Optional[int] = None,
        filename: Optional[str] = None,
    ) -> ProbeResult:
        """Probe a remote file, transferring at most ``probe_bytes``."""
        limit = probe_bytes or self.config.probe_bytes
        name = filename or _filename_from_url(url)
        result = ProbeResult(source=url, source_kind="url", filename=name)
        result.transfer.requested_bytes = limit
        started = time.perf_counter()

        try:
            data = self._fetch_prefix(url, limit, result.transfer)
        except RemoteAccessError as exc:
            result.error = str(exc)
            result.failure_category = exc.category
            result.probe_seconds = time.perf_counter() - started
            return result

        self._interpret(data, result)
        result.probe_seconds = time.perf_counter() - started
        return result

    def probe_local(
        self,
        path: Path,
        *,
        probe_bytes: Optional[int] = None,
    ) -> ProbeResult:
        """Probe a local file by reading only its first ``probe_bytes``.

        Reading a bounded prefix rather than the whole file keeps ``scan`` fast
        on multi-gigabyte downloads and makes local and remote results directly
        comparable.
        """
        path = Path(path)
        limit = probe_bytes or self.config.probe_bytes
        result = ProbeResult(source=str(path), source_kind="local", filename=path.name)
        result.transfer.requested_bytes = limit
        started = time.perf_counter()

        if not path.is_file():
            result.error = f"No such file: {path}"
            result.failure_category = FailureCategory.FILE_NOT_FOUND
            result.probe_seconds = time.perf_counter() - started
            return result

        size = path.stat().st_size
        result.transfer.remote_file_size = size
        try:
            with path.open("rb") as handle:
                data = handle.read(limit)
        except OSError as exc:
            result.error = f"Could not read {path}: {exc}"
            result.failure_category = FailureCategory.DISK_ERROR
            result.probe_seconds = time.perf_counter() - started
            return result

        result.transfer.received_bytes = len(data)
        result.transfer.transfer_time_seconds = 0.0
        self._interpret(data, result)
        result.probe_seconds = time.perf_counter() - started
        return result

    # ------------------------------------------------------------------

    def _fetch_prefix(self, url: str, limit: int, stats: TransferStats) -> bytes:
        """Retrieve at most ``limit`` bytes, preferring an HTTP Range request."""
        range_supported: Optional[bool] = None
        try:
            head = self.http.head(url)
            stats.request_count += 1
            if head.ok:
                stats.remote_file_size = parse_content_length(head.headers)
                range_supported = supports_ranges(head.headers)
            elif head.status_code in (403, 404, 410):
                raise RemoteAccessError(
                    f"HTTP {head.status_code} for {url}",
                    category=http_status_category(head.status_code),
                )
        except RemoteAccessError:
            raise
        stats.range_supported = range_supported

        if range_supported is not False:
            result = self.http.get_range(url, start=0, length=limit)
            stats.request_count += 1
            stats.http_status = result.status_code
            stats.transfer_time_seconds += result.elapsed_seconds

            if result.status_code in (403, 404, 410):
                raise RemoteAccessError(
                    f"HTTP {result.status_code} for {url}",
                    category=http_status_category(result.status_code),
                )
            if result.from_range:
                stats.range_used = True
                stats.range_supported = True
                stats.received_bytes = result.byte_count
                if stats.remote_file_size is None:
                    stats.remote_file_size = parse_content_length(result.headers)
                return result.content
            if result.ok:
                # Range ignored; we still stopped at the limit ourselves.
                stats.range_supported = False
                stats.received_bytes = result.byte_count
                logger.debug("%s ignored the Range header; bounded the read locally", url)
                return result.content
            if result.status_code != 416:
                raise RemoteAccessError(
                    f"HTTP {result.status_code} for {url}",
                    category=http_status_category(result.status_code),
                )

        result = self.http.stream_bounded(url, limit=limit)
        stats.request_count += 1
        stats.http_status = result.status_code
        stats.transfer_time_seconds += result.elapsed_seconds
        stats.range_supported = False
        if not result.ok:
            raise RemoteAccessError(
                f"HTTP {result.status_code} for {url}",
                category=http_status_category(result.status_code),
            )
        stats.received_bytes = result.byte_count
        if stats.remote_file_size is None:
            stats.remote_file_size = parse_content_length(result.headers)
        return result.content

    def _interpret(self, data: bytes, result: ProbeResult) -> None:
        """Decompress, decode, find the header and map the columns."""
        if not data:
            result.error = "The server returned an empty response body"
            result.failure_category = FailureCategory.TRUNCATED_PROBE
            return

        result.compression = detect_compression(data, result.filename)

        try:
            decompressed = decompress_prefix(data, result.compression)
        except Exception as exc:  # CompressionError and its subclasses
            from gwaspoker.failures import classify_exception

            result.error = str(exc)
            result.failure_category = classify_exception(exc)
            return
        result.decompression = decompressed

        if not decompressed.data:
            result.error = (
                f"{result.compression.value} prefix of {len(data)} bytes yielded no "
                "decompressed output; a larger --probe-bytes may be needed"
            )
            result.failure_category = FailureCategory.TRUNCATED_PROBE
            return

        encoding_result = detect_encoding(decompressed.data)
        result.encoding = encoding_result.encoding
        result.encoding_confidence = encoding_result.confidence

        complete_lines, partial = split_complete_lines(encoding_result.text)
        if not complete_lines and partial:
            # A single very long line: treat it as complete so a one-line header
            # in a small file is not discarded.
            complete_lines = [partial]
        if not complete_lines:
            result.error = "No complete lines were recovered from the inspected bytes"
            result.failure_category = FailureCategory.TRUNCATED_PROBE
            return

        try:
            header = detect_header(
                complete_lines,
                encoding=result.encoding,
                max_scan_lines=self.config.max_header_scan_lines,
            )
        except HeaderDetectionError as exc:
            result.error = str(exc)
            result.failure_category = exc.category
            return

        result.header = header
        result.mapping = get_mapper().map_header(header.raw_header)


def _filename_from_url(url: str) -> str:
    """Last path segment of a URL, percent-decoded."""
    from urllib.parse import unquote, urlparse

    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1]) or "unknown"
