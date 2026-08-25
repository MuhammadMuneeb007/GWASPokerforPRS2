"""Compression detection and incremental decompression of partial streams.

The central requirement: recover the beginning of a compressed file's *content*
from the beginning of its *bytes*, without holding, writing or transferring the
whole file.

``zlib.decompressobj(wbits=31)`` does exactly this for gzip -- it decodes as much
as the input allows and stops, without complaining that the stream is
incomplete. A 64 KB gzip prefix typically yields a few hundred KB of text, which
is far more than a header needs.

Zip and tar are handled by reading their leading directory structures out of the
buffer. Formats that fundamentally cannot be decoded from a prefix (bzip2 needs
a complete block; xz needs its index) are detected and reported honestly rather
than guessed at.

v1 shelled out to ``gunzip -c | head``, ``zcat``, ``7z`` and ``tar``, and had a
four-deep nested fallback chain of ``subprocess.run`` calls ending in
``exit(0)``. None of it runs on Windows.
"""

from __future__ import annotations

import io
import logging
import tarfile
import zipfile
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple, Optional

from gwaspoker.failures import CompressionError, FailureCategory

logger = logging.getLogger(__name__)

#: Bytes of decompressed output that is always more than enough for a header
#: plus several data rows.
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024


class Compression(str, Enum):
    """Detected container/compression type."""

    NONE = "none"
    GZIP = "gzip"
    BGZF = "bgzf"
    ZIP = "zip"
    TAR = "tar"
    TAR_GZIP = "tar.gz"
    BZIP2 = "bzip2"
    XZ = "xz"
    ZSTD = "zstd"
    UNKNOWN = "unknown"


@dataclass
class DecompressionResult:
    """Decoded bytes from a compressed prefix, and how they were obtained."""

    compression: Compression
    data: bytes
    complete: bool
    member_name: Optional[str] = None
    note: Optional[str] = None
    compressed_bytes_consumed: int = 0

    @property
    def expansion_ratio(self) -> Optional[float]:
        if not self.compressed_bytes_consumed:
            return None
        return len(self.data) / self.compressed_bytes_consumed

    def to_dict(self) -> dict[str, object]:
        return {
            "compression": self.compression.value,
            "decompressed_bytes": len(self.data),
            "compressed_bytes_consumed": self.compressed_bytes_consumed,
            "expansion_ratio": round(self.expansion_ratio, 2) if self.expansion_ratio else None,
            "complete": self.complete,
            "member_name": self.member_name,
            "note": self.note,
        }


#: Magic-number signatures, longest first.
_MAGIC: tuple[tuple[bytes, Compression], ...] = (
    (b"PK\x03\x04", Compression.ZIP),
    (b"PK\x05\x06", Compression.ZIP),
    (b"\xfd7zXZ\x00", Compression.XZ),
    (b"BZh", Compression.BZIP2),
    (b"\x28\xb5\x2f\xfd", Compression.ZSTD),
    (b"\x1f\x8b", Compression.GZIP),
)

#: Extension hints, longest first so ".tar.gz" beats ".gz".
_EXTENSIONS: tuple[tuple[str, Compression], ...] = (
    (".tar.gz", Compression.TAR_GZIP),
    (".tar.bz2", Compression.BZIP2),
    (".tar.xz", Compression.XZ),
    (".tgz", Compression.TAR_GZIP),
    (".bgz", Compression.BGZF),
    (".gz", Compression.GZIP),
    (".zip", Compression.ZIP),
    (".tar", Compression.TAR),
    (".bz2", Compression.BZIP2),
    (".xz", Compression.XZ),
    (".zst", Compression.ZSTD),
)


def detect_compression_by_magic(data: bytes, filename: str = "") -> Optional[Compression]:
    """Compression identified from the bytes alone, or ``None``.

    This is the only *evidence-based* detector. The filename is consulted only
    to choose between ``.gz`` and ``.tar.gz`` once gzip magic has already been
    confirmed, never to claim compression that the bytes do not show.
    """
    lowered = (filename or "").lower().rstrip("/")

    for magic, compression in _MAGIC:
        if data.startswith(magic):
            if compression is Compression.GZIP:
                if _is_bgzf(data):
                    return Compression.BGZF
                # A gzip whose payload is a tar is a .tar.gz.
                if lowered.endswith((".tar.gz", ".tgz")) or _gzip_payload_is_tar(data):
                    return Compression.TAR_GZIP
            return compression

    if _looks_like_tar(data):
        return Compression.TAR
    return None


