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
from gwaspoker.probe.payload import PayloadClassification, PayloadKind, classify_payload_prefix
from gwaspoker.validation.values import ValueStatus, ValueValidationResult, validate_values

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
    #: Where the bytes actually came from, after redirects. A share link that
    #: 302s to a landing page is the commonest cause of a bogus "decompression
    #: error" on a .gz URL, and without this the report cannot show it.
    final_url: Optional[str] = None
    content_type: Optional[str] = None
    content_disposition: Optional[str] = None
    redirect_count: int = 0
    #: One entry per HTTP attempt, including the ones that failed. Makes every
    #: outcome auditable in supplementary data rather than just countable.
    attempts: tuple[dict[str, Any], ...] = ()

    def record_attempt(
        self,
        method: str,
        *,
        status: Optional[int] = None,
        bytes_received: int = 0,
        seconds: float = 0.0,
        error: Optional[str] = None,
    ) -> None:
        """Log one HTTP attempt and fold its cost into the totals.

        Byte accounting counts **every** payload byte that crossed the network,
        including bytes from an attempt that later timed out. TransferStats
        claims to be "exactly what moved over the network", and the manuscript's
        headline claim is transfer volume, so an abandoned partial read must not
        vanish from the total.
        """
        self.attempts = (
            *self.attempts,
            {
                "method": method,
                "status": status,
                "bytes": bytes_received,
                "seconds": round(seconds, 4),
                "error": error,
            },
        )
        self.request_count += 1
        self.received_bytes += bytes_received
        self.transfer_time_seconds += seconds

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
            "final_url": self.final_url,
            "content_type": self.content_type,
            "content_disposition": self.content_disposition,
            "redirect_count": self.redirect_count,
            "attempts": list(self.attempts),
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
    #: Whether the bytes were plausibly a data file at all, decided before any
    #: decoding was attempted.
    payload: Optional[PayloadClassification] = None
    header: Optional[HeaderDetectionResult] = None
    mapping: Optional[MappingResult] = None
    #: Whether the sampled values support the concepts the header claimed.
    #: Kept separate from `mapping` so header evidence and value evidence
    #: can be measured independently.
    value_validation: Optional[ValueValidationResult] = None
    error: Optional[str] = None
    failure_category: Optional[FailureCategory] = None
    #: Non-fatal observations worth carrying into a report: a mislabelled
    #: extension, an archive member reached past metadata, and so on. A warning
    #: never suppresses a result -- it explains one.
    warnings: list[str] = field(default_factory=list)
    probe_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.header is not None and self.error is None

    @property
    def value_status(self) -> ValueStatus:
        """Overall value-domain status, or NOT_TESTED when nothing was checked."""
        if self.value_validation is None:
            return ValueStatus.NOT_TESTED
        return self.value_validation.overall_status

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
            "payload": self.payload.to_dict() if self.payload else None,
            "decompression": self.decompression.to_dict() if self.decompression else None,
            "header": self.header.to_dict() if self.header else None,
            "mapping": self.mapping.to_dict() if self.mapping else None,
            "value_validation": (
                self.value_validation.to_dict() if self.value_validation else None
            ),
            "succeeded": self.succeeded,
            "error": self.error,
            "warnings": list(self.warnings),
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

    #: Statuses that are a definite answer, not a transient fault. Retrying or
    #: falling back on these would waste a request and misreport the outcome.
    _TERMINAL_STATUSES = (403, 404, 410)

    @staticmethod
    def _record_failed_attempt(stats: TransferStats, method: str, exc: Exception) -> None:
        """Record what a failed attempt cost, not merely that it failed.

        A stream that delivered 96 KiB and then timed out moved 96 KiB. Booking
        it as zero would overstate the transfer reduction GWASPoker reports,
        which is a headline number -- so :class:`PartialTransferError` carries
        the real figures and they are recorded here.
        """
        from gwaspoker.failures import PartialTransferError

        if isinstance(exc, PartialTransferError):
            stats.record_attempt(
                method,
                status=exc.status,
                bytes_received=exc.bytes_received,
                seconds=exc.elapsed_seconds,
                error=str(exc),
            )
            if exc.final_url and stats.final_url is None:
                stats.final_url = exc.final_url
                stats.redirect_count = exc.redirect_count
            if exc.content_type and not stats.content_type:
                stats.content_type = exc.content_type.split(";", 1)[0].strip().lower()
        else:
            stats.record_attempt(method, error=str(exc))

    def _absorb(self, stats: TransferStats, result, method: str) -> None:
        """Fold one successful response's metadata into the transfer stats."""
        stats.record_attempt(
            method,
            status=result.status_code,
            bytes_received=result.byte_count,
            seconds=result.elapsed_seconds,
        )
        stats.http_status = result.status_code
        stats.final_url = result.url
        stats.redirect_count = result.redirect_count
        # The response that delivered the bytes is the authority on their type,
        # but a GET that omits the header must not erase what HEAD told us.
        if result.content_type:
            stats.content_type = result.content_type
        if result.content_disposition:
            stats.content_disposition = result.content_disposition
        if stats.remote_file_size is None:
            stats.remote_file_size = parse_content_length(result.headers)

    def _fetch_prefix(self, url: str, limit: int, stats: TransferStats) -> bytes:
        """Retrieve at most ``limit`` bytes, preferring an HTTP Range request.

        Three attempts at most, and every one is recorded:

        1. ``HEAD`` for the size and range support. **Advisory only** -- a
           timeout here used to abort the whole probe, even though the metadata
           it provides is a convenience. Old consortium servers frequently
           reject or hang on HEAD while serving GET perfectly well.
        2. ``GET`` with a ``Range`` header.
        3. A plain bounded ``GET``, reached when the range attempt failed *or*
           errored. The range attempt previously had no fallback, so one
           transient failure ended the probe.

        A terminal status (403/404/410) stops immediately with a structured
        failure -- those are answers, not faults. The byte ceiling is unchanged
        throughout.
        """
        range_supported: Optional[bool] = None

        # --- 1. HEAD, advisory ------------------------------------------
        try:
            head = self.http.head(url)
            stats.record_attempt("HEAD", status=head.status_code, seconds=head.elapsed_seconds)

            # Capture response metadata whatever the status. A 404 reached
            # after two redirects is explained by the redirects, so discarding
            # them on the failure path throws away the useful half of the
            # answer.
            stats.http_status = head.status_code
            stats.final_url = head.url
            stats.redirect_count = head.redirect_count
            if head.content_type:
                stats.content_type = head.content_type
            if head.content_disposition:
                stats.content_disposition = head.content_disposition

            if head.ok:
                stats.remote_file_size = parse_content_length(head.headers)
                range_supported = supports_ranges(head.headers)
            elif head.status_code in self._TERMINAL_STATUSES:
                raise RemoteAccessError(
                    f"HTTP {head.status_code} for {url}",
                    category=http_status_category(head.status_code),
                )
        except RemoteAccessError as exc:
            if exc.category in (
                FailureCategory.HTTP_403,
                FailureCategory.HTTP_404,
                FailureCategory.API_DEPRECATED,
            ):
                raise
            # HEAD is a convenience, not a requirement: carry on without it.
            self._record_failed_attempt(stats, "HEAD", exc)
            logger.debug("HEAD failed for %s (%s); continuing with GET", url, exc)

        stats.range_supported = range_supported

        # --- 2. Range GET -------------------------------------------------
        if range_supported is not False:
            try:
                result = self.http.get_range(url, start=0, length=limit)
            except RemoteAccessError as exc:
                self._record_failed_attempt(stats, "GET_RANGE", exc)
                logger.debug("Range GET failed for %s (%s); trying a bounded GET", url, exc)
            else:
                if result.status_code in self._TERMINAL_STATUSES:
                    self._absorb(stats, result, "GET_RANGE")
                    raise RemoteAccessError(
                        f"HTTP {result.status_code} for {url}",
                        category=http_status_category(result.status_code),
                    )
                if result.from_range:
                    self._absorb(stats, result, "GET_RANGE")
                    stats.range_used = True
                    stats.range_supported = True
                    return result.content
                if result.ok:
                    # Range ignored; we still stopped at the limit ourselves.
                    self._absorb(stats, result, "GET_RANGE")
                    stats.range_supported = False
                    logger.debug("%s ignored the Range header; bounded the read locally", url)
                    return result.content
                self._absorb(stats, result, "GET_RANGE")
                if result.status_code != 416:
                    raise RemoteAccessError(
                        f"HTTP {result.status_code} for {url}",
                        category=http_status_category(result.status_code),
                    )

        # --- 3. Bounded GET ------------------------------------------------
        try:
            result = self.http.stream_bounded(url, limit=limit)
        except RemoteAccessError as exc:
            self._record_failed_attempt(stats, "GET_BOUNDED", exc)
            raise
        self._absorb(stats, result, "GET_BOUNDED")
        stats.range_supported = False
        if not result.ok:
            raise RemoteAccessError(
                f"HTTP {result.status_code} for {url}",
                category=http_status_category(result.status_code),
            )
        return result.content

    def _interpret(self, data: bytes, result: ProbeResult) -> None:
        """Decompress, decode, find the header and map the columns."""
        if not data:
            result.error = "The server returned an empty response body"
            result.failure_category = FailureCategory.TRUNCATED_PROBE
            return

        # Before any decoding: are these bytes plausibly a data file at all?
        # An HTTP 200 web page used to reach the header scorer and be reported
        # as a successfully detected header, or to surface as a
        # "decompression_error" that blamed the file rather than the URL.
        headers = {}
        if result.transfer.content_type:
            headers["Content-Type"] = result.transfer.content_type
        result.payload = classify_payload_prefix(data, filename=result.filename, headers=headers)

        if result.payload.kind is PayloadKind.NON_DATA:
            result.error = _non_data_message(result)
            result.failure_category = FailureCategory.NON_DATA_RESPONSE
            return

        if result.payload.kind is PayloadKind.CONTENT_MISMATCH:
            # Two very different situations share this classification.
            #
            # `study.txt.gz` that is not gzip but *is* ordinary TSV is a naming
            # error on the server, over data that reads perfectly well. Refusing
            # it would discard files GWASPoker can parse -- so the mismatch is
            # recorded as a warning, compression is set to NONE, and the
            # pipeline continues. The same URL holding an unrecognised binary
            # format has nothing to parse and remains a failure.
            if not result.payload.is_recoverable_mismatch:
                result.error = _non_data_message(result)
                result.failure_category = FailureCategory.CONTENT_MISMATCH
                return

            declared = result.payload.declared_compression or "compression"
            result.warnings.append(
                f"{result.filename} is named as {declared} but carries no {declared} "
                "signature; the bytes read as text and were parsed uncompressed. The "
                "extension is wrong, not the file."
            )
            logger.info("Content mismatch on %s; reading as plain text", result.source)
            result.compression = Compression.NONE
        else:
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
                sample_rows=self.config.sample_rows,
            )
        except HeaderDetectionError as exc:
            result.error = str(exc)
            result.failure_category = exc.category
            return

        # A header candidate scoring above zero is not on its own proof that
        # this is tabular genomic data. Require corroboration from either the
        # mapping or the sampled rows -- but not both, so a genuinely novel
        # schema stays inspectable.
        mapped = get_mapper().map_header(header.raw_header)
        if not _looks_like_a_data_table(header, mapped):
            result.error = (
                f"a header row was found ({', '.join(header.raw_header[:6])}...) but no "
                "column mapped to a known concept and the following rows do not look "
                "like tabular genomic data; this payload is probably not summary "
                "statistics"
            )
            result.failure_category = FailureCategory.UNSUPPORTED_FORMAT
            return

        result.header = header
        result.mapping = mapped

        # Second, independent line of evidence: do the sampled values support
        # the concepts the header names claimed? These rows are already in
        # memory from the probe prefix, so this costs no extra bytes.
        result.value_validation = validate_values(
            result.mapping,
            header.sample_rows,
            max_rows=self.config.validation_rows,
        )


