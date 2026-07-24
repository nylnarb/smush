"""Measurement harness: runs every registered compressor over sample payloads
and reports per-file reduction %, round-trip correctness, and worst cases.

Reads the registry dynamically via `ltc.core._REGISTRY` / `SUPPORTED_TYPES` so
it automatically covers any compressor added elsewhere, without hardcoding
compressor names here. Stdlib only; no network, no writes outside what the
caller explicitly asks for (this module never writes files).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, NamedTuple, Optional, Sequence

from . import compress_payload, decompress as _decompress


class SampleResult(NamedTuple):
    path: str
    compressor: str
    original_tokens: int
    compressed_tokens: int
    reduction: float
    round_trip_ok: bool
    error: Optional[str]
    lossy: bool = False


def _is_round_trip_ok(original: str, result) -> bool:
    """Semantic round-trip check: JSON compares parsed structures, everything
    else compares exact text (matches CompressResult.reversible's contract).

    A lossy result is exempt rather than failing: it never promised a
    round-trip, and the check that matters for it — that it said so — is
    asserted separately (`round_trip` renders as "lossy" in the table). The
    gate is deliberately on the result's own claim, so a codec that silently
    dropped content while still claiming reversible=True still fails here.
    """
    if not result.reversible:
        return True
    try:
        restored = _decompress(result.compressed, result.metadata)
    except Exception:
        return False
    if result.compressor == "json":
        try:
            return json.loads(restored) == json.loads(original)
        except ValueError:
            return restored == original
    return restored == original


#: A sample may name the command that produced it in a sidecar file, e.g.
#: `git_status.txt.cmd` containing `git status`. Without one the sample
#: measures content sniffing alone; with one it measures the path the hook
#: actually takes, which is the one that matters in production.
CMD_SUFFIX = ".cmd"


def _sidecar_command(path: str) -> Optional[str]:
    cmd_path = path + CMD_SUFFIX
    if not os.path.isfile(cmd_path):
        return None
    with open(cmd_path, "r", encoding="utf-8") as f:
        return f.read().strip() or None


def run_sample(path: str) -> SampleResult:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        result = compress_payload(content, "auto", command=_sidecar_command(path))
    except Exception as exc:  # noqa: BLE001 - harness must never crash on one bad sample
        return SampleResult(path, "error", 0, 0, 0.0, False, str(exc))

    ok = _is_round_trip_ok(content, result)
    return SampleResult(
        path=path,
        compressor=result.compressor,
        original_tokens=result.original_tokens,
        compressed_tokens=result.compressed_tokens,
        reduction=result.reduction,
        round_trip_ok=ok,
        error=None,
        lossy=result.lossy,
    )


def run_directory(samples_dir: str) -> List[SampleResult]:
    results = []
    for name in sorted(os.listdir(samples_dir)):
        path = os.path.join(samples_dir, name)
        # Sidecars are inputs to their sample, not samples themselves.
        if os.path.isfile(path) and not name.endswith(CMD_SUFFIX):
            results.append(run_sample(path))
    return results


def format_table(results: Sequence[SampleResult]) -> str:
    headers = ("file", "compressor", "orig_tok", "comp_tok", "reduction", "round_trip")
    rows = []
    for r in results:
        rows.append((
            os.path.basename(r.path),
            r.compressor,
            str(r.original_tokens),
            str(r.compressed_tokens),
            f"{r.reduction:.1%}" if r.error is None else "ERROR",
            ("lossy" if r.lossy else "OK") if r.round_trip_ok
            else ("FAIL" if r.error is None else r.error),
        ))

    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
              for i, h in enumerate(headers)]

    def fmt_row(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    lines.extend(fmt_row(row) for row in rows)

    ok_results = [r for r in results if r.error is None]
    failures = [r for r in results if not r.round_trip_ok]
    if ok_results:
        avg_reduction = sum(r.reduction for r in ok_results) / len(ok_results)
        worst = min(ok_results, key=lambda r: r.reduction)
        lines.append("")
        identity = [r for r in ok_results if r.compressor == "identity"]
        lines.append(f"samples: {len(results)}  round_trip_failures: {len(failures)}  "
                     f"lossy: {sum(1 for r in ok_results if r.lossy)}  "
                     f"identity: {len(identity)} ({len(identity) / len(ok_results):.0%})")
        lines.append(f"average reduction: {avg_reduction:.1%}")
        lines.append(f"worst case: {os.path.basename(worst.path)} "
                     f"({worst.reduction:.1%}, compressor={worst.compressor})")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ltc.harness",
        description="Run all registered compressors over sample payloads and report reduction stats.",
    )
    default_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")
    parser.add_argument("samples_dir", nargs="?", default=default_dir,
                         help=f"Directory of sample payload files (default: {default_dir}).")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.samples_dir):
        print(f"error: no such directory {args.samples_dir!r}", file=sys.stderr)
        return 2

    results = run_directory(args.samples_dir)
    print(format_table(results))
    return 0 if all(r.round_trip_ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
