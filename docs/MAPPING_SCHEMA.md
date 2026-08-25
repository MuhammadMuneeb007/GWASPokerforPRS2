# Mapping schema and PRS-readiness rules

Two things are specified here:

1. the canonical column vocabulary and how raw header names are mapped onto it;
2. the exact rules that turn a mapped header into `READY`, `PARTIAL`,
   `NOT_READY` or `UNKNOWN`.

Both are implemented once — in `src/gwaspoker/mapping/aliases.yaml` plus
`mapping/mapper.py`, and in `readiness/prs.py` — and this document is the
human-readable statement of them.

---

## Part 1: Canonical column vocabulary

### The single source of truth

`src/gwaspoker/mapping/aliases.yaml` is the **only** alias resource in the
package. v1 kept fourteen Python lists duplicated verbatim across three modules;
`tests/test_mapping.py` now enforces that the vocabulary is internally
consistent and that the v1 defects cannot return.

### Structure

```yaml
concepts:
  p_value:
    description: Association p-value on the probability scale (0, 1].
    prs_tool_symbol: P
    category: significance
    aliases: [p_value, pvalue, p, pval, ...]

heuristics:
  suffix:  [{suffix: _pval, concept: p_value, confidence: 0.72}, ...]
  prefix:  [{prefix: hm_, strip: true, confidence: 0.9}]
  contains: [{contains: standard_error, concept: standard_error, confidence: 0.8}]
```

| Key | Meaning |
| --- | --- |
| `description` | What the column means, scientifically |
| `prs_tool_symbol` | Short symbol used by PLINK/PRSice-style tools |
| `category` | `identification`, `allele`, `effect`, `significance`, `frequency`, `sample`, `quality`, `meta` |
| `aliases` | Curated exact aliases, compared after normalization |

### Canonical concepts

| Concept | Symbol | Category | Notes |
| --- | --- | --- | --- |
| `chromosome` | CHR | identification | |
| `position` | BP | identification | |
| `chromosome_position` | CHRPOS | identification | Combined `chr:pos`; must be split before use |
| `variant_id` | SNP | identification | rsID or structured id |
| `effect_allele` | A1 | allele | |
| `other_allele` | A2 | allele | |
| `beta` | BETA | effect | Directly usable as a weight |
| `odds_ratio` | OR | effect | Needs a log transform |
| `hazard_ratio` | HR | effect | Needs a log transform |
| `z_score` | Z | effect | Needs EAF and N to become a beta |
| `standard_error` | SE | effect | |
| `confidence_interval_lower` / `_upper` | CIL / CIU | effect | |
| `p_value` | P | significance | Probability scale |
| `neg_log10_p_value` | NEGLOG10P | significance | **Not** a p-value |
| `heterogeneity_p_value` | HETP | significance | Cochran's Q; not the association p |
| `effect_allele_frequency` | EAF | frequency | Preferred over MAF |
| `minor_allele_frequency` | MAF | frequency | Population-dependent |
| `allele_frequency` | AF | frequency | Reference allele unstated |
| `sample_size` | N | sample | |
| `cases` | NCAS | sample | |
| `controls` | NCON | sample | |
| `info_score` | INFO | quality | |
| `direction` | D | meta | |
| `n_studies` | NSTUDY | meta | |

### Normalization

Applied to both the alias list and the raw column before comparison
(`mapping/normalize.py`):

1. strip a UTF-8 BOM;
2. NFKD-normalize, drop combining marks;
3. case-fold;
4. replace runs of whitespace and punctuation
   (`- . : ; , / \ | ( ) [ ] { } ' " backtick + * # @ ! ? % ^ & = < > ~ $`) with `_`;
5. collapse repeated `_`;
6. trim leading and trailing `_`.

So `"P-Value"`, `p.value`, `P_VALUE` and `P Value` all reach one entry, and the
YAML lists `p_value` once. The raw name is carried alongside the normalized one
everywhere, so a report never shows a name that is not in the user's file.

### The three mapping layers

