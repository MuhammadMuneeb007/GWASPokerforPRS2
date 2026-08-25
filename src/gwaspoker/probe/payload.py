"""Is this payload data at all?

A bounded probe used to go straight from received bytes to compression
detection, decompression, encoding and header scoring. That works when the
server returns what the URL promised. It does not when the server returns a web
page.

An external run over 768 heterogeneous URLs produced responses like::

    <meta | name="MobileOptimized" | content="width" | />

which reached the header scorer, split on ``|``, and were reported as a
successfully detected header. Others surfaced as ``decompression_error`` --
technically true, since a landing page is not a gzip stream, but the wrong
diagnosis: nothing was broken, the URL simply does not serve the file any more.

Share links (Dropbox, institutional file services, old consortium pages) answer
HTTP 200 with HTML while keeping the ``.gz`` in the path, so neither the status
code nor the extension is sufficient.

This module answers one question before any decoding is attempted: **are these
bytes plausibly a data file?** It reports; it never repairs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

#: Bytes inspected when sniffing. Enough to see a doctype or an XML prolog past
#: a byte-order mark or leading whitespace, cheap enough to be free.
SNIFF_BYTES = 2048

#: Markup openers that identify a web page or an XML document. Matched
#: case-insensitively after leading whitespace, and only near the start.
_MARKUP_OPENERS: tuple[bytes, ...] = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<meta",
    b"<?xml",
    b"<!--",
    b"<script",
    b"<title",
    b"<div",
    b"<span",
    b"<svg",
)

#: Content types that are never summary statistics. ``application/xml`` is
#: included because an S3/GCS error document is XML.
_NON_DATA_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml",
    "application/xml",
    "text/xml",
    "application/json",
    "application/pdf",
    "image/",
    "video/",
    "audio/",
)

#: Content types that say "bytes" without saying what kind. Not evidence either
#: way, so they must not trigger a NON_DATA verdict on their own.
_OPAQUE_CONTENT_TYPES: tuple[str, ...] = (
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
    "application/download",
)

_HTML_TAG_RE = re.compile(rb"<\s*/?\s*[a-zA-Z][a-zA-Z0-9]*[\s>/]")


class PayloadKind(str, Enum):
    """What the first bytes of a response look like."""

    #: Plausibly a data file: compressed, or text that is not markup.
    DATA = "data"
    #: A web page, XML document, or other non-data payload.
    NON_DATA = "non_data"
    #: The extension promised a container the bytes do not contain.
    CONTENT_MISMATCH = "content_mismatch"
    #: Nothing to classify.
    EMPTY = "empty"


@dataclass
class PayloadClassification:
    """The verdict, and the evidence behind it."""

    kind: PayloadKind
    reason: str
    content_type: Optional[str] = None
    detected_markup: Optional[str] = None
    declared_compression: Optional[str] = None

    @property
    def is_data(self) -> bool:
        return self.kind is PayloadKind.DATA

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "content_type": self.content_type,
            "detected_markup": self.detected_markup,
            "declared_compression": self.declared_compression,
        }


def _normalised_content_type(headers: Optional[dict[str, str]]) -> Optional[str]:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return None


def looks_like_markup(data: bytes) -> Optional[str]:
    """Return the markup opener found near the start of ``data``, if any.

    Only the first :data:`SNIFF_BYTES` are examined, and an opener must appear
    in the leading portion: a TSV whose *last* column happens to contain
    ``<div>`` is still a TSV.
    """
    if not data:
        return None

    head = data[:SNIFF_BYTES]
    # Skip a BOM and any leading whitespace before looking for an opener.
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = stripped[:512].lower()

    for opener in _MARKUP_OPENERS:
        if lowered.startswith(opener):
            return opener.decode("ascii")

    # Some pages begin with a comment, a stray blank line or a short preamble.
    # Require several tags in the first sniff window before calling it markup,
    # so a data file with one angle bracket is not misjudged.
    tags = _HTML_TAG_RE.findall(head)
    if len(tags) >= 3:
        return tags[0].decode("ascii", errors="replace").strip()
    return None


def classify_payload_prefix(
    data: bytes,
    *,
    filename: str = "",
    headers: Optional[dict[str, str]] = None,
) -> PayloadClassification:
    """Decide whether a probe prefix is plausibly a data file.

    Evidence, in order of authority:

    1. **Magic bytes.** A real gzip/zip/tar/bzip2/xz/zstd signature settles it.
    2. **Markup sniffing.** A doctype or a run of HTML tags means a web page,
       whatever the URL said.
    3. **Declared content type.** ``text/html`` on a ``.gz`` URL is a landing
       page. Opaque types such as ``application/octet-stream`` are ignored --
       they carry no information.
    4. **Extension versus reality.** A ``.gz`` with no gzip magic and no markup
       is a content mismatch, not a decompression failure.

    The extension is a hint throughout; it is never sufficient on its own to
    invoke a decompressor.
    """
    from gwaspoker.probe.compression import Compression, detect_compression_by_magic

    content_type = _normalised_content_type(headers)

    if not data:
        return PayloadClassification(
            kind=PayloadKind.EMPTY,
            reason="the response body was empty",
            content_type=content_type,
        )

    # 1. Real compression magic settles the question immediately.
    magic = detect_compression_by_magic(data)
    if magic is not None:
        return PayloadClassification(
            kind=PayloadKind.DATA,
            reason=f"{magic.value} magic bytes present",
            content_type=content_type,
            declared_compression=magic.value,
        )

    # 2. Markup in the body beats everything the URL or headers claim.
    markup = looks_like_markup(data)
    if markup is not None:
        return PayloadClassification(
            kind=PayloadKind.NON_DATA,
            reason=(
                f"the response body begins with {markup!r}, so the server returned a "
                "web page or XML document rather than summary statistics"
            ),
            content_type=content_type,
            detected_markup=markup,
        )

    # 3. A declared non-data content type, with no contradicting magic.
    if content_type and content_type.startswith(_NON_DATA_CONTENT_TYPES):
        return PayloadClassification(
            kind=PayloadKind.NON_DATA,
            reason=f"the server declared Content-Type: {content_type}",
            content_type=content_type,
        )

    # 4. The extension promised a container that is not there.
    declared = detect_compression_by_extension(filename)
    if declared not in (None, Compression.NONE):
        return PayloadClassification(
            kind=PayloadKind.CONTENT_MISMATCH,
            reason=(
                f"the filename declares {declared.value} but the bytes carry no "
                f"{declared.value} signature; the extension is a hint, not evidence, "
                "so no decompressor was invoked"
            ),
            content_type=content_type,
            declared_compression=declared.value,
        )

    return PayloadClassification(
        kind=PayloadKind.DATA,
        reason="no markup detected and no compression declared; treated as plain text",
        content_type=content_type,
    )


def detect_compression_by_extension(filename: str):
    """Compression implied by the filename, or ``None``. A hint only."""
    from gwaspoker.probe.compression import _EXTENSIONS

    lowered = (filename or "").lower().rstrip("/")
    for suffix, compression in _EXTENSIONS:
        if lowered.endswith(suffix):
            return compression
    return None