def detect_compression(data: bytes, filename: str = "") -> Compression:
    """Identify the container, preferring magic bytes over the filename.

    An external run over 768 heterogeneous URLs produced 111 gzip and 29 ZIP
    "decompression errors" that were mostly not broken archives at all: they
    were share pages and moved files whose URLs still ended in ``.gz``. Trusting
    the extension enough to invoke a decompressor turned "this is not the file"
    into "this file is corrupt".

    So the extension is now a **hint of last resort**. Callers that need to
    distinguish "no compression detected" from "the extension lied" should use
    :func:`detect_compression_by_magic` together with
    :func:`~gwaspoker.probe.payload.classify_payload_prefix`, which
    :meth:`~gwaspoker.probe.remote.RemoteProber._interpret` does.
    """
    magic = detect_compression_by_magic(data, filename)
    if magic is not None:
        return magic

    lowered = (filename or "").lower().rstrip("/")
    for suffix, compression in _EXTENSIONS:
        if lowered.endswith(suffix):
            logger.debug(
                "No magic bytes matched %s; falling back to the %s extension hint",
                filename,
                suffix,
            )
            return compression

    return Compression.NONE


def _is_bgzf(data: bytes) -> bool:
    """BGZF is gzip with an ``BC`` extra subfield -- used by tabix-indexed files."""
    if len(data) < 18 or not data.startswith(b"\x1f\x8b"):
        return False
    flg = data[3]
    if not flg & 0x04:  # FEXTRA
        return False
    xlen = int.from_bytes(data[10:12], "little")
    extra = data[12 : 12 + xlen]
    return b"BC" in extra


def _looks_like_tar(data: bytes) -> bool:
    """POSIX tar carries ``ustar`` at offset 257 of its first header block."""
    return len(data) >= 265 and data[257:262] in (b"ustar",)


def _gzip_payload_is_tar(data: bytes) -> bool:
    """Peek inside a gzip stream to see whether the payload is a tar archive."""
    try:
        head = zlib.decompressobj(31).decompress(data, 512)
    except zlib.error:
        return False
    return _looks_like_tar(head)


def decompress_prefix(
    data: bytes,
    compression: Compression,
    *,
    max_output: int = MAX_DECOMPRESSED_BYTES,
) -> DecompressionResult:
    """Decode the start of a compressed buffer.

    ``data`` is expected to be a *prefix*, so a truncated stream is normal and
    not an error. Raises :class:`CompressionError` only when the format cannot
    be decoded from a prefix at all.
    """
    if compression in (Compression.NONE, Compression.UNKNOWN):
        return DecompressionResult(
            compression=Compression.NONE,
            data=data,
            complete=True,
            compressed_bytes_consumed=len(data),
        )

    if compression in (Compression.GZIP, Compression.BGZF):
        return _decompress_gzip(data, max_output, compression)

    if compression is Compression.TAR_GZIP:
        inner = _decompress_gzip(data, max_output, Compression.GZIP)
        result = _read_tar_member(inner.data, max_output)
        result.compression = Compression.TAR_GZIP
        result.compressed_bytes_consumed = len(data)
        return result

    if compression is Compression.TAR:
        result = _read_tar_member(data, max_output)
        result.compressed_bytes_consumed = len(data)
        return result

    if compression is Compression.ZIP:
        return _read_zip_member(data, max_output)

    if compression is Compression.BZIP2:
        return _decompress_bzip2(data, max_output)

    if compression is Compression.XZ:
        return _decompress_xz(data, max_output)

    if compression is Compression.ZSTD:
        return _decompress_zstd(data, max_output)

    raise CompressionError(
        f"Unsupported compression: {compression.value}",
        category=FailureCategory.UNSUPPORTED_COMPRESSION,
    )