| Layer | Rule | Confidence | `mapping_method` |
| --- | --- | --- | --- |
| 1 | The normalized name *is* a canonical concept name | 1.00 | `canonical` |
| 2 | The normalized name is a curated alias | 0.95 | `alias` |
| 3 | A heuristic matches | 0.60–0.90 | `heuristic` |
| — | Nothing matches | 0.00 | `unknown` |

**Layer 3 heuristics**, in order:

* **`hm_` prefix** (confidence 0.90) — strip it and retry layers 1 and 2. This
  covers the whole harmonised GWAS Catalog column set from one rule.
* **Suffix** — `_pval`, `_pvalue`, `_p_value` (0.72); `_beta` (0.68); `_se`
  (0.68); `_stderr` (0.70); `_or` (0.60); `_maf` (0.68); `_eaf` (0.70);
  `_zscore` (0.70). The `_pval` rule alone replaces roughly 250 hand-enumerated
  metabolomics aliases in v1's `p_value_list`.
* **Substring** — only for phrases with no competing reading:
  `base_pair_location`, `effect_allele_freq`, `standard_error`, `odds_ratio`,
  `hazard_ratio` (0.78–0.80).

Every mapping returns `raw_name`, `canonical_name`, `mapping_method` and
`confidence`. An unresolved column is reported as `unknown` and listed in
`unidentified_columns` — it is **never** forced onto a concept.

### Invariants the tests enforce

* an alias belongs to exactly one concept — a violation raises at load time;
* no alias repeats within a concept;
* no alias is an unvetted concatenation of two other aliases (v1's
  missing-comma signature);
* the five known v1 corruptions are absent;
* fragment aliases (`e`, `_value`, `pair`, `base`, `standard`, `_error`, `odds`)
  are absent;
* normalizing an alias is idempotent.

### Corrections carried out from v1

| v1 behaviour | v2 |
| --- | --- |
| `neg_log_10_p_value` aliased to `p_value` | its own concept `neg_log10_p_value` |
| `p_bolt_lmm_inf` listed under `beta_list` | mapped to `p_value` |
| `a1`, `allele1` in **both** allele lists | `a1`/`allele1` to `effect_allele`; `a0`/`allele0` to `other_allele` |
| `N_list` mixing N, cases and controls | three separate concepts |
| `minorallelefrequency` under `effect_allele_list` | `effect_allele_frequency` |
| `weight` under `N_list` | dropped (meta-analysis weight) |
| `effect_all` in both `beta_list` and `effect_allele_list` | dropped from both — genuinely ambiguous |
| five aliases corrupted by missing commas | absent; a test prevents recurrence |

---

## Part 2: PRS-readiness rules

Implemented in `readiness/prs.py` as data (`PRS_REQUIRED`, `PRS_RECOMMENDED`),
evaluated by one function. Adding a target workflow means adding a table, not
writing code.

### Why these five requirements

A polygenic risk score is a weighted sum of allele dosages. For each variant you
must be able to answer:

1. **Which variant is this?** To align against a genotype file or LD panel.
2. **Which allele carries the effect?** Without it the sign of every weight is
   undefined.
3. **What is the other allele?** To resolve strand ambiguity and to distinguish
   multi-allelic variants at one locus.
4. **How large is the effect?** The weight itself.
5. **How certain is it?** For the thresholding and clumping that nearly every
   PRS method performs.

### Required fields

| Key | Satisfied by | Rule |
| --- | --- | --- |
| `variant_identification` | `variant_id` **or** `chromosome_position` **or** (`chromosome` **and** `position`) | Either identifier form suffices |
| `effect_allele` | `effect_allele` | |
| `other_allele` | `other_allele` | |
| `effect_size` | `beta` **or** `odds_ratio` **or** `hazard_ratio` **or** `z_score` | Any one |
| `significance` | `p_value` **or** `neg_log10_p_value` | Either scale |

### Recommended fields

Their absence rules out particular methods, not PRS in general.

