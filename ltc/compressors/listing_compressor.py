"""File-listing / search-result compressor.

Directory listings and grep-style search results are dominated by one thing:
the same long directory prefix repeated on every line. This compressor pulls
that prefix out once and caps runaway result counts, and both transforms are
exactly reversible.

Strategy
--------
The text is split into physical lines (see ``_split_keep`` in log_compressor;
duplicated here rather than imported, to keep each compressor module
self-contained). Two transforms are applied, in order:

1. Result capping. If there are more than ``_MAX_RESULTS`` lines, only the
   first ``_MAX_RESULTS`` are kept; the rest are replaced by a single
   " ... N results omitted ..." marker line. The omitted lines are stored
   verbatim in metadata so decompress can splice them back in exactly.

2. Common-prefix factoring. Each *kept* line (excluding the marker) is
   inspected for a leading path — either the whole line (plain ``ls``-style
   listing) or the segment before the first ``:`` (grep ``path:line:text``
   style). If every kept line has a detected path, and those paths share a
   directory prefix at least ``_MIN_PREFIX_LEN`` characters long ending on a
   path separator, the prefix is written once into a header line and stripped
   from the front of every line. If even one line lacks a detected path, or
   the shared prefix is too short, this transform is a no-op — homogeneity is
   required so decompress can prepend the prefix unconditionally rather than
   needing a per-line marker.

3. Long-format column elision (**lossy**, and the only lossy transform here).
   ``ls -l`` spends most of every line on a link count, owner, group, byte size
   and timestamp. When a majority of lines have that exact shape, those columns
   are dropped and only the mode and the name survive. Unlike the two
   transforms above, this one cannot be undone — so it only engages on content
   that unambiguously *is* long-format output, and the result reports it via
   ``CompressResult.dropped``. A plain listing or a grep result never triggers
   it and stays fully reversible.

Reversibility
-------------
As with the other codecs, a sentinel string absent from the input is chosen so
marker lines can never collide with real content. Decompressing reverses the
two reversible transforms in reverse order: prefix restoration first, then
splicing back any capped results, reproducing the original text exactly, byte
for byte. If column elision ran, everything else still reverses but the elided
columns are gone for good — which is why ``compress`` marks such a result
``reversible=False`` instead of quietly promising a round-trip it can't make.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..result import CompressResult
from ..tokens import estimate_tokens

NAME = "listing"
VERSION = 1

# --- tuning knobs -----------------------------------------------------------
# Only keep this many result lines before capping the rest.
_MAX_RESULTS = 200
# A shared prefix shorter than this isn't worth a header line.
_MIN_PREFIX_LEN = 4
# A per-directory group smaller than this costs more (its header) than it saves.
_MIN_GROUP_LINES = 3
# Separators recognized as path boundaries (checked in this order).
_SEPARATORS = ("/", "\\")
# Fraction of lines that must be long-format before column elision engages.
_LONG_FORMAT_MAJORITY = 0.7

# One `ls -l` line: mode, links, owner, [group], size, date, name. The group
# column is optional because `ls -lG` and some BSD flags omit it.
_LONG_FORMAT_RE = re.compile(
    r"^(?P<mode>[-dlbcps][-rwxsStT]{9}[+@.]?)\s+"
    r"\d+\s+"                                   # link count
    r"\S+\s+"                                   # owner
    r"(?:\S+\s+)?"                              # group (optional)
    r"[\d,]+\s+"                                # size
    r"(?:\w{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4})"   # `Jul 24 17:43` / `Jul 24 2026`
    r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s+[+-]\d{4})?)\s+"  # `--time-style=long-iso`
    r"(?P<name>\S.*)$"
)
# `ls -l` opens every directory with this; it is not a listing entry.
_TOTAL_HEADER_RE = re.compile(r"^\s*total\s+\d+\s*$", re.IGNORECASE)


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
    """Split on '\\n' only, keeping the terminators, so ``"".join`` is exact."""
    if text == "":
        return []
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1] != "":
        lines.append(parts[-1])
    return lines


# --- path detection ----------------------------------------------------------
def _looks_like_path(candidate: str) -> bool:
    """True if `candidate` looks like a filesystem path worth factoring.

    Deliberately simple: must be non-empty and contain a path separator. This
    is the one explicit, tunable rule for what counts as "a path".
    """
    if not candidate:
        return False
    return any(sep in candidate for sep in _SEPARATORS)


def _line_path(line: str) -> Optional[str]:
    """Return the path portion of a line (sans trailing '\\n'), or None.

    Handles two shapes: a bare path filling the whole line (``ls``-style), and
    ``path:rest`` (grep ``-n``-style). The colon split is tried first so a
    grep line's path doesn't accidentally include ``:line:text``.
    """
    body = line[:-1] if line.endswith("\n") else line
    if ":" in body:
        candidate = body.split(":", 1)[0]
        if _looks_like_path(candidate):
            return candidate
    if _looks_like_path(body):
        return body
    return None


def _common_prefix(paths: List[str]) -> str:
    """Longest common prefix of `paths`, trimmed back to a separator boundary.

    Trimming avoids cutting a shared prefix mid-component (e.g. never turns
    "/home/alice" and "/home/alicia" into a false-shared "/home/alici").
    """
    if not paths:
        return ""
    shortest = min(paths, key=len)
    length = 0
    for i, ch in enumerate(shortest):
        if all(p[i] == ch for p in paths):
            length = i + 1
        else:
            break
    prefix = shortest[:length]
    cut = max(prefix.rfind(sep) for sep in _SEPARATORS)
    if cut < 0:
        return ""
    return prefix[: cut + 1]


# --- transforms (each: lines -> (new_lines, op|None)) -----------------------
def _cap_results(lines: List[str], sentinel: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """If there are more than _MAX_RESULTS lines, drop the tail."""
    if len(lines) <= _MAX_RESULTS:
        return lines, None
    kept = lines[:_MAX_RESULTS]
    dropped = lines[_MAX_RESULTS:]
    marker = f"{sentinel}cap0 … {len(dropped)} results omitted …\n"
    op = {"tag": "cap", "restore": {"0": dropped}}
    return kept + [marker], op


def _is_long_format(lines: List[str]) -> bool:
    """True if a majority of `lines` have the `ls -l` column shape."""
    candidates = [ln for ln in lines
                  if ln.strip() and not _TOTAL_HEADER_RE.match(ln)]
    if not candidates:
        return False
    matches = sum(1 for ln in candidates if _LONG_FORMAT_RE.match(ln.rstrip("\n")))
    return matches >= len(candidates) * _LONG_FORMAT_MAJORITY


def looks_like_long_format(text: str) -> bool:
    """True if `text` is `ls -l`-style output. Used by the core sniffer.

    Long-format lines carry no path separator, so the sniffer's path-majority
    test rejects them — an `ls -la` with no command hint would otherwise never
    reach this codec at all. Exposed here rather than duplicated in core so the
    regex has exactly one home.
    """
    return _is_long_format(_split_keep(text))


def _elide_columns(lines: List[str], sentinel: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Lossy: reduce `ls -l` lines to mode + name, if that's what these are.

    The majority test is what makes this safe to run unconditionally: ordinary
    listings, grep hits and `find` output match ``_LONG_FORMAT_RE`` on zero
    lines, so the transform reports no op and the codec stays reversible.
    """
    if not _is_long_format(lines):
        return lines, None

    out: List[str] = []
    elided = 0
    for ln in lines:
        if _TOTAL_HEADER_RE.match(ln):
            # The `total N` header exists only to caption the size column we
            # just removed, so it goes with it.
            elided += 1
            continue
        m = _LONG_FORMAT_RE.match(ln.rstrip("\n"))
        if not m:
            out.append(ln)
            continue
        eol = "\n" if ln.endswith("\n") else ""
        out.append(f"{m.group('mode')} {m.group('name')}{eol}")
        elided += 1
    op = {"tag": "cols", "lines": elided}
    return out, op


