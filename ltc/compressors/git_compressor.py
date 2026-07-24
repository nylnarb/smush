"""git output compressor. **Lossy.**

Three git commands account for nearly all of the git output an assistant ever
reads, and each wastes tokens in its own way:

* ``git status`` — half the lines are ``(use "git restore <file>..." to ...)``
  hints the reader already knows, and every changed path gets its own line with
  a repeated ``modified:`` label.
* ``git diff`` / ``git show`` — hunks carry three lines of unchanged context on
  each side plus ``index 3f2a1b..9c4d2e 100644`` blobs nobody reads.
* ``git log`` — four lines and an email address per commit, to convey a sha, a
  date and a subject.

Each is handled by one transform, and every transform is a no-op on content
that doesn't have its shape. They run in sequence rather than being dispatched
between, because ``git log -p`` is genuinely both a log and a diff, and
``git show`` is a commit header followed by a diff.

Reversibility
-------------
None. ``decompress`` returns its input unchanged. Nothing load-bearing is
discarded — shas, paths, states, and every ``+``/``-`` line survive — but
context lines, hints and emails do not, and they are not stored anywhere. What
went is described in ``CompressResult.dropped``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..result import CompressResult
from ..tokens import estimate_tokens

NAME = "git"
VERSION = 1

# --- tuning knobs -----------------------------------------------------------
# Unchanged lines kept on each side of a change inside a hunk. Two is enough to
# see what a change sits between; git's default of three is one more than that.
_DIFF_CONTEXT = 2
# Paths listed per status group before the rest become a count.
_MAX_STATUS_PATHS = 40

# `commit 3f2a1b9c4d2e...` — the start of one `git log` entry.
_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{7,40})\b")
_AUTHOR_RE = re.compile(r"^Author:\s*(?P<name>.*?)\s*(?:<(?P<email>[^>]*)>)?\s*$")
_DATE_RE = re.compile(r"^Date:\s*(?P<date>.+?)\s*$")
# `Thu Jul 24 17:43:01 2026 -0500` -> the parts worth keeping.
_GIT_DATE_RE = re.compile(r"^\w{3}\s+(?P<mon>\w{3})\s+(?P<day>\d{1,2})\s+[\d:]+\s+(?P<year>\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}

# Diff scaffolding that carries no information a reader uses.
_DIFF_NOISE_RE = re.compile(
    r"^(index [0-9a-f]+\.\.[0-9a-f]+|--- (a/|/dev/null)|\+\+\+ (b/|/dev/null)"
    r"|(old|new) mode \d+|new file mode \d+|deleted file mode \d+"
    r"|similarity index \d+%)"
)
_HUNK_RE = re.compile(r"^@@ ")

# `        modified:   ltc/core.py` inside `git status`.
_STATUS_ENTRY_RE = re.compile(r"^(?P<indent>\s+)(?P<state>[a-z][a-z ]{2,20}):\s+(?P<path>\S.*)$")
# `  (use "git add <file>..." to include ...)` — a hint line, always droppable.
_STATUS_HINT_RE = re.compile(r"^\s*\(.*\)\s*$")
# Markers that identify long-format `git status` output.
_STATUS_MARKERS = (
    "Changes to be committed:",
    "Changes not staged for commit:",
    "Untracked files:",
    "nothing to commit",
)


def _split_keep(text: str) -> List[str]:
    """Split on '\\n' only, keeping the terminators (see listing_compressor)."""
    if text == "":
        return []
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1] != "":
        lines.append(parts[-1])
    return lines


def _looks_like_status(text: str) -> bool:
    """True for long-format ``git status``.

    Gating matters here: the status transform joins runs of indented lines,
    which would mangle an indented commit message in a ``git log``.
    """
    return text.startswith("On branch ") or any(m in text for m in _STATUS_MARKERS)


def _short_date(raw: str) -> str:
    """``Thu Jul 24 17:43:01 2026 -0500`` -> ``2026-07-24``; raw if unparsable."""
    m = _GIT_DATE_RE.match(raw)
    if not m:
        return raw
    month = _MONTHS.get(m.group("mon"))
    if month is None:
        return raw
    return f"{m.group('year')}-{month:02d}-{int(m.group('day')):02d}"


# --- transforms (each: lines -> (new_lines, dropped_descriptions)) ----------
def _condense_commits(lines: List[str]) -> Tuple[List[str], List[str]]:
    """Fold each ``git log`` commit header block into a single line.

    ``commit <sha>`` / ``Author:`` / ``Date:`` / blank / indented subject
    becomes ``<sha7> <date> <author>: <subject>``. A block that doesn't have
    that shape is passed through untouched, so ``--oneline``, ``--graph`` and
    custom ``--format`` output are unaffected.
    """
    out: List[str] = []
    commits = 0
    body_lines = 0
    emails = 0
    i = 0
    n = len(lines)
    while i < n:
        m = _COMMIT_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue

        sha = m.group(1)[:7]
        j = i + 1
        author = date = None
        # Header lines run until the blank line that precedes the message.
        while j < n and lines[j].strip() and not _COMMIT_RE.match(lines[j]):
            am = _AUTHOR_RE.match(lines[j].rstrip("\n"))
            dm = _DATE_RE.match(lines[j].rstrip("\n"))
            if am:
                author = am.group("name")
                if am.group("email"):
                    emails += 1
            elif dm:
                date = _short_date(dm.group("date"))
            j += 1

        if author is None and date is None:
            out.append(lines[i])  # not a log header block after all
            i += 1
            continue

        # The message: blank line, then indented lines. Keep the subject only.
        k = j
        while k < n and not lines[k].strip():
            k += 1
        subject = ""
        while k < n and (lines[k].startswith("    ") or not lines[k].strip()):
            stripped = lines[k].strip()
            if stripped and not subject:
                subject = stripped
            elif stripped:
                body_lines += 1
            k += 1

        parts = [sha]
        if date:
            parts.append(date)
        head = " ".join(parts)
        out.append(f"{head} {author}: {subject}\n" if author else f"{head} {subject}\n")
        commits += 1
        i = k

    dropped: List[str] = []
    if commits:
        dropped.append(f"{commits} commit header block(s) folded to one line each")
    if emails:
        dropped.append(f"{emails} author email address(es)")
    if body_lines:
        dropped.append(f"{body_lines} commit message body line(s)")
    return out, dropped


def _context_run_keep(run: List[str], has_before: bool, has_after: bool) -> Tuple[List[str], int]:
    """Trim one run of unchanged diff lines to the parts touching a change."""
    head = run[:_DIFF_CONTEXT] if has_before else []
    tail = run[-_DIFF_CONTEXT:] if has_after else []
    if len(run) <= len(head) + len(tail):
        return run, 0
    if not head and not tail:
        return run, 0  # a hunk of pure context: nothing to anchor a trim to
    hidden = len(run) - len(head) - len(tail)
    return head + [f"… {hidden} unchanged line(s) …\n"] + tail, hidden


def _cap_diff_context(lines: List[str]) -> Tuple[List[str], List[str]]:
    """Drop diff scaffolding and trim unchanged context inside each hunk."""
    out: List[str] = []
    noise = 0
    hidden_total = 0

    # A hunk body is buffered so a context run can see whether a change follows
    # it — you cannot decide how much of a run to keep until you know that.
    hunk: List[str] = []
    in_hunk = False

    def flush_hunk() -> None:
        nonlocal hidden_total
        if not hunk:
            return
        runs: List[Tuple[bool, List[str]]] = []  # (is_context, lines)
        for ln in hunk:
            is_ctx = ln.startswith(" ") or not ln.strip()
            if runs and runs[-1][0] == is_ctx:
                runs[-1][1].append(ln)
            else:
                runs.append((is_ctx, [ln]))
        for idx, (is_ctx, run) in enumerate(runs):
            if not is_ctx:
                out.extend(run)
                continue
            kept, hidden = _context_run_keep(run, idx > 0, idx < len(runs) - 1)
            hidden_total += hidden
            out.extend(kept)
        hunk.clear()

    for line in lines:
        # The noise patterns are only checked *outside* a hunk: inside one, a
        # removed source line legitimately starts with `---`, and dropping it
        # would silently change the diff's meaning.
        if not in_hunk and _DIFF_NOISE_RE.match(line):
            noise += 1
            continue
        if _HUNK_RE.match(line):
            flush_hunk()
            in_hunk = True
            out.append(line)
            continue
        if in_hunk and (line[:1] in ("+", "-", " ", "\\") or not line.strip()):
            # An empty line inside a hunk is a context line whose single
            # leading space was stripped somewhere in transit — common enough
            # (editors, copy-paste, CI log viewers) that treating it as the end
            # of the hunk would leave most real diffs half-uncompressed.
            hunk.append(line)
            continue
        flush_hunk()
        in_hunk = False
        out.append(line)
    flush_hunk()

    dropped: List[str] = []
    if noise:
        dropped.append(f"{noise} diff scaffolding line(s) (index/mode/---/+++)")
    if hidden_total:
        dropped.append(f"{hidden_total} unchanged context line(s)")
    return out, dropped


def _group_status(lines: List[str]) -> Tuple[List[str], List[str]]:
    """Drop ``(use ...)`` hints and put each status group on one line."""
    out: List[str] = []
    hints = 0
    grouped_paths = 0
    capped = 0

    pending_state: Optional[str] = None
    pending_indent = ""
    pending: List[str] = []
    bare: List[str] = []
    bare_indent = ""

    def flush() -> None:
        nonlocal pending_state, pending, grouped_paths, capped, bare
        for indent, label, paths in (
            (pending_indent, pending_state, pending),
            (bare_indent, None, bare),
        ):
            if not paths:
                continue
            shown = paths[:_MAX_STATUS_PATHS]
            extra = len(paths) - len(shown)
            capped += extra
            grouped_paths += len(paths)
            joined = ", ".join(shown) + (f" (+{extra} more)" if extra else "")
            out.append(f"{indent}{label + ': ' if label else ''}{joined}\n")
        pending_state, pending, bare = None, [], []

    for line in lines:
        body = line.rstrip("\n")
        if _STATUS_HINT_RE.match(body):
            hints += 1
            continue

        m = _STATUS_ENTRY_RE.match(body)
        if m:
            if bare:
                flush()
            state = m.group("state").strip()
            if state != pending_state:
                if pending:
                    flush()
                pending_state = state
                pending_indent = m.group("indent")
            pending.append(m.group("path").strip())
            continue

        # Untracked entries are bare indented paths with no state label.
        if body.strip() and body[:1] in (" ", "\t"):
            if pending:
                flush()
            bare_indent = body[:len(body) - len(body.lstrip())]
            bare.append(body.strip())
            continue

        flush()
        out.append(line)

    flush()

    dropped: List[str] = []
    if hints:
        dropped.append(f"{hints} git hint line(s)")
    if grouped_paths:
        dropped.append(f"{grouped_paths} path(s) grouped onto shared lines")
    if capped:
        dropped.append(f"{capped} path(s) beyond the per-group cap")
    return out, dropped


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
    """Compress git output. Lossy; not reversible."""
    if text == "":
        return _identity_result(text, "empty input")

    lines = _split_keep(text)
    dropped: List[str] = []

    transforms = [_condense_commits, _cap_diff_context]
    if _looks_like_status(text):
        transforms.append(_group_status)

    for transform in transforms:
        lines, notes = transform(lines)
        dropped.extend(notes)

    compressed = "".join(lines)
    if not dropped:
        # Nothing recognized (a `git push`, an empty status): stay honest and
        # reversible rather than claiming a compression that didn't happen.
        return _identity_result(text, "no git structure recognized")

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

    See ``metadata["dropped"]`` for what is missing; re-run the git command to
    get it back.
    """
    return compressed