def _decompress_gzip(data: bytes, max_output: int, kind: Compression) -> DecompressionResult:
    """Incrementally inflate a gzip prefix.

    ``wbits=31`` selects gzip framing. BGZF is a sequence of complete gzip
    members, so each is inflated in turn until the buffer runs out.
    """
    output = bytearray()
    consumed = 0
    remaining = data
    members = 0

    while remaining and len(output) < max_output:
        decompressor = zlib.decompressobj(31)
        try:
            chunk = decompressor.decompress(remaining, max_output - len(output))
        except zlib.error as exc:
            if output:
                # The first member(s) decoded; a later one is cut short. Fine.
                break
            raise CompressionError(f"gzip stream could not be decoded: {exc}") from exc
        output.extend(chunk)
        members += 1
        consumed = len(data) - len(decompressor.unused_data)
        if not decompressor.eof:
            # Ran out of input mid-member: expected for a bounded probe.
            return DecompressionResult(
                compression=kind,
                data=bytes(output),
                complete=False,
                note="gzip stream truncated at the probe boundary, as intended",
                compressed_bytes_consumed=len(data),
            )
        remaining = decompressor.unused_data
        if not remaining:
            return DecompressionResult(
                compression=kind,
                data=bytes(output),
                complete=True,
                note=f"{members} complete gzip member(s)" if members > 1 else None,
                compressed_bytes_consumed=consumed,
            )

    return DecompressionResult(
        compression=kind,
        data=bytes(output),
        complete=False,
        note="output limit reached" if len(output) >= max_output else None,
        compressed_bytes_consumed=consumed or len(data),
    )


def _read_zip_member(data: bytes, max_output: int) -> DecompressionResult:
    """Read the first plausible data member from a zip prefix.

    Zip stores its central directory at the *end* of the archive, which a prefix
    does not contain. :class:`zipfile.ZipFile` therefore fails, and we fall back
    to inflating the first local file header directly.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = _pick_archive_member([i.filename for i in archive.infolist()])
            if member is None:
                raise CompressionError(
                    "No data-like member found in the zip archive",
                    category=FailureCategory.UNSUPPORTED_FORMAT,
                )
            with archive.open(member) as handle:
                payload = handle.read(max_output)
            return DecompressionResult(
                compression=Compression.ZIP,
                data=payload,
                complete=True,
                member_name=member,
                compressed_bytes_consumed=len(data),
            )
    except (zipfile.BadZipFile, EOFError, KeyError):
        logger.debug("Zip central directory absent from prefix; reading the local header")

    return _read_zip_local_header(data, max_output)


def _read_zip_local_header(data: bytes, max_output: int) -> DecompressionResult:
    """Read the first *data-like* member using only local file headers.

    Zip keeps its index (the central directory) at the **end** of the archive,
    which a bounded prefix does not contain, so member selection has to be done
    by walking the chain of local headers from the front.

    This used to take the first member unconditionally. Real archives routinely
    put something else there -- a ``__MACOSX/`` entry, a directory record, a
    README, a manuscript PDF -- so the probe would score documentation prose as
    a header and report the file as having no recognisable columns. The walk
    below skips those and reports how many it passed.
    """
    if len(data) < 30 or not data.startswith(_ZIP_LOCAL_SIG):
        raise CompressionError(
            "Zip prefix is too short to contain a local file header",
            category=FailureCategory.DECOMPRESSION_ERROR,
        )
    return _walk_zip_prefix(data, max_output)


class _ZipLocalHeader(NamedTuple):
    """The fields of a local file header that member selection needs."""

    name: str
    method: int
    #: ``None`` when the header defers sizes to a trailing data descriptor
    #: (general-purpose bit 3), which is common for zips written to a stream.
    compressed_size: Optional[int]
    body_start: int


def _zip64_compressed_size(extra: bytes, uncompressed_is_zip64: bool) -> Optional[int]:
    """Pull the 64-bit compressed size out of the Zip64 extra field.

    The Zip64 record lists only the fields that overflowed, in a fixed order
    (uncompressed, then compressed), so the offset of the compressed size
    depends on whether the uncompressed size also overflowed.
    """
    pos = 0
    while pos + 4 <= len(extra):
        field_id = int.from_bytes(extra[pos : pos + 2], "little")
        field_len = int.from_bytes(extra[pos + 2 : pos + 4], "little")
        payload = extra[pos + 4 : pos + 4 + field_len]
        if field_id == _ZIP64_EXTRA_ID:
            start = 8 if uncompressed_is_zip64 else 0
            if len(payload) >= start + 8:
                return int.from_bytes(payload[start : start + 8], "little")
            return None
        pos += 4 + field_len
    return None


def _parse_zip_local_header(data: bytes, offset: int) -> Optional[_ZipLocalHeader]:
    """Decode one local file header, or return ``None`` if there isn't one."""
    if offset < 0 or offset + 30 > len(data):
        return None
    if data[offset : offset + 4] != _ZIP_LOCAL_SIG:
        return None

    flags = int.from_bytes(data[offset + 6 : offset + 8], "little")
    method = int.from_bytes(data[offset + 8 : offset + 10], "little")
    compressed = int.from_bytes(data[offset + 18 : offset + 22], "little")
    uncompressed = int.from_bytes(data[offset + 22 : offset + 26], "little")
    name_len = int.from_bytes(data[offset + 26 : offset + 28], "little")
    extra_len = int.from_bytes(data[offset + 28 : offset + 30], "little")

    if not 0 < name_len <= _ZIP_MAX_NAME_LENGTH or extra_len > _ZIP_MAX_EXTRA_LENGTH:
        return None

    name_end = offset + 30 + name_len
    if name_end > len(data):
        return None
    raw_name = data[offset + 30 : name_end]
    if any(byte < 0x20 for byte in raw_name):
        return None
    name = raw_name.decode("utf-8", errors="replace")

    if compressed == 0xFFFFFFFF:
        extra = data[name_end : name_end + extra_len]
        compressed = _zip64_compressed_size(extra, uncompressed == 0xFFFFFFFF) or compressed

    # Bit 3 means "sizes follow the data", so the header's zero is not a size.
    streamed = bool(flags & 0x08) and compressed == 0
    return _ZipLocalHeader(
        name=name,
        method=method,
        compressed_size=None if streamed else compressed,
        body_start=name_end + extra_len,
    )


