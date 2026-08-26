# Inspect a File

Two commands read a file's structure without judging it against PRS
requirements: `probe` for something remote, `scan` for something local.

---

## `scan` — a file you already have

```bash
gwaspoker scan ./downloaded.tsv.gz
```

No network at all. Reports format, compression, encoding, delimiter, the header
row, the canonical mapping, value validation and a readiness verdict.

```text
Input type: local file

Format            TSV.ZIP
Encoding          utf-8 (100% confidence)
Decompressed      11.16 KB (14.9x expansion)

Detected header (row 0, tab-separated, 72% confidence)
  CHR  BP  SNP  A1  A2  BETA  SE  P  N

Value validation: PASS over 50 sampled row(s) (of 50 retained by the probe).
PRS readiness: READY
Evidence: local_file
```

### Formats it reads

`.tsv` · `.csv` · `.txt` · `.tab` · `.gz` · `.bgz` (BGZF) · `.zip` · `.tar` ·
`.tar.gz` · `.tgz` · `.bz2`

!!! info "A local scan is still bounded"

    `--probe-bytes` applies to local files too, which is why a 10 GB file scans
    as fast as a small one. Raise it if you want more rows validated.

### Generating a loader

```bash
gwaspoker scan ./downloaded.tsv.gz --emit-code
```

Prints a `pandas.read_csv` call with the detected delimiter, encoding, header
row and comment character already filled in — so the file that GWASPoker just
parsed correctly is loaded the same way downstream.

---

## `probe` — a file on a server

```bash
gwaspoker probe GCST90271311
gwaspoker probe https://some-consortium.org/gwas.txt.gz
gwaspoker probe GCST90271311 --probe-bytes 262144
```

Fetches a bounded prefix and reports what is in it. `probe` deliberately stops
short of a PRS verdict — that is `assess`.

### What "bounded" means

```text
Remote size       377.78 MB
Bytes inspected   256.00 KB
Transfer avoided  99.9339%
Range requests    supported
```

Three transport attempts at most, and every one is recorded with its method,
status, byte count and duration:

1. **`HEAD`** — for the size and range support. **Advisory**: a server that
   rejects or hangs on HEAD does not abort the probe.
2. **`GET` with `Range: bytes=0-N`** — the normal path.
3. **A bounded `GET`** — when the server ignores ranges, GWASPoker closes the
   connection itself at the limit.

`403`, `404` and `410` are terminal: no fallback is attempted, because a second
request cannot change the answer.

!!! note "Byte accounting includes failed attempts"

    A stream that delivered 96 KiB and then reset moved 96 KiB. Those bytes are
    counted, because `Bytes inspected` claims to be exactly what crossed the
    network.

### Decompression from a prefix

A prefix is not a complete archive, so GWASPoker decodes what it can:

| Format | How a prefix is handled |
| --- | --- |
| gzip, BGZF | `zlib.decompressobj(wbits=31)` inflates incrementally and stops cleanly at the truncation point. A 64 KB prefix typically yields several hundred KB of text. |
| zip | The central directory lives at the *end* of the archive, past every probe boundary, so members are read from local file headers — including Zip64 sizes and the streamed form where sizes trail the data. |
| tar, tar.gz | The member chain is walked, skipping directory records, PAX/GNU headers and documentation. |
| bzip2, xz, zstd | Cannot be decoded from an arbitrary prefix; reported as such rather than guessed at. |

Archives put things in front of the data — `__MACOSX/` forks, READMEs, the
manuscript PDF. GWASPoker walks past them and reports how many it skipped:

```text
walked 4 non-data member(s) to reach 'study/sumstats.tsv'
```

---

## When a server returns something else

A URL ending `.gz` that responds with HTML is not a corrupt archive. GWASPoker
classifies the payload *before* attempting to decode it:

```text
Failure category: non_data_response

the response body begins with '<!doctype html', so the server returned a web
page or XML document rather than summary statistics; Content-Type was text/html;
the transfer itself succeeded, so this is a URL problem rather than a corrupt
file
```

A `.gz` that turns out to be ordinary text is a different case — the extension
is wrong, not the file, so it is read as text with a warning:

```text
Warning: study.tsv.gz is named as gzip but carries no gzip signature; the bytes
read as text and were parsed uncompressed. The extension is wrong, not the file.
```

See [Failure Categories](failures.md) for the full list.

---

## Next

- [Direct URLs and Local Files](inputs.md) — what counts as a target
- [Assess for PRS](assess.md) — from structure to a verdict
- [CLI Reference](cli-reference.md#probe) — every flag