def _factor_prefix(lines: List[str], sentinel: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Strip a shared directory prefix from every line into a header."""
    # A cap marker (if present) is the last line and carries no path; exclude
    # it from both path detection and rewriting.
    is_capped = bool(lines) and lines[-1].startswith(sentinel + "cap")
    body = lines[:-1] if is_capped else lines
    if not body:
        return lines, None

    paths = [_line_path(ln) for ln in body]
    if any(p is None for p in paths):
        return lines, None  # not homogeneous; leave untouched

    prefix = _common_prefix(paths)  # type: ignore[arg-type]
    if len(prefix) < _MIN_PREFIX_LEN:
        return lines, None

    header = f"{sentinel}base {prefix}\n"
    rewritten = [ln[len(prefix):] for ln in body]
    out = [header] + rewritten + (lines[-1:] if is_capped else [])
    op = {"tag": "base", "prefix": prefix}
    return out, op


def _line_dir(line: str) -> Optional[str]:
    """The directory part of a line's path, including its trailing separator.

    Returns None when the line has no detectable path or the path has no
    directory component (a bare filename at the listing root).
    """
    path = _line_path(line)
    if path is None:
        return None
    cut = max(path.rfind(sep) for sep in _SEPARATORS)
    if cut < 0:
        return None
    return path[: cut + 1]


def _group_dirs(lines: List[str], sentinel: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Factor a per-directory prefix out of each *consecutive run* of lines.

    Why this exists: ``_factor_prefix`` above factors ONE prefix shared by
    *every* line, so a listing spanning more than one directory factors nothing
    at all — the common prefix of ``.gitignore`` and
    ``ltc/compressors/git_compressor.py`` is ``""``. Measured on this repo,
    ``git ls-files`` (45 lines of almost entirely redundant paths across 6
    directories) compressed by exactly 0%. Real listings are nearly always
    multi-directory, so that was the common case failing, not an edge case.

    Only *consecutive* runs are grouped, and the lines keep their original
    order. Reordering to gather scattered directories would compress slightly
    better and would stop the result being a faithful listing — `ls`, `find`
    and `git ls-files` all emit sorted output, so same-directory lines are
    already adjacent and there is nothing to gain by shuffling.

    Exactly reversible: each group records its directory and line count, and
    decompress replays them in order.
    """
    is_capped = bool(lines) and lines[-1].startswith(sentinel + "cap")
    body = lines[:-1] if is_capped else lines
    if not body:
        return lines, None

    # Build consecutive runs sharing a directory.
    runs: List[Tuple[Optional[str], List[str]]] = []
    for ln in body:
        d = _line_dir(ln)
        if runs and runs[-1][0] == d:
            runs[-1][1].append(ln)
        else:
            runs.append((d, [ln]))

    out: List[str] = []
    groups: List[List[Any]] = []
    for d, run in runs:
        # A group has to clear its own header cost: the header spends the
        # directory once plus the marker, and each line saves the directory.
        # Below _MIN_GROUP_LINES that trade is a loss, so leave the run alone.
        if d is None or len(d) < _MIN_PREFIX_LEN or len(run) < _MIN_GROUP_LINES:
            out.extend(run)
            continue
        out.append(f"{sentinel}dir {d}\n")
        out.extend(ln[len(d):] for ln in run)
        groups.append([d, len(run)])

    if not groups:
        return lines, None
    out = out + (lines[-1:] if is_capped else [])
    return out, {"tag": "dirs", "groups": groups}


def _ungroup_dirs(lines: List[str], sentinel: str, op: Dict[str, Any]) -> List[str]:
    """Reverse ``_group_dirs``: consume each header and re-prefix its run."""
    groups: List[List[Any]] = op["groups"]
    out: List[str] = []
    i = 0
    g = 0
    while i < len(lines):
        line = lines[i]
        if g < len(groups):
            d, count = groups[g][0], int(groups[g][1])
            if line == f"{sentinel}dir {d}\n":
                for ln in lines[i + 1: i + 1 + count]:
                    out.append(d + ln)
                i += 1 + count
                g += 1
                continue
        out.append(line)
        i += 1
    return out


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


def compress(text: str) -> CompressResult:
    """Compress a file listing / search-result payload."""
    if text == "":
        return _identity_result(text, "empty input")

    sentinel = _pick_sentinel(text)
    lines = _split_keep(text)
    ops: List[Dict[str, Any]] = []

    # Column elision runs first: it turns `ls -l` lines into bare names, which
    # is what the later transforms know how to handle. Cap next, so a huge
    # listing's tail never has to be path-scanned or rewritten; the header
    # transform then runs only over the kept lines.
    # _group_dirs runs last, after the global prefix is gone: on a listing that
    # shares one root it factors the *remaining* per-directory tails, and on a
    # multi-directory listing (where _factor_prefix no-ops) it does all the work.
    for transform in (_elide_columns, _cap_results, _factor_prefix, _group_dirs):
        lines, op = transform(lines, sentinel)
        if op is not None:
            ops.append(op)

    compressed = "".join(lines)
    dropped: List[str] = [
        f"{op['lines']} line(s) of `ls -l` size/owner/date columns"
        for op in ops if op["tag"] == "cols"
    ]
    metadata: Dict[str, Any] = {
        "compressor": NAME,
        "version": VERSION,
        "sentinel": sentinel,
        "ops": ops,
        "lossy": bool(dropped),
        "dropped": dropped,
    }
    return CompressResult(
        compressed=compressed,
        metadata=metadata,
        original_tokens=estimate_tokens(text),
        compressed_tokens=estimate_tokens(compressed),
        compressor=NAME,
        reversible=not dropped,
        dropped=dropped,
    )


def _unfactor_prefix(lines: List[str], sentinel: str, op: Dict[str, Any]) -> List[str]:
    """Reverse ``_factor_prefix``: drop the header, prepend prefix to the rest."""
    prefix: str = op["prefix"]
    header = f"{sentinel}base {prefix}\n"
    if not lines or lines[0] != header:
        return lines  # defensive; should not happen given matching metadata
    body = lines[1:]
    is_capped = bool(body) and body[-1].startswith(sentinel + "cap")
    rewritten = body[:-1] if is_capped else body
    restored = [prefix + ln for ln in rewritten]
    return restored + (body[-1:] if is_capped else [])


def _uncap_results(lines: List[str], sentinel: str, op: Dict[str, Any]) -> List[str]:
    """Reverse ``_cap_results``: replace the marker line with the dropped tail."""
    restore: Dict[str, List[str]] = op["restore"]
    marker_prefix = sentinel + "cap"
    out: List[str] = []
    for ln in lines:
        if ln.startswith(marker_prefix):
            key = ln[len(marker_prefix):].split(" ", 1)[0]
            if key in restore:
                out.extend(restore[key])
                continue
        out.append(ln)
    return out


def decompress(compressed: str, metadata: Dict[str, Any]) -> str:
    """Reverse ``compress``.

    Exact, byte for byte, unless a ``cols`` op is present: those columns were
    discarded and are not recoverable, so everything else is still reversed and
    the elided columns simply stay gone. Callers that need to know check
    ``metadata["lossy"]`` (or ``CompressResult.reversible``) rather than
    inspecting the ops.
    """
    if metadata.get("compressor") == "identity":
        return compressed
    sentinel: str = metadata["sentinel"]
    ops: List[Dict[str, Any]] = metadata.get("ops", [])
    lines = _split_keep(compressed)
    for op in reversed(ops):
        if op["tag"] == "dirs":
            lines = _ungroup_dirs(lines, sentinel, op)
        elif op["tag"] == "base":
            lines = _unfactor_prefix(lines, sentinel, op)
        elif op["tag"] == "cap":
            lines = _uncap_results(lines, sentinel, op)
        # "cols" is deliberately unreversible; see the docstring.
    return "".join(lines)
