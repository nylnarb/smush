"""Heuristic token estimation — no model, no network, no downloads.

We deliberately do NOT ship or load a tokenizer: that would violate the
"no model downloads / no native build" constraints and add weight for a number
that only needs to be approximately right. What callers actually need is a
*consistent, monotonic* estimate so that before/after ratios are meaningful.

The heuristic splits text into runs — word, whitespace, underscore, punctuation
— and charges each run a cost that mirrors what BPE vocabularies actually do.
The constants below were fitted against two independent real tokenizers (GPT-2
and DeepSeek) over pytest, git, listing, traceback, JSON, grep and prose
fixtures; ``tests/test_tokens_calibration.py`` pins those fixtures, their
reference counts and the error band. Five behaviours matter, each earning a
rule:

* **Runs of one repeated punctuation character are nearly free.** ``"=" * 40``
  is two real tokens, not forty. Charging per character made pytest banner bars
  overstate test output by ~20%.
* **Runs of *different* punctuation merge about two-to-one** (``":``, ``},``,
  ``=>``), which is most of what JSON and diffs are made of.
* **Alphanumeric runs mixing letters and digits cost one token per character.**
  Commit hashes and hex ids genuinely tokenize that badly; a per-4-chars rule
  undercounted a 40-char sha fourfold.
* **A single space is free, but longer whitespace runs are not.** Column
  padding and indentation are real tokens in every vocabulary that does not
  merge them away, and ignoring them undercounted ``ls -l`` output. One space
  between words stays free, which keeps prose accurate.
* **Everything else splits into ~5-character sub-pieces**, the classic
  approximation, with digit runs packing denser.

Absolute counts remain estimates; the calibration test states the band.
"""

from __future__ import annotations

import math
import re

# One run of word characters (no underscores), whitespace, underscores, or
# punctuation. Underscores are split off deliberately: BPE breaks snake_case
# identifiers at them, so `test_widget_renders` is many tokens, not one.
_RUN_RE = re.compile(r"[^\W_]+|\s+|_+|[^\w\s]+", re.UNICODE)

# Real tokenizers split long words into sub-word pieces of ~5 chars on average.
_CHARS_PER_SUBWORD = 5
# Digit runs pack denser: "2026" and "197609" are one or two tokens each.
_CHARS_PER_DIGIT_PIECE = 2
# A run of the *same* punctuation character (`====`, `----`) collapses hard.
_CHARS_PER_REPEATED_PUNCT = 8
# A run of *different* punctuation (`":`, `},`, `=>`) merges about two-to-one.
_CHARS_PER_MIXED_PUNCT = 2
# Whitespace runs of at least this length are charged; a single space is free.
_MIN_CHARGED_WHITESPACE = 2
_CHARS_PER_WHITESPACE_PIECE = 4


def _run_cost(run: str) -> int:
    """Estimated tokens for one homogeneous run. See module docstring."""
    first = run[0]
    n = len(run)
    if first.isspace():
        if n < _MIN_CHARGED_WHITESPACE:
            return 0
        return max(1, n // _CHARS_PER_WHITESPACE_PIECE)
    if first == "_":
        return n
    if first.isalnum():
        has_digit = any(ch.isdigit() for ch in run)
        has_alpha = any(ch.isalpha() for ch in run)
        if has_digit and has_alpha:
            # Hashes, hex ids, version-ish blobs: roughly one token per char.
            return n
        per_piece = _CHARS_PER_DIGIT_PIECE if has_digit else _CHARS_PER_SUBWORD
        return max(1, math.ceil(n / per_piece))
    if n > 1 and len(set(run)) == 1:
        return max(1, n // _CHARS_PER_REPEATED_PUNCT)
    return max(1, math.ceil(n / _CHARS_PER_MIXED_PUNCT))


def estimate_tokens(text: str) -> int:
    """Return an estimated token count for `text`. Deterministic and pure."""
    if not text:
        return 0
    return sum(_run_cost(run) for run in _RUN_RE.findall(text))
