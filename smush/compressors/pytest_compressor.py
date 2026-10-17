"""pytest / unittest run compressor. **Lossy.**

A test run is mostly ceremony. The platform banner, the plugin list, the
progress dots, the percentage column and the warnings summary are the same
every time and carry no information about *this* run; the failures and the
final tally carry all of it. So this codec keeps what a reader would act on
and discards the rest, rather than trying to re-encode everything reversibly
like the log/listing/json codecs do.

What is kept, always
--------------------
* every FAILURES / ERRORS section, verbatim except for library stack frames
* the "short test summary info" lines (``FAILED path::test - reason``)
* the final tally line (``1 failed, 41 passed in 0.42s``)
* unittest's ``FAIL:``/``ERROR:`` blocks and its ``Ran N tests`` / ``FAILED``
  trailer

What is dropped
---------------
* the session header (platform, rootdir, plugins, cachedir)
* per-file progress lines and bare dot/F/E progress
* the warnings summary section
* consecutive library traceback frames, folded to a count

Every one of those is recorded in ``CompressResult.dropped`` so the loss is
never silent.

Reversibility
-------------
None. ``decompress`` returns the compressed text unchanged and says so in its
docstring — the dropped bytes are not stored anywhere. That is the deliberate
trade: storing them would defeat the purpose, and the plan's safety mechanism
is the ``dropped`` list plus a re-runnable command, not reversal.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..result import CompressResult
from ..tokens import estimate_tokens
# The app-vs-library frame rule is a *shared tuning decision*, not incidental
# code: if a user adds their virtualenv layout to _LIBRARY_PATH_MARKERS it must
# take effect everywhere frames are folded. So these two are imported rather
# than copied, unlike the trivial line splitter below.
from .log_compressor import _frame_span, _is_library_frame

NAME = "pytest"
VERSION = 1

# --- tuning knobs -----------------------------------------------------------
# Section titles (from pytest's `==== title ====` bars) whose bodies are pure
# ceremony. Matched case-insensitively as a prefix of the title.
_NOISE_SECTIONS = (
    "test session starts",
    "warnings summary",
    "slowest",
)
# A `==== title ====` / `---- title ----` bar. pytest pads with '=' and the
# per-failure headers with '_'.
_BAR_RE = re.compile(r"^(?P<fill>[=_-])(?P=fill){3,}\s*(?P<title>.*?)\s*(?P=fill){3,}$")
# A bar with nothing in it: pure visual separator, always droppable.
_RULE_RE = re.compile(r"^[=_-]{4,}$")
# `tests/test_a.py ......F..                                        [ 23%]`
_PROGRESS_RE = re.compile(r"^\S+\.py[\s.sSFEExXuUpP]*(\[\s*\d+%\])?\s*$")
# unittest's bare progress line: just dots and outcome letters.
_BARE_PROGRESS_RE = re.compile(r"^[.sFExX]+$")
# Verbose per-test result lines, which are the bulk of `-v` output:
#   unittest: `test_x (pkg.mod.Case.test_x) ... ok`
#   pytest:   `tests/test_x.py::test_y PASSED                          [ 23%]`
# Only unambiguous passes are dropped. FAILED, ERROR and SKIPPED lines stay:
# a skip is a test that did not run, which a reader may well need to know.
_VERBOSE_PASS_RE = re.compile(r"^\S.*\.\.\. ok\s*$|^\S+::\S+\s+PASSED\b.*$")
# The final tally, with or without its bar: "1 failed, 41 passed in 0.42s".
_TALLY_RE = re.compile(r"^\s*\d+\s+(passed|failed|error|skipped|deselected|xfailed|xpassed)\b")
# unittest trailers worth keeping verbatim.
_UNITTEST_TAIL_RE = re.compile(r"^(Ran \d+ tests?|OK\b|FAILED\b)")


def _split_keep(text: str) -> List[str]:
    """Split on '\\n' only, keeping the terminators, so ``"".join`` is exact.

    Duplicated per module convention (see listing_compressor) — it is three
    lines of text mechanics with no tuning decision in it.
    """
    if text == "":
        return []
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1] != "":
        lines.append(parts[-1])
    return lines


def _section_title(line: str) -> Optional[str]:
    """The title of a pytest section bar, or None if `line` isn't a bar."""
    m = _BAR_RE.match(line.rstrip("\n"))
    if not m:
        return None
    return m.group("title")


