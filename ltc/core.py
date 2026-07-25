"""Core dispatch — the single transport-agnostic entry point.

`compress_payload(content, content_type)` is the seam that every transport
(CLI now; MCP in Layer 2; a possible auto-proxy in Layer 3) calls without
change. It knows about content *types*, never about callers.

Adding a compressor is a two-line change to _REGISTRY below.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Optional, Tuple

from . import commands
from .result import CompressResult
from .tokens import estimate_tokens
from .compressors import json_compressor
from .compressors import log_compressor
from .compressors import listing_compressor
from .compressors import pytest_compressor
from .compressors import git_compressor

# name -> (compress_fn, decompress_fn)
_REGISTRY: Dict[str, Tuple[Callable[[str], CompressResult],
                           Callable[[str, Dict[str, Any]], str]]] = {
    json_compressor.NAME: (json_compressor.compress, json_compressor.decompress),
    log_compressor.NAME: (log_compressor.compress, log_compressor.decompress),
    log_compressor.GENERIC_NAME: (log_compressor.compress_generic,
                                  log_compressor.decompress),
    listing_compressor.NAME: (listing_compressor.compress, listing_compressor.decompress),
    pytest_compressor.NAME: (pytest_compressor.compress, pytest_compressor.decompress),
    git_compressor.NAME: (git_compressor.compress, git_compressor.decompress),
}

# Content types a caller may request. "auto" sniffs; others map to a compressor.
SUPPORTED_TYPES = ("auto", "json", "log", "listing", "generic", "pytest", "git")

# Compressors that want the originating command itself, not just the content
# type derived from it. Two do, for the same reason in different clothes: the
# output alone cannot tell you what the user asked for.
#   * listing — it drops noise directories, and whether that is compression or a
#     wrong answer depends on whether the user asked for those directories by
#     name.
#   * git — `git diff -U10` asks for wide context by name, and trimming it back
#     to one line answers a different question than the one asked. The bytes of
#     a wide-context diff are indistinguishable from a narrow one's.
# Everything else is decided by the bytes.
_COMMAND_AWARE = frozenset({listing_compressor.NAME, git_compressor.NAME})

# --- sniffing thresholds (single place to tune detection) -------------------
# Fraction of lines that must look path-shaped for "listing" to win.
_LISTING_MAJORITY = 0.7
# Below this many lines a listing isn't worth a header line.
_MIN_LISTING_LINES = 2
# Below this many non-blank lines there is nothing for the generic codec to do.
_MIN_GENERIC_LINES = 8
# `ls -l` always opens with this; it is not part of the listing proper.
_TOTAL_HEADER_RE = re.compile(r"^\s*total\s+\d+\s*$", re.IGNORECASE)
# Banners only a test runner prints. Deliberately few and specific: matching
# one routes content to a lossy codec.
# pytest pads its bars out to the terminal width, so the two-'=' form is always
# a substring of the real thing.
_PYTEST_MARKERS = (
    "test session starts",
    "short test summary info",
    "== FAILURES ==",
    "== ERRORS ==",
)
# unittest's trailer, which pytest never prints.
_UNITTEST_TAIL_RE = re.compile(r"(?m)^Ran \d+ tests? in ")
# `git log`'s per-commit header, anchored to a line start.
_COMMIT_HEADER_RE = re.compile(r"(?m)^commit [0-9a-f]{7,40}$")


def _sniff(content: str) -> str:
    """Best-effort content-type detection. Cheap and side-effect free.

    NOTE (Layer 3 design): when a future proxy runs this automatically on every
    payload, sniffing must stay this cheap and must fail *open* — an unknown
    type returns identity, never an error, so the proxy can never corrupt a
    request it didn't understand. For explicit user invocation (CLI/MCP) we
    could afford stricter detection, but keeping one lenient path avoids two
    behaviours to reason about.
    """
    stripped = content.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(content)
            return "json"
        except ValueError:
            pass
    # The command-shaped detectors run first: they are narrow enough to be
    # near-certain when they fire, and both would otherwise be swallowed by
    # the broader listing/log tests below.
    if _looks_like_pytest(content):
        return "pytest"
    if _looks_like_git(content):
        return "git"
    # Listing is checked before log because log is now the catch-all: a
    # directory listing would otherwise never reach its own codec.
    if _looks_like_listing(content):
        return "listing"
    if _looks_like_log(content):
        return "log"
    if _is_multiline(content):
        return "generic"
    return "identity"


def _is_multiline(content: str) -> bool:
    """True if there is enough structure for the generic codec to bite on.

    Below this there is nothing to collapse or cap, and wrapping the payload
    costs more than it saves.
    """
    return len([ln for ln in content.split("\n") if ln.strip()]) >= _MIN_GENERIC_LINES


def _looks_like_log(content: str) -> bool:
    """Cheap log detector: a traceback header, or a run of repeated lines.

    This selects the *aggressive* (digit-masking) log codec, so it stays
    conservative — content that merely happens to be multi-line falls through
    to the safer "generic" catch-all instead.
    """
    if "Traceback (most recent call last):" in content:
        return True
    lines = content.split("\n")
    # Any 3-in-a-row identical, non-blank lines strongly imply log noise.
    run = 1
    for prev, cur in zip(lines, lines[1:]):
        if cur == prev and cur.strip():
            run += 1
            if run >= 3:
                return True
        else:
            run = 1
    return False


def _looks_like_pytest(content: str) -> bool:
    """Test-run detector for when no command hint was supplied.

    Routes to a *lossy* codec, so it only fires on banners pytest and unittest
    print and nothing else does. A traceback alone is not enough — that is the
    log codec's job, and it keeps everything.
    """
    if any(marker in content for marker in _PYTEST_MARKERS):
        return True
    return bool(_UNITTEST_TAIL_RE.search(content))


def _looks_like_git(content: str) -> bool:
    """git-output detector for when no command hint was supplied.

    Restricted to the two shapes nothing else produces: a unified diff header
    and a `git log` commit header. `git status` is deliberately absent — its
    text is ordinary enough English to misfire on, and status nearly always
    arrives with a command hint attached anyway.
    """
    if content.startswith("diff --git ") or "\ndiff --git " in content:
        return True
    return bool(_COMMIT_HEADER_RE.search(content))


def _looks_like_listing(content: str) -> bool:
    """File-listing / search-result detector.

    Originally this required *every* non-blank line to contain a path
    separator, which almost nothing satisfies in practice — a single ``total
    48`` header from ``ls -la``, or one trailing summary line, disqualified
    the whole payload. A majority threshold keeps the codec's homogeneity
    requirement (it re-checks per line before factoring anyway) while letting
    real listings through.
    """
    # `ls -l` output has no path separators at all, so the path-majority test
    # below can never see it; ask the codec itself instead.
    if listing_compressor.looks_like_long_format(content):
        return True
    lines = [ln for ln in content.split("\n") if ln.strip()]
    # Drop a leading `total N` header, which `ls -l` always emits.
    if lines and _TOTAL_HEADER_RE.match(lines[0]):
        lines = lines[1:]
    if len(lines) < _MIN_LISTING_LINES:
        return False
    path_like = sum(1 for ln in lines if "/" in ln or "\\" in ln)
    return path_like >= len(lines) * _LISTING_MAJORITY


def _resolve_auto(content: str, command: Optional[str]) -> str:
    """Pick a compressor name for "auto", preferring the command hint.

    What produced the output is far more reliable than what the output looks
    like: an all-passing pytest run resembles no traceback at all, yet still
    wants the pytest codec. Content sniffing is the fallback, not the primary.
    """
    hint = commands.classify(command)
    if hint in _REGISTRY:
        return hint
    return _sniff(content)


def compress_payload(content: str, content_type: str = "auto",
                     command: Optional[str] = None) -> CompressResult:
    """Compress `content`. THE entry point. Pure; no I/O, no globals mutated.

    Args:
        content:      Raw text to compress.
        content_type: One of SUPPORTED_TYPES. "auto" sniffs the content.
        command:      The shell command that produced `content`, if known.
                      Used only when content_type is "auto"; an explicit type
                      always wins over the hint.

    Returns:
        A CompressResult. Unknown or unhandled content yields an identity
        result (lossless pass-through) rather than raising, so callers — most
        importantly a future automatic proxy — are never forced to choose
        between crashing and dropping data.
    """
    if content_type not in SUPPORTED_TYPES:
        raise ValueError(
            f"unsupported content_type {content_type!r}; "
            f"expected one of {SUPPORTED_TYPES}"
        )

    resolved = (_resolve_auto(content, command)
                if content_type == "auto" else content_type)

    entry = _REGISTRY.get(resolved)
    if entry is None:
        n = estimate_tokens(content)
        return CompressResult(
            compressed=content,
            metadata={"compressor": "identity", "version": 1,
                      "reason": f"no compressor for {resolved!r}"},
            original_tokens=n,
            compressed_tokens=n,
            compressor="identity",
            reversible=True,
        )
    compress_fn, _ = entry
    if resolved in _COMMAND_AWARE:
        # Passed even when content_type was explicit: an explicit type says
        # *which* codec, never *what the user asked for*, and the second is
        # what gates discarding.
        return compress_fn(content, command=command)
    return compress_fn(content)


def decompress(compressed: str, metadata: Dict[str, Any]) -> str:
    """Reverse a previous compression using its metadata. Pure.

    Dispatches on ``metadata["compressor"]`` so it is fully self-contained: a
    caller only needs the compressed text and the metadata the result carried.
    """
    name = metadata.get("compressor")
    if name == "identity":
        return compressed
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"no decompressor registered for {name!r}")
    _, decompress_fn = entry
    return decompress_fn(compressed, metadata)
