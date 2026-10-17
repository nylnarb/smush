#!/usr/bin/env python3
"""PostToolUse hook: compress Read output before it reaches the model.

Read output is ~37% of a session's context -- nearly twice what Bash output
is -- and until Claude Code 2.1.220 it was unreachable from hook-land. That
release added `updatedToolOutput` to the PostToolUse schema ("Replaces the
tool output before it is sent to the model"), which is what this hook uses.

    ~/.claude/.compress_mode_read  contains `on` or `off` (default off).

Deliberately a SEPARATE switch from `.compress_mode`, which governs Bash. The
two are orthogonal: rtk has no Read equivalent at all, so gating Read
compression on the Bash mode would mean giving up rtk's Bash rewriting -- the
better of the two on Bash today -- purely to reach Read. A split switch lets
both run at once, which is the configuration that actually maximizes savings.

Registering this alongside the existing PreToolUse Bash dispatcher is safe:
the one-hook-per-event rule is PER EVENT, and this is the only PostToolUse
hook. Never add a second one on PostToolUse -- two hooks each returning
`updatedToolOutput` race exactly the way the two PreToolUse hooks did, and the
loser's work vanishes silently.

THE LINE-NUMBER HAZARD, which shapes every gate below.

`tool_response["file"]["content"]` is the RAW file text. Claude Code applies
the `N->` line-number prefixes downstream, AFTER this hook. So compressing the
content does not renumber a numbered document -- it renumbers a *different*
document. Line 40 of a compressed file is not line 40 of the file on disk.

For a log or a data dump nobody minds. For source code the model is about to
edit, it means every `file.py:120` reference it reports is wrong, and every
Edit it attempts quotes text that is not in the file. That is a correctness
bug wearing a saved-token costume. So:

  - Source files use `generic_padded` (smush.compressors.log_compressor),
    which refills every dropped block with blank filler lines so the output
    has EXACTLY as many lines as the input -- line N stays line N. Measured
    against the real corpus (research/, smush/) this recovers ~76% of the
    unpadded codec's savings while keeping line numbers correct by
    construction. Priced in NEXT_SESSION_PROMPTS.txt PART 4 item B.
  - As a second, independent safety net (belief in the codec's own tests is
    not enough for something this correctness-sensitive), the line count of
    the padded output is re-checked here, in the hook, before it is ever
    applied. Any mismatch refuses and falls back to shadow-measurement.
  - Non-source files use the unpadded `generic` codec via "auto", since they
    are not quoted back by line number.
  - Lossy results are refused outright regardless of file type. Losing a line
    of a file the model is reasoning about is never a good trade here.

SAFETY: must never break a Read. Any error, any refusal, any doubt -> emit
nothing and exit 0, which leaves the original output untouched.
"""
import sys
import json
import os
import datetime

CLAUDE_DIR = os.environ.get(
    "SMUSH_CLAUDE_DIR", os.path.join(os.path.expanduser("~"), ".claude")
)
MODE_FILE = os.path.join(CLAUDE_DIR, ".compress_mode_read")
LOG = os.path.join(CLAUDE_DIR, "hooks", "smush-read-compress.log")

SMUSH_REPO = os.environ.get(
    "SMUSH_REPO",
    os.path.join(os.path.expanduser("~"), "repos", "smush"),
)

# Below this, the wrapping overhead is a larger share than anything a codec
# can win back. Measured on Bash output: 38% of calls are under 50 tokens and
# hold 2% of the volume, so a threshold costs almost nothing and stops the
# ledger filling with no-op rows that make the hit rate lie.
MIN_CHARS = 2000

# Files the model reads in order to edit them. See the line-number hazard
# above. Shadow-measured only, never actually compressed.
SOURCE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".java", ".kt", ".swift",
    ".go", ".rs", ".rb", ".php", ".scala", ".sh", ".bash", ".ps1",
    ".sql", ".r", ".jl", ".lua", ".pl", ".vim", ".el",
    ".html", ".css", ".scss", ".vue", ".svelte",
    ".toml", ".ini", ".cfg", ".yaml", ".yml",
    # Prose that gets edited is edited the same way code is -- by quoting a
    # line back. Markdown is not source, but it fails on renumbering for
    # exactly the reason source does.
    ".md", ".markdown", ".rst", ".adoc",
}

