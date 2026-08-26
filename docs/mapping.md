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
| 2b | A curated alias whose *name* is weak evidence (`ID`, `ALT`, `REF`) | 0.75 | `ambiguous_alias` |
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

Suffix rules match at a **word boundary**: the leading underscore is part of
the pattern and survives normalization. Without that, `_or` matches the tail of
any word ending in those two letters — which is how the CSS fragment
`span{background-color:` in an HTML response was mapped to an odds ratio. The
same slip made `FreqSE` a standard error. Boundary-anchored, `trait_OR` still
resolves and `background_color` does not.

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
| `minorallelefrequency` under `effect_allele_list` | `minor_allele_frequency` (see the audit below) |
| `weight` under `N_list` | dropped (meta-analysis weight) |
| `effect_all` in both `beta_list` and `effect_allele_list` | dropped from both — genuinely ambiguous |
| five aliases corrupted by missing commas | absent; a test prevents recurrence |

### Ambiguous-alias audit

Some aliases are not *wrong* so much as *not evidence*. They stay in the
vocabulary — removing them would make common files unreadable — but they map at
reduced confidence (`mapping_method: ambiguous_alias`, confidence 0.75) and
carry a note. Value validation then confirms or challenges them.

| Alias | Was | Now | Why |
| --- | --- | --- | --- |
| `minorallelefrequency` | `effect_allele_frequency` | `minor_allele_frequency` | **A minor allele frequency is not an effect allele frequency.** Which allele is minor is population-dependent; which carries the effect is a property of the analysis. Conflating them silently substitutes one for the other wherever EAF is needed. LDSC's alias table maps both `MAF` and `EAF` onto a single `FRQ` column — exactly the conflation being avoided here. |
| `ALT` | `effect_allele` (0.95) | `alternate_allele` (0.75) | ALT is a **VCF coordinate convention** — the non-reference allele at the site. Many GWAS files do use it as the effect allele, but that is a per-source convention, not a rule. |
| `REF` | `other_allele` (0.95) | `reference_allele` (0.75) | Likewise. Note that LDSC maps `REFERENCE_ALLELE` to A1, the *effect* allele, while VCF semantics put REF opposite ALT. Sources genuinely disagree, which is why GWASPoker refuses to resolve it from the header. |
| `ID`, `NAME`, `MARKER`, `VAR_NAME` | `variant_id` (0.95) | `variant_id` (0.75) | Generic column names. They usually do hold an identifier, but `ID` is equally at home holding a row number. The values decide. |

### Aliases added from external files

An external run over 768 heterogeneous URLs surfaced column names that are
common outside the GWAS Catalog. Each was added only after confirming its
meaning from the tool that emits it, not from its resemblance to a known name.

| Alias | Concept | Source |
| --- | --- | --- |
| `P.2gc`, `SE.2gc` | `p_value`, `standard_error` | double genomic-control correction, standard in GIANT/METAL consortium releases |
| `GWAS_P` | `p_value` | multi-analysis files distinguishing the GWAS column from others |
| `n_total_sum` | `sample_size` | METAL's summed N across contributing cohorts |
| `FreqAllele1HapMapCEU`, `eaf_hapmapceu` | `effect_allele_frequency` | frequency of allele 1 in the HapMap CEU reference — allele 1 is the effect allele in these files |
| `mach_r2`, `mach_rsq` | `info_score` | MaCH/minimac imputation quality |

### Deliberately left unmapped

Not mapping a column is a result, not a gap. These recur in external files and
stay `unknown` on purpose:

| Column | Why not |
| --- | --- |
| `FreqSE`, `MinFreq`, `MaxFreq` | METAL's *dispersion* of the frequency across cohorts, not a frequency. `FreqSE` is not a standard error of an effect. |
| `Overall`, `Direction` | Per-cohort direction strings (`+-+?`), not an effect size. |
| `P_BMD`, `P_LM`, `beta_BMD`, `beta_LM` | The file holds more than one analysis. A blanket `P_*` rule would map both to `p_value`, and taking the highest-confidence one would silently pick a phenotype. Until there is an explicit policy for choosing the primary analysis, GWASPoker reports the ambiguity instead of resolving it. |
| `SNP_hg18`, `SNP_hg19` | Build-specific position strings; which build is wanted is the caller's decision. |

