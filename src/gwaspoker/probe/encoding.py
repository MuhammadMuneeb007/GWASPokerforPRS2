"""Text encoding detection for partially transferred files.

Two constraints make this different from ordinary encoding detection:

1. The buffer is a *prefix* of a file, so it very likely ends mid-character.
   Decoding must tolerate that without reporting a corrupt file.
2. Summary statistics are overwhelmingly ASCII. The interesting cases are a
   UTF-8 BOM, and Latin-1 author names or trait descriptions in a metadata
   preamble.

v1 hard-coded ``encoding="utf-8"`` in every ``pd.read_csv`` call and opened
files in text mode with the platform default elsewhere, so the same file decoded
differently on Windows (cp1252) and Linux (utf-8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: Byte-order marks, longest first so UTF-32 is not misread as UTF-16.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)

_FALLBACK_ORDER: tuple[str, ...] = ("utf-8", "cp1252", "latin-1")


@dataclass
class EncodingResult:
    """A detected encoding and the text decoded with it."""

    encoding: str
    confidence: float
    text: str
    had_bom: bool = False
    truncated_tail_bytes: int = 0
    method: str = "heuristic"

    def to_dict(self) -> dict[str, object]:
        return {
            "encoding": self.encoding,
            "confidence": round(self.confidence, 3),
            "had_bom": self.had_bom,
            "truncated_tail_bytes": self.truncated_tail_bytes,
            "method": self.method,
        }


def detect_bom(data: bytes) -> Optional[str]:
    """Return the encoding implied by a leading byte-order mark, if any."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return encoding
    return None


def _decode_tolerating_truncation(data: bytes, encoding: str) -> tuple[str, int]:
    """Decode ``data``, dropping up to 4 trailing bytes of a split character.

    Returns ``(text, dropped_bytes)``. Raises :class:`UnicodeDecodeError` if the
    failure is not at the very end, which means the encoding is genuinely wrong.
    """
    for dropped in range(0, 5):
        chunk = data[: len(data) - dropped] if dropped else data
        try:
            return chunk.decode(encoding), dropped
        except UnicodeDecodeError as exc:
            # Only tolerate failures within the last few bytes.
            if exc.start < len(chunk) - 4:
                raise
    raise UnicodeDecodeError(encoding, data, max(0, len(data) - 4), len(data), "undecodable tail")


def detect_encoding(data: bytes) -> EncodingResult:
    """Detect the encoding of a byte prefix and decode it.

    Order: BOM, then :mod:`charset_normalizer` when the data is not pure ASCII,
    then a fixed fallback chain. Latin-1 always succeeds, so detection never
    raises -- but it reports low confidence when it had to fall back that far.
    """
    if not data:
        return EncodingResult("utf-8", 1.0, "", method="empty")

    bom_encoding = detect_bom(data)
    if bom_encoding:
        text, dropped = _decode_tolerating_truncation(data, bom_encoding)
        return EncodingResult(
            encoding=bom_encoding,
            confidence=1.0,
            text=text,
            had_bom=True,
            truncated_tail_bytes=dropped,
            method="bom",
        )

    # Pure ASCII is unambiguous and by far the common case.
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        pass
    else:
        return EncodingResult("utf-8", 1.0, text, method="ascii")

    try:
        from charset_normalizer import from_bytes

        matches = from_bytes(data)
        best = matches.best()
        if best is not None and best.encoding:
            encoding = str(best.encoding)
            try:
                text, dropped = _decode_tolerating_truncation(data, encoding)
            except (UnicodeDecodeError, LookupError):
                logger.debug("charset-normalizer proposed %s but it did not decode", encoding)
            else:
                # chaos is a badness score in [0, 1]; invert it for confidence.
                confidence = max(0.0, min(1.0, 1.0 - float(getattr(best, "chaos", 0.0))))
                return EncodingResult(
                    encoding=encoding,
                    confidence=confidence,
                    text=text,
                    truncated_tail_bytes=dropped,
                    method="charset-normalizer",
                )
    except ImportError:  # pragma: no cover - charset-normalizer is a dependency
        logger.debug("charset-normalizer unavailable; using the fallback chain")

    for index, encoding in enumerate(_FALLBACK_ORDER):
        try:
            text, dropped = _decode_tolerating_truncation(data, encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # latin-1 never fails, so confidence drops sharply down the chain.
        confidence = (0.9, 0.6, 0.35)[min(index, 2)]
        return EncodingResult(
            encoding=encoding,
            confidence=confidence,
            text=text,
            truncated_tail_bytes=dropped,
            method="fallback",
        )

    # Unreachable in practice: latin-1 decodes every byte sequence.
    return EncodingResult(
        encoding="latin-1",
        confidence=0.1,
        text=data.decode("latin-1", errors="replace"),
        method="replace",
    )


def split_complete_lines(text: str) -> tuple[list[str], str]:
    """Split decoded text into complete lines plus a possibly partial last line.

    A bounded probe almost always ends mid-line. Feeding that fragment to the
    header detector as if it were a whole row would produce a row with the wrong
    field count and skew the scoring, so it is separated out here.
    """
    if not text:
        return [], ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if normalized.endswith("\n"):
        return parts[:-1], ""
    return parts[:-1], parts[-1]
