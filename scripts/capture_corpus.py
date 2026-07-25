"""Capture real command output into a local corpus, then measure against it.

Why this exists
---------------
`samples/` is synthetic — every file in it was written to exercise a codec, and
the harness scores 46.5% against it. On real session output the same codecs
scored 8%. That gap is not the codecs being broken; it is `samples/` being a
set of fixtures chosen by the same person who wrote the thing they test, which
is the oldest way to measure well and learn nothing.

This script builds the other corpus: run a list of ordinary commands, keep
their real stdout, and report what the codecs actually do to it. The number it
prints is the one worth optimizing.

Privacy
-------
Output lands in `samples/real/`, which is **gitignored**. It is verbatim output
from a real machine — absolute paths, branch names, installed packages — and is
local measurement data, not project content. `samples/` stays synthetic so the
published numbers remain reproducible by anyone. Never move a file from
`samples/real/` into `samples/`.

Usage
-----
    python scripts/capture_corpus.py            # capture + measure
    python scripts/capture_corpus.py --measure  # measure what's already there
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from ltc import compress_payload, decompress  # noqa: E402

# Deliberately NOT under samples/. A directory that must never be published
# should not live inside the one that is. The first version of this script put
# it at samples/real/ and the harness immediately tried to open the directory
# as a sample (PermissionError), which the test suite caught. Sibling trees,
# no chance of a real capture being mistaken for a publishable fixture.
CORPUS = os.path.join(REPO, "corpus")

# Ordinary commands from ordinary work. Deliberately NOT chosen for how well
# they compress -- that selection bias is the exact thing this corpus exists to
# escape. Read-only by design: nothing here mutates the repo.
COMMANDS = [
    ("git_status", ["git", "status"]),
    ("git_ls_files", ["git", "ls-files"]),
    ("git_log", ["git", "log", "--stat", "-10"]),
    ("git_diff_cached", ["git", "diff", "HEAD~1"]),
    ("git_branch", ["git", "branch", "-a", "-v"]),
    ("unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
    ("pip_list", [sys.executable, "-m", "pip", "list"]),
    ("ls_long", ["ls", "-la"]),
    ("ls_recursive", ["ls", "-R", "ltc", "tests"]),
    ("grep_def", ["grep", "-rn", "def ", "ltc"]),
]


def capture() -> int:
    os.makedirs(CORPUS, exist_ok=True)
    written = 0
    for name, argv in COMMANDS:
        try:
            proc = subprocess.run(argv, cwd=REPO, capture_output=True,
                                  text=True, timeout=180)
        except Exception as e:
            print(f"  skip {name}: {e}")
            continue
        # stdout only. stderr is kept out of the corpus for the same reason the
        # hook keeps it out of the pipe: mixing diagnostics into a payload
        # breaks the homogeneity every content detector depends on.
        out = proc.stdout
        if not out.strip():
            print(f"  skip {name}: no stdout")
            continue
        path = os.path.join(CORPUS, name + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        # The command sidecar the harness already understands, so the corpus
        # measures the routing path production actually takes (command hint
        # first) rather than content sniffing alone.
        with open(path + ".cmd", "w", encoding="utf-8") as f:
            f.write(" ".join(argv[:3]))
        written += 1
        print(f"  captured {name} ({len(out)} bytes)")
    return written


def measure() -> int:
    if not os.path.isdir(CORPUS):
        print(f"no corpus at {CORPUS} — run without --measure first")
        return 1

    names = sorted(n for n in os.listdir(CORPUS) if not n.endswith(".cmd"))
    if not names:
        print("corpus is empty")
        return 1

    print()
    print(f"{'sample':<22}{'codec':<10}{'orig':>7}{'saved':>8}{'reduc':>8}  round-trip")
    print("-" * 64)
    total_orig = total_saved = 0
    failures = 0
    identity = 0
    for name in names:
        path = os.path.join(CORPUS, name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        cmd = None
        if os.path.exists(path + ".cmd"):
            with open(path + ".cmd", "r", encoding="utf-8") as f:
                cmd = f.read().strip()

        res = compress_payload(content, command=cmd)
        total_orig += res.original_tokens
        total_saved += res.tokens_saved
        if res.compressor == "identity":
            identity += 1

        if res.reversible:
            ok = decompress(res.compressed, res.metadata) == content
            verdict = "OK" if ok else "FAIL"
            failures += 0 if ok else 1
        else:
            verdict = "lossy"
        print(f"{name[:21]:<22}{res.compressor:<10}{res.original_tokens:>7}"
              f"{res.tokens_saved:>8}{res.reduction:>7.0%}  {verdict}")

    print("-" * 64)
    overall = (total_saved / total_orig) if total_orig else 0.0
    print(f"{'TOTAL':<22}{'':<10}{total_orig:>7}{total_saved:>8}{overall:>7.0%}")
    print(f"\n{len(names)} samples, identity {identity} "
          f"({identity / len(names):.0%}), round-trip failures {failures}")
    print("\nCompare against `python -m ltc.harness` (synthetic samples/). A wide")
    print("gap means the synthetic corpus is flattering the codecs, not that the")
    print("codecs improved.")
    return 1 if failures else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--measure", action="store_true",
                   help="Skip capture; measure the existing corpus.")
    args = p.parse_args(argv)

    if not args.measure:
        print(f"capturing into {CORPUS}  (gitignored)")
        if capture() == 0:
            print("nothing captured")
            return 1
    return measure()


if __name__ == "__main__":
    raise SystemExit(main())