`A1` and `A2` keep full 0.95 confidence: A1-as-effect-allele is near-universal
in PRS-facing tooling (PLINK, METAL, LDSC and PRSice all read it that way), so
the header genuinely is strong evidence there.

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

### The z-score is not an effect size

A z-score is a test statistic, `beta / se`. Recovering a weight from it needs,
at minimum, the per-variant sample size and an allele frequency:

```text
se   ~= 1 / sqrt(2 * N * f * (1 - f))
beta ~= Z * se
```

Without both companions the column cannot yield PRS weights at all, so
`z_score` alone does **not** satisfy the effect-size requirement and the verdict
is `PARTIAL`, not `READY`. Beta is usable directly; odds and hazard ratios are
usable after a deterministic log transform that needs nothing else, so those
three satisfy it on their own.

### A single arm is not a sample size

`cases` alone does not establish total N, and neither does `controls`. Either N
is stated directly, or both arms are — in which case the total is derivable as
their sum, and the requirement note records that it was **derived rather than
observed**.

### Required fields

| Key | Satisfied by | Rule |
| --- | --- | --- |
| `variant_identification` | `variant_id` **or** `chromosome_position` **or** (`chromosome` **and** `position`) | Either identifier form suffices |
| `effect_allele` | `effect_allele` | |
| `other_allele` | `other_allele` | |
| `effect_size` | `beta` **or** `odds_ratio` **or** `hazard_ratio`; **or** `z_score` *together with* `sample_size` *and* an allele-frequency concept | See below |
| `significance` | `p_value` **or** `neg_log10_p_value` | Either scale |

### Recommended fields

Their absence rules out particular methods, not PRS in general.

| Key | Satisfied by | Why it matters |
| --- | --- | --- |
| `standard_error` | `standard_error` | Required by LDpred2, PRS-CS and other Bayesian methods |
| `sample_size` | `sample_size` **or** (`cases` **and** `controls`) | A single arm is not a total; the sum is reported as *derived* |
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

## Part 3: Value-domain validation

Header names **propose** semantic concepts. Sampled values **test** whether
those proposals are plausible. The two are separate lines of evidence and stay
separate all the way through the output.

The motivating case:

```text
Header:  CHR
Values:  1:123456
         1:892331
         2:773291
```

The name says chromosome. The values are chromosome-position strings. A tool
that maps on the header alone hands a PRS pipeline a column it cannot use.

Implemented in `src/gwaspoker/validation/values.py`, run automatically by the
probe on rows it has already decoded.

### What it is not

GWASPoker is pre-download triage. Value validation performs **structural sanity
checks only**. It does not:

* filter variants on INFO, MAF, p-value or anything else;
* harmonise alleles against a reference genome, or lift over coordinates;
* normalise, deduplicate, or score;
* perform full GWAS QC.

**It never silently transforms values.** Where a transformation would be needed
it is named in `requires_transformation` and left undone:

| Situation | Reported | Applied |
| --- | --- | --- |
| Odds ratio present | "natural log before use as a PRS weight" | no |
| −log₁₀(p) present | "10**-x to recover the p-value scale" | no |
| Combined `chr:pos` | "split into separate chromosome and position columns" | no |
| `CHR` holding `1:12345` | `suggested_concept: chromosome_position` | **column is not remapped** |

GWASLab, MungeSumstats and the rest remain downstream, and are never consulted
to make a GWASPoker prediction — that would make any later comparison against
them circular.

### Sampling

Rows come from the probe prefix that is **already in memory**. Validation never
triggers another request. `config.sample_rows` (default 50) controls how many
the header detector retains; `config.validation_rows` (default 50) how many are
tested. Both `available_rows` and `rows_checked` appear in the output.

If the probe recovered no data rows, the result is `NOT_TESTED` with a reason —
never a fabricated pass or fail.

### Value-domain rules