VALID_MODES = ("on", "off")
DEFAULT_MODE = "off"


def log(msg):
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (ts, msg))
    except Exception:
        pass


def passthrough(reason=""):
    """No stdout + exit 0 => the model receives the original Read output."""
    if reason:
        log("skip: " + reason)
    sys.exit(0)


def read_mode():
    try:
        with open(MODE_FILE, "r", encoding="utf-8") as f:
            mode = f.read().strip().lower()
    except Exception:
        return DEFAULT_MODE
    return mode if mode in VALID_MODES else DEFAULT_MODE


def main():
    raw = sys.stdin.read().lstrip("\ufeff")

    if read_mode() != "on":
        sys.exit(0)  # no log spam; the user asked for nothing to happen

    try:
        data = json.loads(raw)
    except Exception:
        passthrough()

    if data.get("tool_name") != "Read":
        sys.exit(0)

    resp = data.get("tool_response")
    if not isinstance(resp, dict):
        passthrough("tool_response not a dict")

    fileobj = resp.get("file")
    if not isinstance(fileobj, dict):
        passthrough("no file object (image/notebook/PDF read?)")

    content = fileobj.get("content")
    if not isinstance(content, str) or len(content) < MIN_CHARS:
        sys.exit(0)  # too small to be worth a ledger row

    path = fileobj.get("filePath") or ""
    ext = os.path.splitext(path)[1].lower()

    sys.path.insert(0, SMUSH_REPO)
    try:
        from smush import compress_payload
    except Exception as e:
        passthrough("smush import failed: %r" % (e,))

    is_source = ext in SOURCE_EXTS
    try:
        # No command hint on purpose. Every command-aware codec fails closed
        # without one, which is exactly the behaviour wanted here: routing is
        # allowed to pick a codec, nothing is allowed to decide a line is
        # unwanted. Source/markdown always route to the padded codec
        # explicitly -- "auto" only ever sniffs its way to the unpadded one.
        result = compress_payload(
            content,
            content_type="generic_padded" if is_source else "auto",
        )
    except Exception as e:
        passthrough("compress raised: %r" % (e,))

    saved = result.original_tokens - result.compressed_tokens
    pct = (100.0 * saved / result.original_tokens) if result.original_tokens else 0.0
    tag = "%s %s %d->%d tok (%.1f%%)" % (
        os.path.basename(path), result.compressor,
        result.original_tokens, result.compressed_tokens, pct,
    )

    if result.lossy or not result.reversible:
        passthrough("REFUSED lossy | " + tag)

    if saved <= 0:
        passthrough("no gain | " + tag)

    if is_source:
        # Second, independent check that padding actually preserved the line
        # count -- belief in the codec's own unit tests is not enough for
        # something this correctness-sensitive. Any mismatch falls back to
        # shadow-measurement rather than risk a wrong line number.
        orig_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        new_lines = result.compressed.count("\n") + (0 if result.compressed.endswith("\n") else 1)
        if new_lines != orig_lines:
            log("SHADOW (source, padding check failed %d!=%d) | %s" % (new_lines, orig_lines, tag))
            sys.exit(0)

    new_content = result.compressed + (
        "\n\n[smush: compressed by `%s`, %d->%d tokens (%.1f%% saved). %s]"
        % (result.compressor, result.original_tokens, result.compressed_tokens, pct,
           "Line numbers above match " + os.path.basename(path) + " exactly (padded)."
           if is_source else
           "Line numbers above are positions in this compressed text, NOT in "
           + os.path.basename(path) + ". Re-read with a limit/offset if you need true line numbers.")
    )

    # Mutate the observed object rather than rebuilding it. The replacement is
    # validated against Read's own outputSchema and must also survive
    # mapToolResultToToolResultBlockParam; fail either and Claude Code
    # SILENTLY reverts to the original, logging hook_error_during_execution.
    # Copying the real object forward is the only way to be sure the shape is
    # whatever this version actually expects.
    new_file = dict(fileobj)
    new_file["content"] = new_content
    new_file["numLines"] = new_content.count("\n") + 1
    new_resp = dict(resp)
    new_resp["file"] = new_file

    log("APPLIED | " + tag)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": new_resp,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # absolute backstop: a hook error must never break a Read
