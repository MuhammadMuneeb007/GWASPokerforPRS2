"""Optional ELECTRA question-answering fallback for sample counts.

Only reached when the structured API and the deterministic patterns have both
failed, and only when the user asked for it with ``--llm``.

Three properties matter, all of which the original implementation lacked:

* **Lazy.** ``transformers`` and ``torch`` are imported inside the function.
  Installing GWASPoker does not pull in PyTorch, and ``search``, ``probe``,
  ``assess`` and ``scan`` all work without it.
* **Cached.** One pipeline per process, held by :func:`_get_pipeline`. v1 called
  ``pipeline('question-answering', model=...)`` on *every* extraction, three
  times per study through ``DataFrame.apply``.
* **Never authoritative.** Answers are recorded with ``source="llm"`` and the
  model's own score, and they never overwrite a structured or regex value.

Install with ``pip install "gwaspoker[llm]"``. The model is downloaded to the
standard Hugging Face cache on first use, not at install time.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Any, Optional

from gwaspoker.failures import FAILURES, FailureCategory

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ahotrod/electra_large_discriminator_squad2_512"

QUESTIONS = {
    "cases": "What is the number of cases?",
    "controls": "What is the number of controls?",
    "total": "What is the total sample size?",
}

#: Answers scoring below this are discarded. Extractive QA models happily return
#: a low-confidence span for a question the context does not answer.
MIN_ANSWER_SCORE = 0.20


@dataclass
class QaCounts:
    """Counts proposed by the QA model, each with the model's own score."""

    total: Optional[int] = None
    cases: Optional[int] = None
    controls: Optional[int] = None
    total_confidence: float = 0.0
    cases_confidence: float = 0.0
    controls_confidence: float = 0.0


def llm_available() -> bool:
    """True if the optional ``[llm]`` extra is installed. Does not load a model."""
    import importlib.util

    return importlib.util.find_spec("transformers") is not None


@functools.lru_cache(maxsize=2)
def _get_pipeline(model_name: str, device: str) -> Optional[Any]:
    """Build the QA pipeline once per (model, device) per process.

    ``lru_cache`` is what fixes v1's repeated model construction. The first call
    may take minutes while the model downloads; subsequent calls are free.
    """
    try:
        from transformers import pipeline
    except ImportError:
        FAILURES.record(
            "llm",
            FailureCategory.DEPENDENCY_MISSING,
            'The LLM fallback needs the optional extra: pip install "gwaspoker[llm]"',
        )
        return None

    logger.info("Loading question-answering model %s (first use may download it)", model_name)
    kwargs: dict[str, Any] = {"model": model_name, "tokenizer": model_name}
    if device and device != "auto":
        kwargs["device"] = device
    try:
        return pipeline("question-answering", **kwargs)
    except (OSError, ValueError, RuntimeError) as exc:
        FAILURES.record(
            "llm",
            FailureCategory.LLM_ERROR,
            f"Could not load {model_name}: {exc}",
            exception=exc,
        )
        return None


def ask(question: str, context: str, *, model_name: Optional[str] = None, device: str = "auto"):
    """Ask one question of the context. Returns ``(answer, score)`` or ``None``."""
    qa = _get_pipeline(model_name or DEFAULT_MODEL, device)
    if qa is None:
        return None
    try:
        result = qa(question=question, context=context)
    except (RuntimeError, ValueError, IndexError) as exc:
        FAILURES.record(
            "llm",
            FailureCategory.LLM_ERROR,
            f"Question answering failed: {exc}",
            exception=exc,
        )
        return None
    if not isinstance(result, dict):
        return None
    return str(result.get("answer", "")), float(result.get("score", 0.0))


def extract_counts_with_qa(
    context: str,
    *,
    model_name: Optional[str] = None,
    device: str = "auto",
    min_score: float = MIN_ANSWER_SCORE,
) -> Optional[QaCounts]:
    """Ask the model for cases, controls and total from one description.

    Returns ``None`` if the model is unavailable, so the caller can distinguish
    "not asked" from "asked and got nothing".
    """
    from gwaspoker.metadata.samples import parse_count

    if not context.strip():
        return None
    if _get_pipeline(model_name or DEFAULT_MODEL, device) is None:
        return None

    counts = QaCounts()
    for field_name, question in QUESTIONS.items():
        answer = ask(question, context, model_name=model_name, device=device)
        if answer is None:
            continue
        text, score = answer
        if score < min_score:
            logger.debug(
                "Discarding %s answer %r (score %.3f < %.2f)", field_name, text, score, min_score
            )
            continue
        value = parse_count(text)
        if value is None or value <= 0:
            continue
        setattr(counts, field_name, value)
        setattr(counts, f"{field_name}_confidence", round(score, 4))

    # A model that answers "12,000" to all three questions has not understood
    # the context; treat that as no answer rather than as three answers.
    values = [v for v in (counts.total, counts.cases, counts.controls) if v is not None]
    if len(values) >= 2 and len(set(values)) == 1:
        logger.debug("Discarding QA output: identical answers to distinct questions")
        return QaCounts()

    return counts
