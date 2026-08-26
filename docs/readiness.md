# PRS Readiness Rules

The complete rules — including the worked examples and the reasoning behind
each threshold — live in [Column Mapping](mapping.md#part-2-prs-readiness-rules).
This page is the summary.

---

## The five required fields

A polygenic risk score needs exactly five things. Nothing else is *required*.

| Requirement | Satisfied by |
| --- | --- |
| **Variant identification** | `variant_id`, **or** `chromosome_position`, **or** (`chromosome` **and** `position`) |
| **Effect allele** | `effect_allele` |
| **Other allele** | `other_allele` |
| **Effect size** | `beta`, **or** `odds_ratio`, **or** `hazard_ratio`, **or** `z_score` *with* `sample_size` *and* a frequency |
| **Significance** | `p_value` **or** `neg_log10_p_value` |

!!! question "Why these five and no more"

    A PRS is a weighted sum of allele dosages. You need to know *which variant*
    (identification), *which allele carries the weight* (effect allele), *what
    it is being compared against* (other allele), *how large the weight is*
    (effect size), and *whether to include the variant at all* (significance).
    Everything else — standard error, frequency, imputation quality — changes
    *which method* you can use, not whether a PRS is possible at all.

---

## Recommended fields

Their absence rules out particular methods, not PRS in general.

| Field | What it unlocks |
| --- | --- |
| `standard_error` | LDpred2, PRS-CS and other Bayesian methods |
| `sample_size` | Method calibration; z-score conversion |
| Allele frequency | Frequency filtering; z-score to beta conversion |
| `info_score` | Excluding poorly imputed variants |

---

## Two rules worth stating explicitly

### A z-score is not an effect size

A z-score is a *test statistic*, not a per-allele weight. Converting one to a
beta requires the sample size **and** an allele frequency:

$$\hat\beta \approx \frac{z}{\sqrt{2f(1-f)(N + z^2)}}$$

So `z_score` satisfies the effect-size requirement **only when `sample_size`
and a frequency concept are also present**. Alone, it is reported as
`CONDITIONAL` with the missing pieces named — never as `READY`.

### A single arm is not a sample size

`cases` alone is not `N`. Neither is `controls`. GWASPoker keeps `sample_size`,
`cases` and `controls` as three distinct concepts, and reports their sum as
**derived** when both arms are present — so a downstream method knows the total
was computed rather than published.

!!! warning "v1 conflated all three"

    v1's `N_list` mixed sample size, case count and control count into one
    alias set, so a case-only column could silently become the total. That is
    a systematic under-count in every method that scales by N.

---

## From confidence to status

A requirement's status comes from the **confidence of the mapping that satisfies
it**, so a curated alias is visibly stronger evidence than a suffix heuristic.

| Confidence | Status | Symbol |
| --- | --- | --- |
| ≥ 0.90 | `satisfied` | ✓ |
| 0.50 – 0.89 | `uncertain` | ? |
| < 0.50, or nothing matched | `missing` | ✗ |

For an `all_of` group — chromosome **and** position — the confidence is the
**minimum** across the group. A requirement is only as strong as its weakest
part.

Value validation then scales that confidence: `FAIL` multiplies it by 0.40,
`WARN` by 0.85. See [Value Validation](validation.md).

---

## The three verdicts

<div class="grid cards" markdown>

- ### <span class="verdict-ready">READY</span>

    Every required field is `satisfied`, and no value check contradicts one.

- ### <span class="verdict-conditional">CONDITIONAL</span>

    Every required field is satisfiable, but at least one needs a stated
    transformation or an additional column first. The report names each
    condition and what would resolve it.

- ### <span class="verdict-not-ready">NOT_READY</span>

    A required field is `missing`, or a value check contradicts one badly
    enough to drop it below threshold.

</div>

### Common conditional cases

| Situation | Condition reported |
| --- | --- |
| Only `odds_ratio` present | Log-transform to obtain a per-allele weight |
| Only `z_score`, with `sample_size` and a frequency | Convert to beta using the formula above |
| `neg_log10_p_value` instead of `p_value` | Back-transform, or use a method that accepts the log scale |
| Effect size present but no `standard_error` | Bayesian methods unavailable; clumping and thresholding still works |

---

## Warnings

Warnings never change a verdict; they explain it. Typical ones:

- an allele frequency that is really a **minor** allele frequency, so the
  effect-allele frequency must be derived rather than read;
- a mapping that rests on an [ambiguous alias](mapping.md#ambiguous-alias-audit)
  (`ID`, `ALT`, `REF`) and therefore on the values rather than the name;
- a sample size that came from free text or from the optional language model
  rather than from the GWAS Catalog's structured field.

---

## Next

- [Column Mapping](mapping.md) — the full vocabulary and worked examples
- [Value Validation](validation.md) — how values confirm or challenge a mapping