| Concept | Accepted | Rejected |
| --- | --- | --- |
| `chromosome` | `1`–`25`, `X`, `Y`, `M`/`MT`, optional `chr` prefix | `1:12345`, `1_12345_A_G` |
| `position` | positive integer | `0`, negatives, `chr1:12345`, `rs123` |
| `chromosome_position` | `1:12345`, `chr1:12345`, `2_88112` | — (flags `requires_split`) |
| `variant_id` | `rs12345`, `1:12345:A:G`, `1_12345_A_G` | plain integers are weak evidence |
| `effect_allele` / `other_allele` | `A`/`C`/`G`/`T`, indel strings, `I`/`D` | non-nucleotides |
| `beta` | finite | `inf`, `nan` |
| `odds_ratio`, `hazard_ratio` | finite, `> 0` | `0`, negatives |
| `z_score` | finite | `inf`, `nan` |
| `standard_error` | finite, `>= 0` | negatives |
| `p_value` | `0 <= p <= 1` | `< 0`, `> 1` |
| `neg_log10_p_value` | finite, `>= 0` | negatives |
| `effect_allele_frequency`, `allele_frequency` | `0 <= f <= 1` | outside |
| `minor_allele_frequency` | `0 <= f <= 0.5` (±0.001) | `0.8` |
| `sample_size` | positive | `0`, negatives |
| `cases`, `controls` | `>= 0` | negatives |
| `info_score` | `0 <= INFO <= 2` | outside |

Two rules deserve their reasoning stated.

**`p_value == 0` warns, it does not fail.** Exactly zero occurs through
floating-point underflow at very small p-values. Calling the column invalid
because of it would be wrong, so zeros are counted and reported separately.
Values `> 1` *are* a genuine domain violation, and when they dominate the
column, `neg_log10_p_value` is suggested — but the column is not remapped on
that evidence alone.

**`info_score` uses a broad `[0, 2]` window.** This is a structural check
asking "is this column plausibly an INFO score?", not a QC threshold. LDSC's own
`filter_info` treats values outside `[0, 2]` as evidence of a mislabelled
column, which is the same question. How many fall in the usual `0–1` range is
reported as a note. **No INFO filter is applied** — that is QC, and it belongs
downstream.

### Cross-column checks

* **Allele identity** — rows where the effect and other allele are the same
  string are counted and reported. Alleles are never reoriented.
* **`cases + controls` vs `N`** — compared with a 1% tolerance, because
  per-variant N legitimately varies with missingness. Disagreement is reported;
  no value is adjusted.

### Statuses

| Status | Meaning |
| --- | --- |
| `PASS` | ≥ 95% of non-missing sampled values are in domain |
| `WARN` | 80–95%, or a domain-specific caveat such as `p == 0` |
| `FAIL` | < 80% — the values contradict the header's claim |
| `NOT_TESTED` | no rule for the concept, or nothing non-missing to test |

`NOT_TESTED` is never a negative finding. A concept with no rule is not assumed
valid; it is simply not judged, and the output says so.

### How it feeds readiness

Header mapping proposes; values corroborate or contradict. The effective
confidence for a requirement is the header confidence scaled by the value
evidence:

| Value status | Factor | Effect |
| --- | --- | --- |
| `PASS` | ×1.0 | satisfied |
| `WARN` | ×0.85 | usually still satisfied, warning surfaced |
| `FAIL` | ×0.4 | drops below the threshold — **not** confidently satisfied |
| `NOT_TESTED` | ×1.0 | header evidence stands alone |

The weakest contributing column governs. Both numbers survive into the output:

```json
{
  "key": "variant_identification",
  "status": "uncertain",
  "header_confidence": 0.95,
  "confidence": 0.38,
  "value_status": "FAIL",
  "note": "CHR: only 0% of sampled values are 1-25, X, Y, MT/M ..."
}
```

`header_confidence` records what the name alone supported; `confidence` records
the effective figure after the data had its say. Collapsing them would lose the
distinction, and the manuscript measures four things separately:

**A.** header detection accuracy · **B.** header semantic mapping accuracy ·
**C.** sampled value-domain consistency · **D.** PRS-readiness decision

An `unknown` mapping stays `unknown`. Value validation never forces a mapping
into existence.

---

## Extending the vocabulary

1. Add the alias to the right concept in `aliases.yaml`.
2. Run `pytest tests/test_mapping.py`. A conflict with another concept raises at
   load time; a concatenation-shaped alias fails a test.
3. If it is a legitimate separator-free compound, add it to
   `LEGITIMATE_COMPOUND_ALIASES` in `tests/test_mapping.py` — deliberately a
   manual step, so someone confirms it is a real column name.
4. Update the table in this document if a new concept was introduced.
