"""The single HTTP layer.

All network access in GWASPoker goes through :class:`HttpClient`. Parsing code
never constructs a request and never sees a :class:`requests.Response`; it is
handed bytes or decoded JSON. That separation is what makes the parsing modules
unit-testable without a network.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from gwaspoker.config import GWASPokerConfig, get_config
from gwaspoker.failures import RemoteAccessError, http_status_category

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, max_per_second: float) -> None:
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class HttpResult:
    """A completed HTTP exchange, with the timing the manuscript needs."""

    __slots__ = ("url", "status_code", "headers", "content", "elapsed_seconds", "from_range")

    def __init__(
        self,
        url: str,
        status_code: int,
        headers: dict[str, str],
        content: bytes,
        elapsed_seconds: float,
        from_range: bool = False,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.elapsed_seconds = elapsed_seconds
        self.from_range = from_range

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def byte_count(self) -> int:
        return len(self.content)


class HttpClient:
    """A retrying, rate-limited :mod:`requests` session."""

    def __init__(self, config: Optional[GWASPokerConfig] = None) -> None:
        self.config = config or get_config()
        self._limiter = _RateLimiter(self.config.max_requests_per_second)
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": self.config.user_agent, "Accept-Encoding": "identity"}
        )
        # Retry only on transient statuses. 404 and 410 are answers, not failures
        # to retry: a 410 from the summary-statistics API is a permanent,
        # documented deprecation and must reach the caller unchanged.
        retry = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.retry_backoff,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.config.connect_timeout, self.config.request_timeout)

    # -- primitives ------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        allow_redirects: bool = True,
    ) -> HttpResult:
        """Perform a complete GET. Use only for small resources."""
        self._limiter.wait()
        started = time.perf_counter()
        try:
            response = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=allow_redirects,
            )
        except requests.exceptions.RequestException as exc:
            raise RemoteAccessError(
                f"GET {url} failed: {exc}", category=_category_for(exc)
            ) from exc
        elapsed = time.perf_counter() - started
        logger.debug("GET %s -> %s (%.3fs)", response.url, response.status_code, elapsed)
        return HttpResult(
            url=response.url,
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            elapsed_seconds=elapsed,
            from_range=response.status_code == 206,
        )

    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[Any], HttpResult]:
        """GET and decode JSON. Returns ``(None, result)`` if the body is not JSON."""
        result = self.get(url, params=params, headers={"Accept": "application/json"})
        if not result.ok:
            return None, result
        import json

        try:
            return json.loads(result.content.decode("utf-8")), result
        except (ValueError, UnicodeDecodeError):
            logger.debug("Response from %s was not valid JSON", result.url)
            return None, result

    def head(self, url: str, *, allow_redirects: bool = True) -> HttpResult:
        """HEAD, used to learn size and range support before probing."""
        self._limiter.wait()
        started = time.perf_counter()
        try:
            response = self._session.head(
                url, timeout=self.timeout, allow_redirects=allow_redirects
            )
        except requests.exceptions.RequestException as exc:
            raise RemoteAccessError(
                f"HEAD {url} failed: {exc}", category=_category_for(exc)
            ) from exc
        elapsed = time.perf_counter() - started
        logger.debug("HEAD %s -> %s (%.3fs)", response.url, response.status_code, elapsed)
        return HttpResult(
            url=response.url,
            status_code=response.status_code,
            headers=dict(response.headers),
            content=b"",
            elapsed_seconds=elapsed,
        )

    def get_range(self, url: str, *, start: int = 0, length: int = 65_536) -> HttpResult:
        """Request ``length`` bytes from ``start`` using an HTTP Range header.

        A ``206`` response means the server honoured the range. A ``200`` means
        it ignored it and is sending the whole file -- the caller must then stop
        reading itself, which :meth:`stream_bounded` does.
        """
        end = start + length - 1
        self._limiter.wait()
        started = time.perf_counter()
        try:
            response = self._session.get(
                url,
                headers={"Range": f"bytes={start}-{end}"},
                timeout=self.timeout,
                stream=True,
            )
            # Read at most `length` bytes even if the server ignored the Range.
            buffer = bytearray()
            for chunk in response.iter_content(chunk_size=32_768):
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) >= length:
                    break
            content = bytes(buffer[:length])
            status = response.status_code
            headers = dict(response.headers)
            response.close()
        except requests.exceptions.RequestException as exc:
            raise RemoteAccessError(
                f"Range GET {url} failed: {exc}", category=_category_for(exc)
            ) from exc
        elapsed = time.perf_counter() - started
        logger.debug(
            "GET %s bytes=%d-%d -> %s, %d bytes (%.3fs)",
            url,
            start,
            end,
            status,
            len(content),
            elapsed,
        )
        return HttpResult(
            url=url,
            status_code=status,
            headers=headers,
            content=content,
            elapsed_seconds=elapsed,
            from_range=status == 206,
        )

    def stream_bounded(self, url: str, *, limit: int) -> HttpResult:
        """Stream a response and stop after ``limit`` bytes.

        Used when the server does not honour Range requests. The connection is
        closed as soon as the limit is reached, so the remainder of the file is
        never intentionally transferred.
        """
        self._limiter.wait()
        started = time.perf_counter()
        try:
            response = self._session.get(url, timeout=self.timeout, stream=True)
            buffer = bytearray()
            for chunk in response.iter_content(chunk_size=32_768):
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) >= limit:
                    break
            content = bytes(buffer[:limit])
            status = response.status_code
            headers = dict(response.headers)
            response.close()
        except requests.exceptions.RequestException as exc:
            raise RemoteAccessError(
                f"Bounded GET {url} failed: {exc}", category=_category_for(exc)
            ) from exc
        elapsed = time.perf_counter() - started
        return HttpResult(
            url=url,
            status_code=status,
            headers=headers,
            content=content,
            elapsed_seconds=elapsed,
            from_range=False,
        )

    def iter_download(
        self,
        url: str,
        *,
        start: int = 0,
        chunk_size: int = 1_048_576,
    ) -> tuple[Iterator[bytes], int, Optional[int]]:
        """Open a full download stream.

        Returns ``(chunks, status_code, total_bytes)``. ``start > 0`` issues an
        open-ended Range request to resume a partial file.
        """
        headers = {"Range": f"bytes={start}-"} if start > 0 else None
        self._limiter.wait()
        try:
            response = self._session.get(url, headers=headers, timeout=self.timeout, stream=True)
        except requests.exceptions.RequestException as exc:
            raise RemoteAccessError(
                f"GET {url} failed: {exc}", category=_category_for(exc)
            ) from exc
        if not (200 <= response.status_code < 300):
            status = response.status_code
            response.close()
            raise RemoteAccessError(
                f"GET {url} returned HTTP {status}",
                category=http_status_category(status),
            )
        length_header = response.headers.get("Content-Length")
        total = int(length_header) if length_header and length_header.isdigit() else None
        return response.iter_content(chunk_size=chunk_size), response.status_code, total


def _category_for(exc: requests.exceptions.RequestException):
    from gwaspoker.failures import FailureCategory

    if isinstance(exc, requests.exceptions.Timeout):
        return FailureCategory.NETWORK_TIMEOUT
    return FailureCategory.NETWORK_ERROR


def parse_content_length(headers: dict[str, str]) -> Optional[int]:
    """Read a byte count from ``Content-Length`` or ``Content-Range``."""
    raw = headers.get("Content-Range") or headers.get("content-range")
    if raw and "/" in raw:
        total = raw.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    raw = headers.get("Content-Length") or headers.get("content-length")
    if raw and raw.strip().isdigit():
        return int(raw.strip())
    return None


def supports_ranges(headers: dict[str, str]) -> bool:
    """True if the server advertises byte-range support."""
    value = headers.get("Accept-Ranges") or headers.get("accept-ranges") or ""
    return value.strip().lower() == "bytes"
