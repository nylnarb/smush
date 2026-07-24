"""Command-string -> content-type hint.

The single biggest lever ltc has: the caller usually knows *what produced*
the output, and that is far more reliable than guessing from the bytes. A
`pytest` run whose tests all passed looks nothing like a traceback, but we
still want the pytest codec; content sniffing alone can never know that.

Design constraints, same as the rest of the package: pure, stdlib-only, no
I/O, no global state. `classify` returns None rather than guessing when it
does not recognize a command, so the caller falls back to content sniffing.
Being wrong here is worse than being silent -- a wrong hint routes output to
a codec that will mangle it, whereas None just costs a little compression.
"""

from __future__ import annotations

import re
from typing import Optional

# Shell noise that precedes the real command: env assignments, `sudo`, and
# leading redirections. Stripped so `FOO=1 pytest -q` still classifies.
_LEADING_NOISE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+|sudo\s+|time\s+)+")

# Separators that start a new command within one shell string.
_SPLIT = re.compile(r"\|\||&&|\||;|\n")

# First token -> hint. Checked after python-module special cases below.
_DIRECT = {
    "pytest": "pytest",
    "py.test": "pytest",
    "tox": "pytest",
    "nose2": "pytest",
    "git": "git",
    "ls": "listing",
    "dir": "listing",
    "find": "listing",
    "fd": "listing",
    "grep": "listing",
    "rg": "listing",
    "ack": "listing",
    "tree": "listing",
}

# `git` subcommands worth the git codec. Anything else (push, clone, ...)
# produces short output where compression is pointless or harmful.
_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "blame", "branch"}

# `python -m <mod>` / `python -m <mod>` forms that mean "a test run".
_PYTHON_TEST_MODULES = {"pytest", "unittest", "nose2", "tox"}

_PYTHON_NAMES = ("python", "python3", "py", "python.exe", "python3.exe")


def _tokens(segment: str) -> list:
    """Cheap whitespace tokenization of one command segment."""
    return _LEADING_NOISE.sub("", segment).split()


def _basename(token: str) -> str:
    """Last path component of a command word, lowercased.

    Commands are frequently invoked by absolute path
    (``"C:/.../python.exe"``, ``.venv/Scripts/python``), so the bare name is
    what actually identifies the tool.
    """
    cleaned = token.strip().strip('"').strip("'")
    for sep in ("/", "\\"):
        if sep in cleaned:
            cleaned = cleaned.rsplit(sep, 1)[-1]
    return cleaned.lower()


def _classify_segment(segment: str) -> Optional[str]:
    """Hint for a single command (no pipes/semicolons), or None."""
    tokens = _tokens(segment)
    if not tokens:
        return None

    name = _basename(tokens[0])

    # `python -m pytest ...` and friends: the module is the real command.
    if name in _PYTHON_NAMES:
        if "-m" in tokens:
            i = tokens.index("-m")
            if i + 1 < len(tokens):
                module = tokens[i + 1].split(".")[0].lower()
                if module in _PYTHON_TEST_MODULES:
                    return "pytest"
        return None  # arbitrary python: unknowable, let content decide

    if name == "git":
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue  # skip global flags like `-c user.name=x`
            return "git" if tok.lower() in _GIT_SUBCOMMANDS else None
        return None

    return _DIRECT.get(name)


def classify(command: Optional[str]) -> Optional[str]:
    """Map a shell command string to a content-type hint, or None.

    Multi-command strings (``a && b | c``) are resolved to the hint of the
    *last* segment that yields one: in a pipeline the final stage produces
    what actually reaches the terminal, and in a `&&` chain the trailing
    command is almost always the one whose output matters.
    """
    if not command or not command.strip():
        return None
    hint = None
    for segment in _SPLIT.split(command):
        seg_hint = _classify_segment(segment)
        if seg_hint is not None:
            hint = seg_hint
    return hint