def _find_next_zip_header(data: bytes, start: int) -> Optional[int]:
    """Scan forward for the next plausible local header.

    Needed only when a member's size was deferred to a data descriptor and so
    cannot be skipped arithmetically. The signature can occur by chance inside
    compressed bytes, so each hit is re-parsed and rejected unless its fields
    are self-consistent.
    """
    pos = start
    while 0 <= pos < len(data):
        pos = data.find(_ZIP_LOCAL_SIG, pos)
        if pos < 0:
            return None
        if _parse_zip_local_header(data, pos) is not None:
            return pos
        pos += 4
    return None


def _walk_zip_prefix(data: bytes, max_output: int) -> DecompressionResult:
    """Follow the local-header chain to the first member worth reading."""
    offset: Optional[int] = 0
    skipped: list[str] = []
    fallback: Optional[_ZipLocalHeader] = None

    for _ in range(_ZIP_MAX_MEMBERS_WALKED):
        if offset is None:
            break
        header = _parse_zip_local_header(data, offset)
        if header is None:
            break

        if not _is_archive_directory(header.name) and not _is_archive_noise(header.name):
            if fallback is None:
                fallback = header
            return _inflate_zip_member(data, header, max_output, skipped)

        skipped.append(header.name)
        if header.compressed_size:
            offset = header.body_start + header.compressed_size
        elif header.compressed_size == 0 and _is_archive_directory(header.name):
            offset = header.body_start
        else:
            offset = _find_next_zip_header(data, max(header.body_start, offset + 4))

    if fallback is not None:
        return _inflate_zip_member(data, fallback, max_output, skipped)

    if skipped:
        raise CompressionError(
            "The zip prefix contains only non-data members "
            f"({', '.join(skipped[:5])}); the summary statistics, if present, "
            "start beyond the probe boundary. Increase --probe-bytes to reach them.",
            category=FailureCategory.UNSUPPORTED_FORMAT,
        )
    raise CompressionError(
        "No readable member was found in the zip prefix",
        category=FailureCategory.DECOMPRESSION_ERROR,
    )


