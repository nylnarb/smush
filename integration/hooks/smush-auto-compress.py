#!/usr/bin/env python3
"""Bash output-compression dispatcher PreToolUse hook for Claude Code (Windows).

This is the ONLY PreToolUse hook registered for the Bash tool. It reads a
single mode file and delegates to exactly one compressor, so the two tools
can never race each other.

    ~/.claude/.compress_mode  contains one word:

        smush   -> pipe the command's stdout through `python -m smush compress`
        rtk   -> hand off to headroom-rtk-rewrite.py (rtk's own rewriting)
        off   -> do nothing; the command runs unmodified
        (missing / unreadable / anything else) -> treated as `rtk`

Why one hook instead of two: Claude Code gives every PreToolUse hook the
*original* tool_input, and each hook that emits `updatedInput` competes to
replace it -- only one survives. With two registered hooks, whichever is
listed first silently wins and the other's work is discarded regardless of
any switch file. Collapsing them into one dispatcher is what makes the mode
switch actually decide anything.

Toggle with /smush-on, /rtk-on, /compress-off; inspect with /compress-status.

SAFETY: Must never block a command. Any error, unknown mode, missing binary,
or non-rewritable command results in silent passthrough (exit 0, no output).

INSTALLATION: see integration/README.md in the smush repo. Every machine-specific
path below is an environment-variable override with a portable default:

    SMUSH_CLAUDE_DIR  where .claude lives          (default: ~/.claude)
    SMUSH_PYTHON      interpreter that runs smush    (default: this interpreter)
    SMUSH_REPO        clone of smush (default: ~/repos/smush)
"""
import sys
import json
import os
import re
import subprocess
import datetime

HOME = os.path.expanduser("~")

CLAUDE_DIR = os.environ.get("SMUSH_CLAUDE_DIR", os.path.join(HOME, ".claude"))
MODE_FILE = os.path.join(CLAUDE_DIR, ".compress_mode")
LOG = os.path.join(CLAUDE_DIR, "hooks", "smush-auto-compress.log")
RTK_HOOK = os.path.join(CLAUDE_DIR, "hooks", "headroom-rtk-rewrite.py")

# Overridable so the same hook works on a machine with a different layout
# (e.g. a work laptop) without editing this file.
PYTHON = os.environ.get("SMUSH_PYTHON", sys.executable)
SMUSH_REPO = os.environ.get(
    "SMUSH_REPO", os.path.join(HOME, "repos", "smush")
)
STATS_DB = os.path.join(SMUSH_REPO, "smush_stats.db")

VALID_MODES = ("smush", "rtk", "off")
DEFAULT_MODE = "rtk"

# Commands where wrapping in a pipe would break the point of running them
# (interactive tools that need a real terminal on stdout).
#
# NOTE: "cd " used to be in this list, which was a large silent hole. The test
# is startswith against the WHOLE command, so `cd <repo> && <anything>` -- how
# most commands in a repo session are actually written -- matched and was
# exempted outright. Measured on one real session: 38 of 55 Bash calls skipped
# this way. A leading cd is now hoisted out of the wrapper instead (see
# split_leading_cd).
SKIP_PREFIXES = ("claude ", "ollama run")

# A leading `cd <dir> &&` / `cd <dir>;` chain, one link at a time. The dir may
# be quoted (paths on Windows commonly contain spaces), so accept a quoted run
# or an unquoted token that stops at the next shell operator.
CD_PREFIX_RE = re.compile(
    r'^\s*cd\s+(?:"[^"]*"|\'[^\']*\'|[^\s;&|]+)\s*(?:&&|;)\s*'
)
# A bare `cd <dir>` with nothing after it: there is no output to compress.
BARE_CD_RE = re.compile(r'^\s*cd(\s|$)')


def log(msg):
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (ts, msg))
    except Exception:
        pass


def passthrough(reason=""):
    """No stdout + exit 0 => Claude Code runs the original command unchanged."""
    if reason:
        log("passthrough: " + reason)
    sys.exit(0)


def read_mode():
    """Return one of VALID_MODES. Unreadable or unrecognized -> DEFAULT_MODE.

    Deliberately forgiving: a typo in the mode file must degrade to the old
    behaviour rather than silently disabling compression everywhere.
    """
    try:
        with open(MODE_FILE, "r", encoding="utf-8") as f:
            mode = f.read().strip().lower()
    except Exception:
        return DEFAULT_MODE
    return mode if mode in VALID_MODES else DEFAULT_MODE


