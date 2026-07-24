"""Log / command-output compressor.

Logs are the noisiest thing an LLM ever has to read: the same line repeated a
hundred times, deep tracebacks through library code the user can't change, and
tens of thousands of lines when only the head and tail matter. This compressor
attacks all three, and every transform is exactly reversible.

Strategy
--------
The text is split into physical lines and pushed through a small pipeline of
line-list transforms. Each transform replaces a block of lines with a single,
human-readable *marker line* and records the exact bytes it removed in the ops
list stored in ``metadata``. The three transforms, in application order:

1. Stack-trace trimming. Consecutive traceback frames whose file path looks like
   library / third-party code are folded into one marker; application frames are
   kept verbatim. The app-vs-library rule is a single explicit predicate,
   ``_is_library_frame`` (tunable via ``_LIBRARY_PATH_MARKERS``).

2. Run collapse. A run of >= ``_MIN_RUN`` *near-identical* lines becomes one
   representative line plus a repeat count. "Near-identical" is defined by
   ``_run_normalize`` (digits masked, so lines differing only by a counter or a
   timestamp collapse together) — also a single, tunable function.

3. Head / tail capping. If the (already-reduced) output still exceeds
   ``_MAX_LINES``, the middle is dropped and replaced by a
   " ... N lines dropped ... " marker.

Reversibility
-------------
As with the JSON compressor we pick a sentinel string S that does not occur in
the input, so every marker (S + tag + index) is guaranteed not to collide with
real log text. Each op stores the exact original lines it removed, keyed by the
index embedded in its marker. ``decompress`` reverses the ops in reverse order,
splicing the stored lines back in wherever it finds a marker. This reproduces
the original text *exactly*, byte for byte — logs are plain text, so we hold the
codec to exact round-trip (unlike the JSON codec, which only promises structural
equality).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..result import CompressResult
from ..tokens import estimate_tokens

NAME = "log"
# Same codec, conservative settings: used as the catch-all for arbitrary
# command output, where digit-masked run collapsing is unsafe (a numeric
# table would collapse to a single row). See `compress_generic`.
GENERIC_NAME = "generic"
VERSION = 1

# --- tuning knobs -----------------------------------------------------------
# A run of this many near-identical lines (or more) is collapsed to one line.
_MIN_RUN = 3
# Head/tail cap: only kicks in past _MAX_LINES; keep this many from each end.
_MAX_LINES = 120
_HEAD_LINES = 40
_TAIL_LINES = 40

# Substrings that mark a traceback frame's file path as library / third-party
# code (lowercased comparison). Explicit and tunable: add your virtualenv layout
# or drop an entry to keep those frames. Anything NOT matching is an app frame.
_LIBRARY_PATH_MARKERS: Tuple[str, ...] = (
    "site-packages",
    "dist-packages",
    "/lib/python",
    "\\lib\\python",
    "/python3",
    "\\python3",
    "lib/python3",
    "<frozen ",
    "/usr/lib/",
    "/usr/local/lib/",
)

# A Python traceback frame header: '  File "path", line N, in func'.
_FILE_RE = re.compile(r'^(?P<indent>[ \t]*)File "(?P<path>[^"]*)", line \d+, in ')

# Mask decimal runs so lines differing only by numbers/timestamps are "near".
_DIGIT_RE = re.compile(r"\d+")


# --- sentinel selection -----------------------------------------------------
def _pick_sentinel(text: str) -> str:
    """Return a short string that is NOT a substring of `text` (see json codec)."""
    base = "~~"
    if base not in text:
        return base
    i = 0
    while True:
        cand = base + str(i)
        if cand not in text:
            return cand
        i += 1


# --- exact line splitting ---------------------------------------------------
def _split_keep(text: str) -> List[str]:
    """Split on '\\n' only, keeping the terminators, so ``"".join`` is exact.

    We deliberately avoid ``str.splitlines`` — it also breaks on \\v, \\f, \\x1c
    and other Unicode boundaries, which would make markers unstable. Every line
    element except possibly the last ends in '\\n'.
    """
    if text == "":
        return []
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1] != "":
        lines.append(parts[-1])
    return lines


# --- heuristics -------------------------------------------------------------
def _is_library_frame(path: str) -> bool:
    """True if a traceback frame's file path is library / third-party code.

    The one explicit app-vs-library rule. Application frames (relative paths,
    the user's project tree) return False and are always kept.
    """
    low = path.lower()
    return any(marker in low for marker in _LIBRARY_PATH_MARKERS)


def _run_normalize(line: str) -> str:
    """Canonical form used to decide if two lines are 'near-identical'.

    Digits are masked so that e.g. 'retry 1 of 5' and 'retry 2 of 5' — or two
    log lines differing only in their timestamp — collapse into one run.
    """
    return _DIGIT_RE.sub("0", line)


def _frame_span(lines: List[str], i: int) -> Optional[Tuple[int, str]]:
    """If a traceback frame starts at `lines[i]`, return (end_exclusive, path).

    A frame is a ``File "...", line N, in f`` header plus, if present, the single
    indented source line that follows it. Returns None if `lines[i]` is not a
    frame header.
    """
    m = _FILE_RE.match(lines[i])
    if not m:
        return None
    end = i + 1
    # Consume one following indented source line (not itself a frame header).
    if end < len(lines):
        nxt = lines[end]
        if nxt[:1] in (" ", "\t") and not _FILE_RE.match(nxt):
            end += 1
    return end, m.group("path")


# --- transforms (each: lines -> (new_lines, op|None)) -----------------------
def _trim_stack(lines: List[str], sentinel: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Fold maximal runs of library traceback frames into single markers."""
    out: List[str] = []
    restore: Dict[str, List[str]] = {}
    idx = 0
    i = 0
    n = len(lines)
    while i < n:
        span = _frame_span(lines, i)
        if span is None or not _is_library_frame(span[1]):
            out.append(lines[i])
            i += 1
            continue
        # Gather a maximal run of consecutive library frames.
        block: List[str] = []
        frames = 0
        indent = _FILE_RE.match(lines[i]).group("indent")  # type: ignore[union-attr]
        while i < n:
            span = _frame_span(lines, i)
            if span is None or not _is_library_frame(span[1]):
                break
            block.extend(lines[i:span[0]])
            frames += 1
            i = span[0]
        key = str(idx)
        restore[key] = block
        out.append(
            f"{indent}{sentinel}stack{idx} … {frames} library frame(s) hidden …\n"
        )
        idx += 1
    op = {"tag": "stack", "restore": restore} if restore else None
    return out, op


def _collapse_runs(lines: List[str], sentinel: str, normalize=None) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Collapse runs of >= _MIN_RUN near-identical lines to one line + a count.

    `normalize` decides what "near-identical" means. It defaults to the
    digit-masking `_run_normalize`, which is right for logs but wrong for
    arbitrary output; callers compressing unknown content pass `str` to
    require exact matches instead.
    """
    if normalize is None:
        normalize = _run_normalize
    out: List[str] = []
    restore: Dict[str, List[str]] = {}
    idx = 0
    i = 0
    n = len(lines)
    while i < n:
        norm = normalize(lines[i])
        j = i
        while j + 1 < n and normalize(lines[j + 1]) == norm:
            j += 1
        run_len = j - i + 1
        if run_len >= _MIN_RUN:
            key = str(idx)
            restore[key] = lines[i:j + 1]
            rep = lines[i].rstrip("\n")
            out.append(f"{rep} {sentinel}run{idx} ×{run_len}\n")
            idx += 1
        else:
            out.extend(lines[i:j + 1])
        i = j + 1
    op = {"tag": "run", "restore": restore} if restore else None
    return out, op


def _cap_middle(lines: List[str], sentinel: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """If still over _MAX_LINES, keep head+tail and drop the middle."""
    if len(lines) <= _MAX_LINES:
        return lines, None
    mid = lines[_HEAD_LINES:len(lines) - _TAIL_LINES]
    if not mid:
        return lines, None
    head = lines[:_HEAD_LINES]
    tail = lines[len(lines) - _TAIL_LINES:]
    marker = f"{sentinel}drop0 … {len(mid)} lines dropped …\n"
    op = {"tag": "drop", "restore": {"0": mid}}
    return head + [marker] + tail, op


# --- public API -------------------------------------------------------------
def _identity_result(text: str, reason: str) -> CompressResult:
    n = estimate_tokens(text)
    return CompressResult(
        compressed=text,
        metadata={"compressor": "identity", "version": VERSION, "reason": reason},
        original_tokens=n,
        compressed_tokens=n,
        compressor="identity",
        reversible=True,
    )


def _compress(text: str, mask_digits: bool, name: str) -> CompressResult:
    """Shared implementation behind `compress` and `compress_generic`."""
    if text == "":
        return _identity_result(text, "empty input")

    sentinel = _pick_sentinel(text)
    lines = _split_keep(text)
    ops: List[Dict[str, Any]] = []
    normalize = _run_normalize if mask_digits else str

    def collapse(ls, s):
        return _collapse_runs(ls, s, normalize)

    # Order matters: trimming and collapsing shrink the list before the cap
    # decides whether a middle drop is still needed. decompress reverses this.
    for transform in (_trim_stack, collapse, _cap_middle):
        lines, op = transform(lines, sentinel)
        if op is not None:
            ops.append(op)

    compressed = "".join(lines)
    metadata: Dict[str, Any] = {
        "compressor": name,
        "version": VERSION,
        "sentinel": sentinel,
        "ops": ops,
    }
    return CompressResult(
        compressed=compressed,
        metadata=metadata,
        original_tokens=estimate_tokens(text),
        compressed_tokens=estimate_tokens(compressed),
        compressor=name,
        reversible=True,
    )


def compress(text: str) -> CompressResult:
    """Compress log / command output. Exactly reversible via ``decompress``."""
    return _compress(text, mask_digits=True, name=NAME)


def compress_generic(text: str) -> CompressResult:
    """Catch-all for arbitrary command output. Exactly reversible.

    Identical to `compress` except that runs must match *exactly* to collapse.
    Digit masking is what makes the log codec strong on timestamped noise, and
    it is precisely what would destroy a table of numbers, so unknown content
    does not get it.
    """
    return _compress(text, mask_digits=False, name=GENERIC_NAME)


def _expand(lines: List[str], sentinel: str, op: Dict[str, Any]) -> List[str]:
    """Reverse one op: replace each of its marker lines with the stored block."""
    tag: str = op["tag"]
    restore: Dict[str, List[str]] = op["restore"]
    rx = re.compile(re.escape(sentinel + tag) + r"(\d+)")
    out: List[str] = []
    for ln in lines:
        m = rx.search(ln)
        if m and m.group(1) in restore:
            out.extend(restore[m.group(1)])
        else:
            out.append(ln)
    return out


def decompress(compressed: str, metadata: Dict[str, Any]) -> str:
    """Reverse ``compress``, reproducing the original text exactly."""
    if metadata.get("compressor") == "identity":
        return compressed
    sentinel: str = metadata["sentinel"]
    ops: List[Dict[str, Any]] = metadata.get("ops", [])
    lines = _split_keep(compressed)
    for op in reversed(ops):
        lines = _expand(lines, sentinel, op)
    return "".join(lines)