| Key | Satisfied by | Why it matters |
| --- | --- | --- |
| `standard_error` | `standard_error` | Required by LDpred2, PRS-CS and other Bayesian methods |
| `sample_size` | `sample_size` **or** `cases` **or** `controls` | Some methods accept a study-level N instead of per-variant |
| `allele_frequency` | `effect_allele_frequency` **or** `minor_allele_frequency` **or** `allele_frequency` | Frequency filtering; z-score to beta conversion |
| `imputation_quality` | `info_score` | Excluding poorly imputed variants |

### Requirement status

A requirement's status comes from the **confidence of the mapping that satisfies
it** — so a column matched by a curated alias is stronger evidence than one
matched by a suffix heuristic, and the difference is visible.

| Confidence | Status | Symbol |
| --- | --- | --- |
| ≥ 0.90 | `satisfied` | ✓ |
| 0.50 – 0.89 | `uncertain` | ? |
| < 0.50, or nothing matched | `missing` | ✗ |

For an `all_of` group (chromosome **and** position), the confidence is the
**minimum** across the group.

### Verdict

| Verdict | Condition |
| --- | --- |
| `READY` | Every required field `satisfied` |
| `PARTIAL` | No required field `missing`, but at least one `uncertain`; **or** one to two required fields missing |
| `NOT_READY` | Three or more required fields missing, or all of them |
| `UNKNOWN` | No requirements could be evaluated |

Recommended fields never change the verdict. They change the *decision text*:
a `READY` file missing standard error and sample size is reported as ready with
an explicit note that Bayesian methods are ruled out.

`confidence` on the overall assessment is the minimum confidence across the
required fields — the weakest link, not an average that would hide it.

### Warnings

Raised alongside the verdict when a file is usable but a naive reading of it
would be wrong:

| Trigger | Warning |
| --- | --- |
| `odds_ratio` present, `beta` absent | Take the natural log before using as weights |
| `hazard_ratio` present, `beta` absent | Take the natural log before using as weights |
| `z_score` is the only effect measure | Conversion also needs EAF and per-variant N |
| `neg_log10_p_value` present, `p_value` absent | Significance is −log₁₀(p), not p |
| `minor_allele_frequency` present, `effect_allele_frequency` absent | Which allele is minor is population-dependent |
| `chromosome_position` without separate `chromosome`/`position` | Must be split before most tools can read it |
| Duplicate column names | Listed by name |
| Any heuristic mapping | The columns concerned are listed |

None of these transformations is ever applied automatically. Log-transforming
an odds ratio is a scientific decision that belongs to the user, and to the
downstream tool.

### Two routes, one verdict

| Route | `evidence_source` | Bytes of data read |
| --- | --- | --- |
| GWAS-SSF declaration | `gwas_ssf_metadata (GWAS-SSF v1.0)` | 0 |
| File probe | `file_probe` | ≤ `--probe-bytes` |
| Local file | `local_file` | ≤ `--probe-bytes` |

Both routes run the same evaluator over the same vocabulary, so their verdicts
are directly comparable. `assess --force-probe` runs both and reports any
disagreement — that comparison is the benchmark's central measurement.

### Worked examples

```text
CHR POS SNP A1 A2 BETA SE P N
  -> READY. All five required satisfied; SE and N present; EAF and INFO absent.

chromosome base_pair_location effect_allele other_allele beta
standard_error effect_allele_frequency p_value
  -> READY. The GWAS-SSF v1.0 mandatory set.

SNP A1 OR P
  -> PARTIAL. Other allele missing.
     Warning: effect sizes are odds ratios; take the natural log.

rsid beta
  -> NOT_READY. Effect allele, other allele and p-value all missing.
```

---

## Extending the vocabulary

1. Add the alias to the right concept in `aliases.yaml`.
2. Run `pytest tests/test_mapping.py`. A conflict with another concept raises at
   load time; a concatenation-shaped alias fails a test.
3. If it is a legitimate separator-free compound, add it to
   `LEGITIMATE_COMPOUND_ALIASES` in `tests/test_mapping.py` — deliberately a
   manual step, so someone confirms it is a real column name.
4. Update the table in this document if a new concept was introduced.
