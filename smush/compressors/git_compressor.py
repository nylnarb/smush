"""git output compressor. **Lossy.**

Three git commands account for nearly all of the git output an assistant ever
reads, and each wastes tokens in its own way:

* ``git status`` — half the lines are ``(use "git restore <file>..." to ...)``
  hints the reader already knows, and every changed path gets its own line with
  a repeated ``modified:`` label.
* ``git diff`` / ``git show`` — every file opens with four lines of preamble
  that between them name the path three times, hunks carry three lines of
  unchanged context on each side, and each hunk header ends with a copy of a
  line from higher up the same file.
* ``git log`` — four lines and an email address per commit, to convey a sha, a
  date and a subject.

Each is handled by one transform, and every transform is a no-op on content
that doesn't have its shape. They run in sequence rather than being dispatched
between, because ``git log -p`` is genuinely both a log and a diff, and
``git show`` is a commit header followed by a diff.

Why ``git diff`` caps out around 5%
-----------------------------------
It is worth writing down, because the number looks like a bug and is not. On
the real-output corpus (``corpus/git_diff_cached.txt``, 15,777 tokens) the
composition is:

    +/- changed lines   14,006 tok   89%
    context lines        1,011 tok    6%
    hunk headers           308 tok    2%
    file preambles         374 tok    2%

The rule that a diff is read to understand a change — so no ``+``/``-`` line,
path, sha or hunk line-number may go — puts 89% of the payload permanently off
limits. Every remaining token is ceremony, and taking *all* of it is a ~10%
ceiling. This codec takes about half of that, which is most of the ceremony
that can go without making the output stop looking like a diff.

Measured and rejected, so the next reader does not re-derive them:

* **Prefix-factoring runs of same-sign lines** (the trick that works in
  ``listing_compressor``): 7 tokens, 0.0%. Paths in a listing share long
  prefixes by construction; source lines in a diff share indentation and
  nothing else, and indentation is already cheap.
* **Intraline factoring of ``-``/``+`` pairs** (``a{-old|+new}b``): 74 tokens,
  0.5%. Only 10 pairs in the corpus were similar enough to merge — real edits
  replace lines wholesale far more often than they tweak them.
* **Move detection** (a ``-`` line reappearing verbatim as a ``+``): 152
  tokens, 1.0%, over 8 lines. Not worth the false-positive risk.
* **Collapsing per-line ``+``/``-`` markers into per-run headers**: 752 tokens,
  4.8% — the only remaining lever above 1%, and deliberately not taken. It
  makes attributing a line to added-or-removed depend on counting rather than
  reading, in the one content type where getting that backwards is a
  correctness failure rather than a lost token.

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
# Unchanged lines kept on each side of a change inside a hunk. The hunk header
# carries the line numbers, so one anchor line per side is enough to place a
# change in its file; git's default of three is two more than that. Measured on
# the real-output corpus, 2 -> 1 is worth ~5x more than 3 -> 2, because git
# emits 3 lines a side and a run *between* two changes is therefore 6 long:
# keeping 2+2 hides 2 of them, keeping 1+1 hides 4.
_DIFF_CONTEXT = 1
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
# `@@ -13,6 +13,14 @@ def widget():` — the line numbers are load-bearing and
# always kept; the trailing text is the enclosing scope git guessed at, which is
# a copy of a line already present higher in the same file.
_HUNK_HEADER_RE = re.compile(r"^(?P<nums>@@ -[\d,]+ \+[\d,]+ @@)(?P<tail>.+)$")

# --- per-file diff preamble -------------------------------------------------
# Every file in a diff opens with four lines that between them carry one line of
# information: the path (three times), the blob shas, and the mode. These fold
# into a single header. Each pattern below is matched only inside a preamble —
# never inside a hunk, where a removed source line may legitimately start `---`.
_DIFF_GIT_RE = re.compile(r"^diff --git (?P<rest>.+?)\s*$")
_INDEX_RE = re.compile(
    r"^index (?P<a>[0-9a-f]+)\.\.(?P<b>[0-9a-f]+)(?: (?P<mode>\d+))?\s*$")
_NEW_FILE_RE = re.compile(r"^new file mode (?P<mode>\d+)\s*$")
_DEL_FILE_RE = re.compile(r"^deleted file mode (?P<mode>\d+)\s*$")
_OLD_MODE_RE = re.compile(r"^old mode (?P<mode>\d+)\s*$")
_NEW_MODE_RE = re.compile(r"^new mode (?P<mode>\d+)\s*$")
_RENAME_FROM_RE = re.compile(r"^(?:rename|copy) from (?P<path>.+?)\s*$")
_RENAME_TO_RE = re.compile(r"^(?:rename|copy) to (?P<path>.+?)\s*$")
_SIMILARITY_RE = re.compile(r"^(?:dis)?similarity index \d+%\s*$")
_MARKER_RE = re.compile(r"^(--- |\+\+\+ )")
_BINARY_RE = re.compile(r"^(Binary files .* differ|GIT binary patch)\s*$")

# The header lines the folder knows how to consume. Anything else ends the
# preamble and is passed through untouched — an unrecognized extended header
# (a future git version's) must survive, not be swallowed.
_PREAMBLE_RES = (
    _INDEX_RE, _NEW_FILE_RE, _DEL_FILE_RE, _OLD_MODE_RE, _NEW_MODE_RE,
    _RENAME_FROM_RE, _RENAME_TO_RE, _SIMILARITY_RE, _MARKER_RE, _BINARY_RE,
)

# `git diff -U10`, `--unified=10`, `--function-context`/`-W`: the user asked for
# wide context by name. Trimming it back answers a different question than the
# one asked, so the context transform stands down. Fail closed on the explicit
# request, not on the absence of one — a diff's shape is unambiguous, so the
# no-hint default stays the ordinary trim.
_WIDE_CONTEXT_RE = re.compile(
    r"(?:^|\s)(?:-U\s*\d+|--unified=\d+|--function-context|-W)(?:\s|$)")

# `        modified:   smush/core.py` inside `git status`.
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


def _split_diff_paths(rest: str) -> Tuple[Optional[str], Optional[str]]:
    """``a/old b/new`` -> ``("old", "new")``; ``(None, None)`` if unsure.

    Paths may contain spaces, and git quotes ones with worse than that. Rather
    than reimplement git's quoting rules, this looks for the ``" b/"`` that
    leaves a well-formed ``a/...`` on its left and gives up otherwise — a
    give-up means the preamble is passed through verbatim, which is the correct
    failure for a transform whose whole job is to not lose a path.
    """
    if rest.startswith('"'):
        return None, None
    idx = rest.find(" b/")
    while idx != -1:
        left, right = rest[:idx], rest[idx + 3:]
        if left.startswith("a/") and right and not right.startswith('"'):
            return left[2:], right
        idx = rest.find(" b/", idx + 1)
    return None, None


def _fold_preamble(header: Dict[str, Any]) -> Optional[str]:
    """Render one parsed per-file preamble as a single line, or None to bail."""
    old, new = header["old"], header["new"]
    if new is None:
        return None
    path = new if old in (None, new) else f"{old} -> {new}"

    flags: List[str] = []
    if header["binary"]:
        flags.append("binary")
    if header["new_file"] is not None:
        flags.append("new" if header["new_file"] == "100644"
                    else f"new mode {header['new_file']}")
    elif header["deleted"] is not None:
        flags.append("deleted" if header["deleted"] == "100644"
                    else f"deleted mode {header['deleted']}")
    if header["old_mode"] and header["new_mode"]:
        flags.append(f"mode {header['old_mode']}->{header['new_mode']}")

    parts = ["diff", path]
    if flags:
        parts.append("(" + ", ".join(flags) + ")")
    if header["sha_a"] and header["sha_b"]:
        parts.append(f"{header['sha_a']}..{header['sha_b']}")
    return " ".join(parts) + "\n"


def _fold_file_headers(lines: List[str]) -> Tuple[List[str], List[str]]:
    """Collapse each file's four-line diff preamble into one line.

    ``diff --git a/x b/x`` / ``index abc..def 100644`` / ``--- a/x`` / ``+++
    b/x`` states the path three times and the mode once to convey a path, two
    shas and a mode. It becomes ``diff x abc..def``. Nothing load-bearing goes:
    the path, both shas, and any new/deleted/rename/mode/binary fact survive on
    the folded line. What goes is the repetition and the mode when it did not
    change.

    A preamble whose paths cannot be parsed, or that contains an extended header
    this function does not recognize, is emitted verbatim. The transform is a
    no-op on anything that is not a ``diff --git`` preamble, which is what lets
    it run unconditionally alongside the log and status transforms.
    """
    out: List[str] = []
    folded = 0
    modes = 0
    i = 0
    n = len(lines)

    while i < n:
        m = _DIFF_GIT_RE.match(lines[i].rstrip("\n"))
        if not m:
            out.append(lines[i])
            i += 1
            continue

        old, new = _split_diff_paths(m.group("rest"))
        header: Dict[str, Any] = {
            "old": old, "new": new, "sha_a": None, "sha_b": None,
            "mode": None, "new_file": None, "deleted": None,
            "old_mode": None, "new_mode": None, "binary": False,
        }
        j = i + 1
        consumed: List[str] = []
        while j < n:
            body = lines[j].rstrip("\n")
            if not any(r.match(body) for r in _PREAMBLE_RES):
                break
            im = _INDEX_RE.match(body)
            if im:
                header["sha_a"], header["sha_b"] = im.group("a"), im.group("b")
                header["mode"] = im.group("mode")
            elif _NEW_FILE_RE.match(body):
                header["new_file"] = _NEW_FILE_RE.match(body).group("mode")
            elif _DEL_FILE_RE.match(body):
                header["deleted"] = _DEL_FILE_RE.match(body).group("mode")
            elif _OLD_MODE_RE.match(body):
                header["old_mode"] = _OLD_MODE_RE.match(body).group("mode")
            elif _NEW_MODE_RE.match(body):
                header["new_mode"] = _NEW_MODE_RE.match(body).group("mode")
            elif _BINARY_RE.match(body):
                header["binary"] = True
            elif _RENAME_FROM_RE.match(body):
                header["old"] = _RENAME_FROM_RE.match(body).group("path")
            elif _RENAME_TO_RE.match(body):
                header["new"] = _RENAME_TO_RE.match(body).group("path")
            elif _MARKER_RE.match(body):
                # `--- a/x` / `+++ b/x` carry only the path, already known. A
                # `/dev/null` on either side is redundant with the new/deleted
                # file mode line git always emits beside it.
                pass
            consumed.append(lines[j])
            j += 1

        line = _fold_preamble(header)
        if line is None:
            # Could not read it confidently: keep every byte.
            out.append(lines[i])
            out.extend(consumed)
        else:
            out.append(line)
            folded += 1
            if header["mode"] and header["new_file"] is None and \
                    header["deleted"] is None and not header["old_mode"]:
                modes += 1
        i = j

    dropped: List[str] = []
    if folded:
        dropped.append(f"{folded} per-file diff preamble(s) folded to one line "
                       f"(paths, shas and mode changes kept)")
    if modes:
        dropped.append(f"{modes} unchanged file mode marker(s)")
    return out, dropped


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


def _context_run_keep(run: List[str], has_before: bool, has_after: bool,
                      context: int = _DIFF_CONTEXT) -> Tuple[List[str], int]:
    """Trim one run of unchanged diff lines to the parts touching a change."""
    head = run[:context] if has_before else []
    tail = run[-context:] if (has_after and context) else []
    if len(run) <= len(head) + len(tail):
        return run, 0
    if not head and not tail:
        return run, 0  # a hunk of pure context: nothing to anchor a trim to
    hidden = len(run) - len(head) - len(tail)
    return head + [f"… {hidden} unchanged line(s) …\n"] + tail, hidden


def _cap_diff_context(lines: List[str],
                      context: Optional[int] = _DIFF_CONTEXT
                      ) -> Tuple[List[str], List[str]]:
    """Drop diff scaffolding and trim unchanged context inside each hunk.

    `context` of None leaves unchanged lines alone entirely — used when the
    command asked for wide context by name. The scaffolding and hunk-header
    work still happens; only the part the user explicitly overrode stands down.
    """
    out: List[str] = []
    noise = 0
    hidden_total = 0
    hunk_tails = 0

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
            if not is_ctx or context is None:
                out.extend(run)
                continue
            kept, hidden = _context_run_keep(run, idx > 0, idx < len(runs) - 1,
                                             context)
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
            # `@@ -a,b +c,d @@ <enclosing scope>`: the line numbers stay, the
            # trailing scope guess goes. It is git repeating a line that already
            # appears higher up in the same file, and on the real-output corpus
            # it is 60% of every hunk header's tokens.
            hm = _HUNK_HEADER_RE.match(line.rstrip("\n"))
            if hm and hm.group("tail").strip():
                out.append(hm.group("nums") + "\n")
                hunk_tails += 1
            else:
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
    if hunk_tails:
        dropped.append(f"{hunk_tails} hunk header scope hint(s) "
                       f"(line numbers kept)")
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


def wants_wide_context(command: Optional[str]) -> bool:
    """True if `command` asked for more diff context than git's default."""
    return bool(command) and bool(_WIDE_CONTEXT_RE.search(command))


def compress(text: str, command: Optional[str] = None) -> CompressResult:
    """Compress git output. Lossy; not reversible.

    `command` is used for exactly one decision: whether the caller asked for
    wide diff context (`-U10`, `--function-context`). Everything else is
    decided by the bytes, because a diff's shape is unambiguous in a way a
    directory listing's is not. With no hint the ordinary trim applies — the
    fail-closed case here is the *explicit* override, not its absence.
    """
    if text == "":
        return _identity_result(text, "empty input")

    lines = _split_keep(text)
    dropped: List[str] = []
    context = None if wants_wide_context(command) else _DIFF_CONTEXT

    def cap(ls: List[str]) -> Tuple[List[str], List[str]]:
        return _cap_diff_context(ls, context)

    transforms = [_condense_commits, _fold_file_headers, cap]
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