def _inflate_zip_member(
    data: bytes,
    header: _ZipLocalHeader,
    max_output: int,
    skipped: list[str],
) -> DecompressionResult:
    """Decode one member's payload out of the prefix."""
    body = data[header.body_start :]
    if header.compressed_size is not None:
        body = body[: header.compressed_size]
    truncated = header.compressed_size is None or len(body) < header.compressed_size

    notes: list[str] = []
    if skipped:
        notes.append(f"walked {len(skipped)} non-data member(s) to reach {header.name!r}")

    if header.method == _ZIP_STORED:
        payload = body[:max_output]
        notes.append("stored (uncompressed) zip member read from the local header")
        return DecompressionResult(
            compression=Compression.ZIP,
            data=payload,
            complete=not truncated and len(payload) >= len(body),
            member_name=header.name,
            note="; ".join(notes) or None,
            compressed_bytes_consumed=len(data),
        )

    if header.method != _ZIP_DEFLATED:
        raise CompressionError(
            f"Zip compression method {header.method} is not supported from a prefix",
            category=FailureCategory.UNSUPPORTED_COMPRESSION,
        )

    try:
        payload = zlib.decompressobj(-15).decompress(body, max_output)
    except zlib.error as exc:
        raise CompressionError(f"Zip member could not be inflated: {exc}") from exc

    if not payload and not body:
        raise CompressionError(
            f"Zip member {header.name!r} starts beyond the probe boundary; "
            "increase --probe-bytes to reach it",
            category=FailureCategory.TRUNCATED_PROBE,
        )

    notes.append("inflated from the local file header; central directory not in prefix")
    return DecompressionResult(
        compression=Compression.ZIP,
        data=payload,
        complete=False,
        member_name=header.name,
        note="; ".join(notes),
        compressed_bytes_consumed=len(data),
    )


