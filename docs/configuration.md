# Configuration

Every setting has a default that works. Configuration exists for the cases where
it does not.

---

## Precedence

Lowest to highest — a later source overrides an earlier one:

1. The **defaults** on `GWASPokerConfig`
2. A **config file** — `--config PATH`, else `./gwaspoker.toml`, else
   `./gwaspoker.yaml`, else `~/.config/gwaspoker/config.toml`
3. **Environment variables** prefixed `GWASPOKER_`
4. **Explicit CLI options**

```bash
gwaspoker assess GCST90271311 --config ./project.toml --probe-bytes 524288
```

Here the file supplies everything, and `--probe-bytes` overrides whatever the
file said.

---

## A config file

=== "TOML"

    ```toml
    # gwaspoker.toml
    [network]
    request_timeout = 90.0
    max_requests_per_second = 6.0
    max_workers = 8

    [probe]
    probe_bytes = 524288
    sample_rows = 200
    validation_rows = 200

    [download]
    download_dir = "./sumstats"
    verify_checksum = true
    ```

=== "YAML"

    ```yaml
    # gwaspoker.yaml
    network:
      request_timeout: 90.0
      max_requests_per_second: 6.0
      max_workers: 8

    probe:
      probe_bytes: 524288
      sample_rows: 200
      validation_rows: 200

    download:
      download_dir: ./sumstats
      verify_checksum: true
    ```

=== "Environment"

    ```bash
    export GWASPOKER_PROBE_BYTES=524288
    export GWASPOKER_MAX_REQUESTS_PER_SECOND=6
    export GWASPOKER_DOWNLOAD_DIR=./sumstats
    ```

---

## Settings

### Networking

| Setting | Default | Notes |
| --- | --- | --- |
| `request_timeout` | `60.0` | Seconds for a whole request. |
| `connect_timeout` | `15.0` | Seconds to establish a connection. |
| `max_retries` | `3` | Retries for *transient* failures only. 403/404/410 are never retried. |
| `retry_backoff` | `0.5` | Exponential backoff base, in seconds. |
| `max_requests_per_second` | `8.0` | Process-wide, shared by every thread. |
| `max_workers` | `6` | Threads for the file-availability stage. |
| `user_agent` | derived from the version | Sent on every request. |

!!! warning "Raise the request rate carefully"

    The GWAS Catalog documents 15 queries/second **for REST API v2**. That
    figure is *not* documented for `ftp.ebi.ac.uk`, which is where the
    file-availability checks actually go — so the default of 8 is deliberately
    conservative and is **not** derived from the v2 limit.

    `max_workers` overlaps latency; it does not raise the request rate. The
    limiter is the floor.

### Upstream endpoints

| Setting | Default |
| --- | --- |
| `rest_api_v2_base` | `https://www.ebi.ac.uk/gwas/rest/api/v2` |
| `rest_api_v1_base` | `https://www.ebi.ac.uk/gwas/rest/api` |
| `solr_search_base` | `https://www.ebi.ac.uk/gwas/api/search` |
| `sumstats_api_base` | `https://www.ebi.ac.uk/gwas/summary-statistics/api` |
| `ftp_base` | `https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics` |
| `prefer_api_version` | `auto` — v2, falling back to v1 |
| `api_page_size` | `20` |

The summary-statistics API is **withdrawn** and answers HTTP 410. GWASPoker
still queries it and records the 410 as a fact, rather than assuming it. See
[Data Sources](data-sources.md).

### Probing

| Setting | Default | Notes |
| --- | --- | --- |
| `probe_bytes` | `262144` (256 KB) | The byte ceiling, in every transport path. |
| `max_header_scan_lines` | `200` | How far into a preamble to look for a header. |
| `sample_rows` | `50` | Rows kept for structure checks. |
| `validation_rows` | `50` | Rows sampled for value validation. |
| `prefer_harmonised` | `auto` | `auto`, `yes` or `no`. |
| `default_search_limit` | `25` | |

**Choosing `probe_bytes`:** 64 KB resolves most GWAS-SSF-like files. 256 KB is
the default because it also covers files with long metadata preambles. 1 MB is
rarely needed except for archives whose data sits behind large members.

### Sample-size resolution

| Setting | Default | Notes |
| --- | --- | --- |
| `enable_llm_fallback` | `False` | Off by default. |
| `llm_model` | `ahotrod/electra_large_discriminator_squad2_512` | |
| `llm_device` | `auto` | |

A sample size obtained from the model is **labelled as such** in every report,
so it is never mistaken for a curated figure.

### Downloading

| Setting | Default | Notes |
| --- | --- | --- |
| `download_dir` | current directory | |
| `verify_checksum` | `True` | MD5 against the published checksum. |
| `download_chunk_bytes` | `1048576` (1 MB) | |
| `allow_resume` | `True` | Resume via a range request when supported. |

### Output

| Setting | Default |
| --- | --- |
| `provenance_path` | `None` |
| `failure_log_path` | `None` |

---

## Seeing what is in effect

```bash
gwaspoker assess GCST90271311 --provenance run.json
```

The provenance file contains the **fully resolved** configuration — every value
after all four precedence layers have been applied. That is what should be
reported alongside results, not the config file you wrote.

---

## Next

- [Reproducibility](reproducibility.md) — what to record for a paper
- [Data Sources](data-sources.md) — every upstream interface
