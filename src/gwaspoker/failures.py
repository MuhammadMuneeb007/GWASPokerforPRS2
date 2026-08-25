"""Structured failure classification.

Every failure in GWASPoker carries a category from :class:`FailureCategory`, the
stage it happened in, the study or URL involved, and the originating exception.
This replaces the twenty-three bare ``except:`` clauses of the original modules,
several of which had ``pass`` bodies, and one of which returned the *integer* 0
from a number-parsing helper so that a parse error was indistinguishable from a
genuine zero.

Two rules hold throughout ``src/gwaspoker``:

1. No bare ``except:`` and no ``except Exception: pass``. Exceptions are caught
   by type, converted to a :class:`FailureRecord`, and either logged or raised.
2. A failure never produces a plausible-looking value. ``None`` and the string
   ``"unknown"`` are the only failure sentinels.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FailureCategory(str, Enum):
    """Controlled vocabulary for failure reasons.

    Values are stable strings so that they can be written to benchmark manifests
    and grouped across runs.
    """

    # --- API / metadata -------------------------------------------------
    API_NOT_AVAILABLE = "api_not_available"
    API_DEPRECATED = "api_deprecated"
    API_ERROR = "api_error"
    API_SCHEMA_ERROR = "api_schema_error"
    METADATA_MISSING = "metadata_missing"
    INVALID_ACCESSION = "invalid_accession"
    NOT_REPRESENTED = "not_represented"

    # --- Network --------------------------------------------------------
    FILE_NOT_FOUND = "file_not_found"
    HTTP_403 = "http_403"
    HTTP_404 = "http_404"
    HTTP_ERROR = "http_error"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_ERROR = "network_error"
    RANGE_NOT_SUPPORTED = "range_not_supported"

    # --- File inspection ------------------------------------------------
    #: The server returned a web page, an XML document, or another non-data
    #: payload. Distinct from a broken archive: the transfer succeeded and the
    #: bytes are intact, they simply are not summary statistics. A URL ending
    #: `.gz` that answers `text/html` is almost always a share/landing page.
    NON_DATA_RESPONSE = "non_data_response"
    #: The filename extension and the actual bytes disagree -- a `.gz` whose
    #: magic bytes are not gzip, for instance. Distinct from
    #: DECOMPRESSION_ERROR, which means a genuine compressed stream failed to
    #: decode.
    CONTENT_MISMATCH = "content_mismatch"
    UNSUPPORTED_COMPRESSION = "unsupported_compression"
    UNSUPPORTED_FORMAT = "unsupported_format"
    DECOMPRESSION_ERROR = "decompression_error"
    ENCODING_ERROR = "encoding_error"
    HEADER_NOT_FOUND = "header_not_found"
    DELIMITER_NOT_DETECTED = "delimiter_not_detected"
    TRUNCATED_PROBE = "truncated_probe"

    # --- Mapping / assessment -------------------------------------------
    MAPPING_INCOMPLETE = "mapping_incomplete"

    # --- Transfer -------------------------------------------------------
    DOWNLOAD_ERROR = "download_error"
    CHECKSUM_FAILED = "checksum_failed"
    DISK_ERROR = "disk_error"

    # --- Optional integrations ------------------------------------------
    DEPENDENCY_MISSING = "dependency_missing"
    GWASLAB_ERROR = "gwaslab_error"
    LLM_ERROR = "llm_error"

    UNKNOWN = "unknown"


class GWASPokerError(Exception):
    """Base class for GWASPoker errors that carry a failure category."""

    category: FailureCategory = FailureCategory.UNKNOWN

    def __init__(self, message: str, *, category: Optional[FailureCategory] = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class CatalogApiError(GWASPokerError):
    """The GWAS Catalog API could not answer a query."""

    category = FailureCategory.API_ERROR


class AccessionNotFoundError(GWASPokerError):
    """The accession is syntactically valid but not present in the Catalog."""

    category = FailureCategory.INVALID_ACCESSION


class RemoteAccessError(GWASPokerError):
    """A remote file could not be reached."""

    category = FailureCategory.NETWORK_ERROR


class FileResolutionError(GWASPokerError):
    """No summary-statistics file could be resolved for a study."""

    category = FailureCategory.FILE_NOT_FOUND


class CompressionError(GWASPokerError):
    """A compressed stream could not be decoded."""

    category = FailureCategory.DECOMPRESSION_ERROR


class HeaderDetectionError(GWASPokerError):
    """No plausible header row was found in the inspected bytes."""

    category = FailureCategory.HEADER_NOT_FOUND


class ChecksumError(GWASPokerError):
    """A downloaded file did not match its published checksum."""

    category = FailureCategory.CHECKSUM_FAILED


class OptionalDependencyError(GWASPokerError):
    """An optional extra is required for the requested operation."""

    category = FailureCategory.DEPENDENCY_MISSING


@dataclass
class FailureRecord:
    """One classified failure, suitable for a log line or a manifest cell."""

    stage: str
    category: FailureCategory
    message: str
    study: Optional[str] = None
    url: Optional[str] = None
    exception_type: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "stage": self.stage,
            "failure_category": self.category.value,
            "study": self.study,
            "url": self.url,
            "exception": self.exception_type,
            "message": self.message,
        }

    def __str__(self) -> str:  # pragma: no cover - formatting only
        target = self.study or self.url or "-"
        return f"[{self.category.value}] {self.stage}: {target}: {self.message}"


def classify_exception(exc: BaseException) -> FailureCategory:
    """Map a caught exception onto a :class:`FailureCategory`.

    Import of :mod:`requests` is deferred so that the failure vocabulary stays
    usable in contexts where networking is not involved.
    """
    if isinstance(exc, GWASPokerError):
        return exc.category

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        requests = None  # type: ignore[assignment]

    if requests is not None:
        if isinstance(exc, requests.exceptions.Timeout):
            return FailureCategory.NETWORK_TIMEOUT
        if isinstance(exc, requests.exceptions.HTTPError):
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            return http_status_category(status) if status else FailureCategory.HTTP_ERROR
        if isinstance(exc, requests.exceptions.RequestException):
            return FailureCategory.NETWORK_ERROR

    if isinstance(exc, UnicodeDecodeError):
        return FailureCategory.ENCODING_ERROR
    if isinstance(exc, (EOFError, OSError)) and "gzip" in str(exc).lower():
        return FailureCategory.DECOMPRESSION_ERROR
    if isinstance(exc, FileNotFoundError):
        return FailureCategory.FILE_NOT_FOUND
    if isinstance(exc, OSError):
        return FailureCategory.DISK_ERROR
    if isinstance(exc, ImportError):
        return FailureCategory.DEPENDENCY_MISSING
    return FailureCategory.UNKNOWN


def http_status_category(status: int) -> FailureCategory:
    """Map an HTTP status code onto a failure category.

    410 Gone is distinguished from 404 Not Found: the GWAS Catalog Summary
    Statistics API returns 410 because it has been *withdrawn*, which is a
    permanent, documented state and not a transient server problem.
    """
    if status == 403:
        return FailureCategory.HTTP_403
    if status == 404:
        return FailureCategory.HTTP_404
    if status == 410:
        return FailureCategory.API_DEPRECATED
    if status == 416:
        return FailureCategory.RANGE_NOT_SUPPORTED
    if 500 <= status < 600:
        return FailureCategory.API_ERROR
    return FailureCategory.HTTP_ERROR


class FailureLog:
    """Collects :class:`FailureRecord` objects for one GWASPoker invocation."""

    def __init__(self) -> None:
        self._records: list[FailureRecord] = []

    def record(
        self,
        stage: str,
        category: FailureCategory,
        message: str,
        *,
        study: Optional[str] = None,
        url: Optional[str] = None,
        exception: Optional[BaseException] = None,
    ) -> FailureRecord:
        """Add a failure and log it at WARNING level."""
        rec = FailureRecord(
            stage=stage,
            category=category,
            message=message,
            study=study,
            url=url,
            exception_type=type(exception).__name__ if exception is not None else None,
        )
        self._records.append(rec)
        logger.warning("%s", rec)
        return rec

    def record_exception(
        self,
        stage: str,
        exc: BaseException,
        *,
        study: Optional[str] = None,
        url: Optional[str] = None,
    ) -> FailureRecord:
        """Classify and add an exception."""
        return self.record(
            stage,
            classify_exception(exc),
            str(exc) or type(exc).__name__,
            study=study,
            url=url,
            exception=exc,
        )

    @property
    def records(self) -> list[FailureRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __bool__(self) -> bool:
        return bool(self._records)

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._records]

    def write_jsonl(self, path: Path) -> None:
        """Append the collected failures to a JSON Lines file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for rec in self._records:
                handle.write(json.dumps(rec.to_dict()) + "\n")


#: Process-wide failure log. Commands read it at the end of a run to report a
#: summary and, when ``--failure-log`` is given, to persist it.
FAILURES = FailureLog()