def _is_noise_section(title: str) -> bool:
    low = title.lower()
    return any(low.startswith(marker) for marker in _NOISE_SECTIONS)


def _fold_library_frames(lines: List[str]) -> Tuple[List[str], int]:
    """Replace runs of library traceback frames with one count marker.

    Returns the new lines and how many frames were folded away. Mirrors
    ``log_compressor._trim_stack`` but emits a plain, readable marker instead
    of a reversible sentinel — nothing here is coming back, so a marker the
    model can read beats one the decompressor could parse.
    """
    out: List[str] = []
    folded = 0
    i = 0
    n = len(lines)
    while i < n:
        span = _frame_span(lines, i)
        if span is None or not _is_library_frame(span[1]):
            out.append(lines[i])
            i += 1
            continue
        indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        frames = 0
        while i < n:
            span = _frame_span(lines, i)
            if span is None or not _is_library_frame(span[1]):
                break
            frames += 1
            i = span[0]
        folded += frames
        out.append(f"{indent}… {frames} library frame(s) hidden …\n")
    return out, folded


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


def compress(text: str) -> CompressResult:
    """Compress a test run to its failures and tally. Lossy; not reversible."""
    if text == "":
        return _identity_result(text, "empty input")

    lines = _split_keep(text)
    out: List[str] = []
    # `keeping` flips off inside a noise section and back on at the next bar.
    keeping = True
    progress_dropped = 0
    passing_dropped = 0
    header_dropped = 0
    blank_dropped = 0
    blank_run = 0

    for line in lines:
        body = line.rstrip("\n")
        title = _section_title(line)

        if title is not None:
            if _is_noise_section(title):
                keeping = False
                header_dropped += 1
                continue
            keeping = True
            if title:
                # Keep the section's identity, drop its 70 columns of padding.
                # Per-failure headers ("____ test_thing ____") are titles too,
                # and this is exactly what we want for them.
                out.append(f"== {title} ==\n")
            continue

        if _RULE_RE.match(body):
            continue  # bare separator rule: pure decoration

        if _PROGRESS_RE.match(body) or _BARE_PROGRESS_RE.match(body):
            progress_dropped += 1
            continue

        if _VERBOSE_PASS_RE.match(body):
            passing_dropped += 1
            continue

        # The tally and unittest's trailer survive even inside a noise section:
        # they are the one thing every reader wants.
        if _TALLY_RE.match(body) or _UNITTEST_TAIL_RE.match(body):
            out.append(line)
            keeping = True
            continue

        if not keeping:
            if body.strip():
                header_dropped += 1
            continue

        if not body.strip():
            # Collapse blank-line padding between kept blocks to at most one.
            blank_run += 1
            if blank_run > 1:
                blank_dropped += 1
                continue
        else:
            blank_run = 0
        out.append(line)

    out, folded_frames = _fold_library_frames(out)

    dropped: List[str] = []
    if progress_dropped:
        dropped.append(f"{progress_dropped} progress line(s)")
    if passing_dropped:
        dropped.append(f"{passing_dropped} passing test name(s)")
    if header_dropped:
        dropped.append(f"{header_dropped} session/warning header line(s)")
    if folded_frames:
        dropped.append(f"{folded_frames} library stack frame(s)")
    # Counted last and listed last because it is the least interesting loss —
    # but it must be counted. Blank-line collapsing alone can shrink a payload
    # (a `unittest -v` run hits exactly this path), and a lossy result with an
    # empty `dropped` list would leave the footer saying "dropped unspecified",
    # defeating the one mechanism that makes lossy compression safe.
    if blank_dropped:
        dropped.append(f"{blank_dropped} blank line(s)")

    compressed = "".join(out)
    if not dropped and compressed == text:
        return _identity_result(text, "nothing to drop")

    metadata: Dict[str, Any] = {
        "compressor": NAME,
        "version": VERSION,
        "lossy": True,
        "dropped": dropped,
    }
    return CompressResult(
        compressed=compressed,
        metadata=metadata,
        original_tokens=estimate_tokens(text),
        compressed_tokens=estimate_tokens(compressed),
        compressor=NAME,
        reversible=False,
        dropped=dropped,
    )


def decompress(compressed: str, metadata: Dict[str, Any]) -> str:
    """Return `compressed` unchanged — this codec is lossy by design.

    Kept so the registry has a uniform (compress, decompress) pair and no
    caller has to special-case lossy codecs. What was dropped is described in
    ``metadata["dropped"]``; recovering it means re-running the tests.
    """
    return compressed