def split_leading_cd(cmd):
    """Split a leading `cd <dir> &&` chain off the front of `cmd`.

    Returns (cd_prefix, rest). The prefix keeps its trailing separator, so
    prefix + rest reconstructs the original command exactly.

    The cd has to run in the *parent* shell. Claude Code keeps one shell alive
    across Bash calls, so a cd swallowed into our `( ... )` subshell would stop
    persisting and quietly change where every later command runs. Hoisting it
    out is what makes `cd X && <cmd>` compressible at all -- the previous code
    skip-listed the whole thing rather than deal with this.
    """
    prefix = ""
    rest = cmd
    while True:
        m = CD_PREFIX_RE.match(rest)
        if not m:
            return prefix, rest
        prefix += m.group(0)
        rest = rest[m.end():]


def has_heredoc(cmd):
    """True if the command contains a heredoc, which wrapping would break.

    `python - <<'PY' ... PY` wrapped as `( ... ) 2>file | smush; cat file` is a
    bash syntax error: the redirections and the `;` trailer land between the
    heredoc's opening line and its body. Detection is deliberately crude -- a
    false positive costs one uncompressed command, while a false negative
    breaks the command outright, so the asymmetry says over-match.
    """
    return "<<" in cmd


def delegate_to_rtk(raw_stdin):
    """Run rtk's hook with the same payload and forward its verdict verbatim.

    headroom-rtk-rewrite.py is left completely unmodified; it stays an
    independently testable unit and this is the only place that calls it.
    """
    try:
        proc = subprocess.run(
            [PYTHON, RTK_HOOK],
            input=raw_stdin,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception as e:
        passthrough("rtk delegation failed: %r" % (e,))
    out = proc.stdout or ""
    if out.strip():
        sys.stdout.write(out)
    sys.exit(0)


def wrap_for_smush(data, tool_input, cd_prefix, cmd):
    """Rewrite the command so its stdout flows through `smush compress`.

    `cd_prefix` is emitted OUTSIDE the subshell so its directory change still
    persists into later Bash calls; only `cmd` is wrapped and compressed. It is
    also excluded from the --command hint, since `cd` says nothing about what
    kind of output is coming.

    stderr is deliberately NOT merged into the pipe: mixing diagnostics into
    the payload breaks the homogeneity every content detector depends on, and
    error text is exactly what must never be summarized away.

    --footer-when-lossy is always passed. The meta sidecar below is overwritten
    on every call, so a lossy compression is unrecoverable in this path; that
    one appended line is the only thing telling the reader what is missing.
    """
    session_id = data.get("session_id") or ""
    wrapped = cd_prefix + (
        '( %s ) 2>/tmp/.smush_stderr | PYTHONPATH="%s" "%s" -m smush compress '
        '--type auto --meta-out /tmp/.smush_last.meta.json --footer-when-lossy '
        '--log-stats "%s" --session-id "%s" --command "%s"; '
        'cat /tmp/.smush_stderr >&2'
    ) % (
        cmd,
        SMUSH_REPO.replace("\\", "/"),
        PYTHON.replace("\\", "/"),
        STATS_DB.replace("\\", "/"),
        session_id,
        cmd.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:500],
    )

    updated = dict(tool_input)
    updated["command"] = wrapped

    log("SMUSH | %s" % cmd)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        }
    }))
    sys.exit(0)


def main():
    # A stray UTF-8 BOM would otherwise fail json parsing and silently
    # degrade every command to passthrough.
    raw_stdin = sys.stdin.read().lstrip("\ufeff")

    mode = read_mode()
    if mode == "off":
        sys.exit(0)  # no log spam; the user asked for nothing to happen

    try:
        data = json.loads(raw_stdin)
    except Exception:
        passthrough()

    if data.get("tool_name") != "Bash":
        passthrough()

    if mode == "rtk":
        delegate_to_rtk(raw_stdin)

    # --- mode == "smush" -----------------------------------------------------
    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        passthrough()

    if has_heredoc(cmd):
        passthrough("skipped (heredoc): " + cmd.strip()[:60])

    # Hoist any `cd X &&` chain out first, then judge what is actually left:
    # `cd repo && claude foo` must still be recognized as an interactive
    # command, and `cd repo` alone must still be left alone.
    cd_prefix, rest = split_leading_cd(cmd)
    stripped = rest.strip()

    if not stripped or BARE_CD_RE.match(stripped):
        passthrough("skipped (bare cd, no output): " + cmd.strip()[:60])

    if any(stripped.startswith(p) for p in SKIP_PREFIXES):
        passthrough("skipped (interactive/skip-listed): " + stripped[:60])

    wrap_for_smush(data, tool_input, cd_prefix, rest)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # absolute backstop: a hook error must never block a command
