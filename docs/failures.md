# Failure Categories

Every layer of GWASPoker classifies failures through one enum, `FailureCategory`.
A failure is a *result* — it has a category, a message that names the cause, and
a place in the report.

---

## Three rules

!!! success "A failure never returns a plausible-looking value"

    If GWASPoker cannot determine something, it says so. It does not substitute
    a default that will be mistaken for a finding.

!!! success "Transient and permanent failures are different categories"

    `network_timeout` is worth retrying. `http_404` is not. Collapsing them
    would either waste requests or hide a permanent answer behind a retry loop.

!!! success "The category names the cause, not the symptom"

    A landing page served on a `.gz` URL is `non_data_response`, not
    `decompression_error`. Nothing is corrupt; the URL does not serve the file.

---

## The categories

### Upstream API

| Category | Means |
| --- | --- |
| `api_not_available` | The endpoint could not be reached. |
| `api_deprecated` | The endpoint is withdrawn — HTTP 410. The summary-statistics API answers this. |
| `api_error` | The endpoint answered with an error. |
| `api_schema_error` | The response did not match the expected schema. |
| `metadata_missing` | The study exists but the field is not populated. |
| `invalid_accession` | Not a GCST accession, a usable URL, or an existing file. |
| `not_represented` | The study is not in the GWAS Catalog. |

### Reaching the file

| Category | Means |
| --- | --- |
| `file_not_found` | No summary-statistics file resolved for the study. |
| `http_403` | Access forbidden. Terminal — no fallback attempted. |
| `http_404` | Not found. Terminal. |
| `http_error` | Another HTTP status. |
| `network_timeout` | The request timed out. Retried. |
| `network_error` | Connection failure, DNS failure, reset. Retried. |
| `range_not_supported` | The server ignored `Range`; a bounded stream was used instead. |

### What came back

| Category | Means |
| --- | --- |
| `non_data_response` | The server returned a web page, XML document, or other non-data payload. **The transfer succeeded** — this is a URL problem, not a corrupt file. |
| `content_mismatch` | The extension promised a container the bytes do not contain. Not always fatal — see below. |
| `unsupported_compression` | A recognised format that cannot be decoded from a prefix (bzip2, xz, zstd). |
| `unsupported_format` | Recognised, but with no data member to read. |
| `decompression_error` | Genuinely corrupt compressed data. |
| `encoding_error` | The bytes could not be decoded as text. |

### Reading the table

| Category | Means |
| --- | --- |
| `header_not_found` | No row scored as a header. |
| `delimiter_not_detected` | No delimiter was consistent across sampled rows. |
| `truncated_probe` | The prefix ended before a complete answer. Raise `--probe-bytes`. |
| `mapping_incomplete` | A header was found, but too few columns resolved. |

### Downloading and after

| Category | Means |
| --- | --- |
| `download_error` | The transfer failed. |
| `checksum_failed` | The file does not match its published MD5. |
| `disk_error` | Could not write locally. |
| `dependency_missing` | An optional extra is required but not installed. |
| `gwaslab_error` | The optional GWASLab hand-off failed. |
| `llm_error` | The optional sample-size model failed. |
| `unknown` | Unclassified. Should be rare; report it. |

---

## `content_mismatch` is not always fatal

Two very different situations share this classification, and GWASPoker treats
them differently.

=== "Recoverable: text under a `.gz` name"

    ```text
    Warning: study.tsv.gz is named as gzip but carries no gzip signature; the
    bytes read as text and were parsed uncompressed. The extension is wrong,
    not the file.
    ```

    A naming error on the server, over data GWASPoker can read. Refusing it
    would discard usable files, so the mismatch becomes a **warning**,
    compression is set to `NONE`, and parsing continues. The fact is still
    recorded in `warnings` and `payload.is_textual`.

=== "Fatal: binary under a `.gz` name"

    ```text
    Failure category: content_mismatch

    the filename declares gzip but the bytes carry no gzip signature; the
    extension is a hint, not evidence, so no decompressor was invoked -- the
    bytes are not readable as text either, so there is nothing to parse
    ```

    Nothing to recover.

---

## The failure log

```bash
gwaspoker run --trait migraine --limit 50 --failure-log failures.jsonl
```

Non-fatal failures accumulate in a process-wide log, are summarised at the end
of a run, and are written as JSON Lines — one object per failure, with the
category, message, the operation that produced it, and the target it concerned.

```json
{"category": "non_data_response", "target": "https://example.org/study.tsv.gz",
 "operation": "probe", "message": "the response body begins with '<!doctype html'...",
 "timestamp": "2026-08-26T10:14:03Z"}
```

This is what turns a batch run into an analysable result rather than a wall of
console output.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. **A `NOT_READY` verdict is a success** — the question was answered. |
| `1` | The operation failed. The category is printed and, with `--failure-log`, recorded. |
| `2` | The command line was wrong — unknown flag, missing argument, value out of range. |

---

## Next

- [Inspect a File](probe-scan.md) — where most of these arise
- [Architecture](architecture.md#error-handling) — the enforcement rules