def _read_tar_member(data: bytes, max_output: int) -> DecompressionResult:
    """Read the first data-like member from a tar prefix."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            names = [m.name for m in archive.getmembers() if m.isfile()]
            member = _pick_archive_member(names)
            if member is None:
                raise CompressionError(
                    "No data-like member found in the tar archive",
                    category=FailureCategory.UNSUPPORTED_FORMAT,
                )
            handle = archive.extractfile(member)
            payload = handle.read(max_output) if handle else b""
            return DecompressionResult(
                compression=Compression.TAR,
                data=payload,
                complete=True,
                member_name=member,
            )
    except tarfile.TarError:
        logger.debug("Tar index incomplete in prefix; reading the first header block")

    # Walk the member headers ourselves. A prefix has no archive index, but it
    # does contain a chain of 512-byte headers, and each states its own size.
    return _walk_tar_prefix(data, max_output)


#: Tar typeflags that are not a regular data file.
#: ``5`` directory, ``1``/``2`` links, ``x``/``g`` PAX metadata,
#: ``L``/``K`` GNU long name/link, ``V`` volume label.
_TAR_SKIP_TYPEFLAGS = frozenset(b"1234567xgLKV")


def _parse_tar_octal(field: bytes) -> int:
    """Parse a tar numeric field. Returns 0 for the many malformed variants."""
    text = field.split(b"\x00")[0].split(b" ")[0].strip()
    if not text:
        return 0
    try:
        return int(text, 8)
    except ValueError:
        return 0


def _walk_tar_prefix(data: bytes, max_output: int) -> DecompressionResult:
    """Find the first real data member in a truncated tar stream.

    The previous implementation skipped exactly one 512-byte header and treated
    whatever followed as the payload. That is right only when the first member
    is the data file. It is wrong when the archive opens with a directory entry,
    a PAX extended header, a GNU long-name record or a ``._``-prefixed macOS
    resource fork -- which is common, and which is why 36 valid ``ustar``
    archives in the external run failed to yield their headers even though the
    bytes were present.

    This walks the header chain instead, honouring each member's declared size,
    and returns the first member that is a regular file with a data-like name.
    """
    if len(data) < 512:
        raise CompressionError(
            "Tar prefix is shorter than one header block",
            category=FailureCategory.DECOMPRESSION_ERROR,
        )

    offset = 0
    skipped: list[str] = []
    fallback: Optional[tuple[str, int, int]] = None  # name, start, size

    while offset + 512 <= len(data):
        header = data[offset : offset + 512]
        if header == b"\x00" * 512:  # end-of-archive marker
            break

        name = header[:100].split(b"\x00")[0].decode("utf-8", errors="replace")
        if not name:
            break

        size = _parse_tar_octal(header[124:136])
        typeflag = header[156:157]
        start = offset + 512
        # Members are padded to a 512-byte boundary.
        advance = 512 + ((size + 511) // 512) * 512

        is_regular = typeflag in (b"0", b"\x00", b"") or typeflag not in _TAR_SKIP_TYPEFLAGS
        noisy = _is_archive_noise(name)

        if is_regular and size > 0 and not noisy:
            if _pick_archive_member([name]) == name:
                payload = data[start : start + min(size, max_output)]
                return DecompressionResult(
                    compression=Compression.TAR,
                    data=payload,
                    complete=len(payload) >= size,
                    member_name=name,
                    note=(
                        f"walked {len(skipped)} non-data member(s) to reach {name!r}"
                        if skipped
                        else None
                    ),
                )
            if fallback is None:
                fallback = (name, start, size)
        else:
            skipped.append(name)

        if advance <= 0:
            break
        offset += advance

    # No member matched a known data extension. Use the first regular file we
    # saw rather than giving up -- an extensionless member is still readable.
    if fallback is not None:
        name, start, size = fallback
        payload = data[start : start + min(size, max_output)]
        return DecompressionResult(
            compression=Compression.TAR,
            data=payload,
            complete=len(payload) >= size,
            member_name=name,
            note=(
                f"no member carried a known data extension; using the first regular "
                f"file {name!r}"
                + (f" after skipping {len(skipped)} metadata member(s)" if skipped else "")
            ),
        )

    raise CompressionError(
        "No regular data member was found in the tar prefix"
        + (f" (skipped: {', '.join(skipped[:5])})" if skipped else ""),
        category=FailureCategory.UNSUPPORTED_FORMAT,
    )


def _decompress_bzip2(data: bytes, max_output: int) -> DecompressionResult:
    import bz2

    try:
        payload = bz2.BZ2Decompressor().decompress(data, max_output)
    except (OSError, ValueError, EOFError) as exc:
        raise CompressionError(
            "bzip2 needs a complete 900 KB block before any output can be produced; "
            f"the probe prefix was insufficient ({exc}). Increase --probe-bytes or "
            "download the file.",
            category=FailureCategory.DECOMPRESSION_ERROR,
        ) from exc
    return DecompressionResult(
        compression=Compression.BZIP2,
        data=payload,
        complete=False,
        compressed_bytes_consumed=len(data),
    )


def _decompress_xz(data: bytes, max_output: int) -> DecompressionResult:
    import lzma

    try:
        payload = lzma.LZMADecompressor().decompress(data, max_output)
    except lzma.LZMAError as exc:
        raise CompressionError(
            f"xz stream could not be decoded from a prefix: {exc}",
            category=FailureCategory.DECOMPRESSION_ERROR,
        ) from exc
    return DecompressionResult(
        compression=Compression.XZ,
        data=payload,
        complete=False,
        compressed_bytes_consumed=len(data),
    )


def _decompress_zstd(data: bytes, max_output: int) -> DecompressionResult:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CompressionError(
                "Zstandard support needs Python 3.14+ or the 'zstandard' package",
                category=FailureCategory.UNSUPPORTED_COMPRESSION,
            ) from exc
        decompressor = zstandard.ZstdDecompressor().decompressobj()
        try:
            payload = decompressor.decompress(data)[:max_output]
        except zstandard.ZstdError as exc:
            raise CompressionError(f"zstd stream could not be decoded: {exc}") from exc
    else:
        try:
            payload = zstd.ZstdDecompressor().decompress(data)[:max_output]
        except Exception as exc:  # the stdlib module raises its own error type
            raise CompressionError(f"zstd stream could not be decoded: {exc}") from exc
    return DecompressionResult(
        compression=Compression.ZSTD,
        data=payload,
        complete=False,
        compressed_bytes_consumed=len(data),
    )


#: Extensions that plausibly hold summary statistics, in preference order.
#: Zip structural signatures and header limits used when walking a prefix.
_ZIP_LOCAL_SIG = b"PK\x03\x04"
_ZIP64_EXTRA_ID = 0x0001
_ZIP_STORED = 0
_ZIP_DEFLATED = 8
#: Sanity bounds for rejecting a signature that occurred by chance inside
#: compressed bytes rather than at a real header.
_ZIP_MAX_NAME_LENGTH = 4096
_ZIP_MAX_EXTRA_LENGTH = 65535
#: A prefix that needs more hops than this is not going to yield data usefully.
_ZIP_MAX_MEMBERS_WALKED = 64

_DATA_EXTENSIONS: tuple[str, ...] = (
    ".tsv",
    ".txt",
    ".csv",
    ".ma",
    ".assoc",
    ".meta",
    ".tbl",
    ".linear",
    ".logistic",
    ".sumstats",
    ".gwas",
    ".out",
    ".regenie",
)

#: Members that are never the data file.
#: Extensions that are never summary statistics. Matched with ``endswith`` (and
#: tolerating a trailing ``.gz``), not with ``in``: a substring test would reject
#: ``study.results.txt`` for containing ``.r``.
_ARCHIVE_NOISE_EXTENSIONS: tuple[str, ...] = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".tif",
    ".tiff",
    ".svg",
    ".bmp",
    ".xlsx",
    ".xls",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".md5",
    ".sha256",
    ".html",
    ".htm",
    ".xml",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".bib",
    ".r",
    ".py",
    ".sh",
    ".pl",
    ".zip",
    ".bam",
    ".bai",
    ".vcf",
    ".idx",
    ".tbi",
)

#: Name fragments that mark a member as documentation or filesystem litter.
#: Matched anywhere in the path, because these appear with many extensions
#: (``README``, ``README.txt``, ``docs/readme.md``).
_ARCHIVE_NOISE_NAMES: tuple[str, ...] = (
    "readme",
    "license",
    "licence",
    "copying",
    "changelog",
    "manifest",
    "__macosx",
    ".ds_store",
    "thumbs.db",
)

#: Retained as the union of both lists for callers that only need one sequence.
_ARCHIVE_NOISE: tuple[str, ...] = _ARCHIVE_NOISE_EXTENSIONS + _ARCHIVE_NOISE_NAMES


def _is_archive_noise(name: str) -> bool:
    """True when an archive member is documentation, media or filesystem litter.

    v1 selected the largest member of an archive, which picks the bundled
    manuscript PDF whenever one is present. Selecting by name instead means the
    choice is explainable, and a wrong choice is visible in ``member_name``.
    """
    lowered = name.lower().rstrip("/")
    if not lowered:
        return True
    if any(fragment in lowered for fragment in _ARCHIVE_NOISE_NAMES):
        return True
    stem = lowered[:-3] if lowered.endswith(".gz") else lowered
    return stem.endswith(_ARCHIVE_NOISE_EXTENSIONS)


def _is_archive_directory(name: str) -> bool:
    """True for directory entries, which carry no payload."""
    return name.endswith("/") or name.endswith("\\")


def _pick_archive_member(names: list[str]) -> Optional[str]:
    """Choose the member of an archive most likely to be the summary statistics.

    Preference order: a known data extension, then anything not obviously noise.
    Directory entries and macOS resource forks are excluded. v1 instead took the
    largest member, which selects a bundled PDF when one is present.
    """
    candidates = [n for n in names if not _is_archive_directory(n) and not _is_archive_noise(n)]
    if not candidates:
        return None
    for extension in _DATA_EXTENSIONS:
        for name in candidates:
            lowered = name.lower()
            if lowered.endswith(extension) or lowered.endswith(extension + ".gz"):
                return name
    return candidates[0]