def _non_data_message(result: ProbeResult) -> str:
    """Explain a non-data response in terms the user can act on."""
    parts = [result.payload.reason]
    transfer = result.transfer
    if transfer.final_url and transfer.final_url != result.source:
        parts.append(f"the request was redirected to {transfer.final_url}")
    if transfer.content_type:
        parts.append(f"Content-Type was {transfer.content_type}")
    parts.append(
        "the transfer itself succeeded, so this is a URL problem rather than a " "corrupt file"
    )
    return "; ".join(parts)


def _looks_like_a_data_table(header: HeaderDetectionResult, mapping: MappingResult) -> bool:
    """Corroborate a detected header with independent evidence.

    Either a recognised column concept, or sampled rows that behave like a
    table, is enough. Requiring a mapping hit unconditionally would reject
    genuinely novel schemas, which are exactly the files worth inspecting.
    """
    from gwaspoker.mapping.normalize import is_probably_data_row

    if mapping.resolved:
        return True
    rows = header.sample_rows
    if not rows:
        return False
    consistent = sum(1 for row in rows if len(row) == len(header.raw_header))
    data_like = sum(1 for row in rows if is_probably_data_row(row))
    return consistent / len(rows) >= 0.8 and data_like / len(rows) >= 0.5


def _filename_from_url(url: str) -> str:
    """Last path segment of a URL, percent-decoded."""
    from urllib.parse import unquote, urlparse

    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1]) or "unknown"
