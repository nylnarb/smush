"""Command-line interface for ltc.

Stdlib only. Two modes:

  compress:   ltc compress <file|-> [--type TYPE] [--stats] [--meta-out PATH]
  decompress: ltc decompress <file|-> --meta PATH [--type TYPE (ignored)]

`--stats` prints a before/after token report to stderr so stdout stays a
clean pipe of the compressed/decompressed text. The metadata sidecar is
plain JSON, written/read explicitly since it's required to reverse a
compression.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from . import compress_payload, decompress as _decompress, SUPPORTED_TYPES
from . import stats_store


def _read_input(path: Optional[str]) -> str:
    if path is None or path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _default_meta_path(path: Optional[str]) -> str:
    if path is None or path == "-":
        return "stdin.ltc.meta.json"
    return path + ".ltc.meta.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ltc", description="Local token compressor.")
    sub = parser.add_subparsers(dest="command", required=True)

    compress_p = sub.add_parser("compress", help="Compress a file or stdin.")
    compress_p.add_argument("file", nargs="?", default="-",
                             help="Input file, or '-' for stdin (default).")
    compress_p.add_argument("--type", choices=SUPPORTED_TYPES, default="auto",
                             help="Content type (default: auto-detect).")
    compress_p.add_argument("--stats", action="store_true",
                             help="Print before/after token stats to stderr.")
    compress_p.add_argument("--meta-out", default=None,
                             help="Where to write the metadata sidecar JSON "
                                  "(default: <file>.ltc.meta.json).")
    compress_p.add_argument("--log-stats", default=None,
                             help="Append this call's token savings to a "
                                  "cumulative SQLite ledger at PATH (for "
                                  "persistent 'tokens saved' tracking).")
    compress_p.add_argument("--session-id", default=None,
                             help="Claude Code session id to tag this row with, "
                                  "so savings can be queried per-session.")
    # dest is NOT "command": the subparser above already claims that name for
    # the subcommand itself, and colliding silently breaks dispatch in main().
    compress_p.add_argument("--command", dest="source_command", default=None,
                             help="The shell command that produced this input. "
                                  "Used as a content-type hint when --type is "
                                  "'auto'; far more reliable than sniffing.")

    decompress_p = sub.add_parser("decompress", help="Decompress a file or stdin.")
    decompress_p.add_argument("file", nargs="?", default="-",
                               help="Compressed input file, or '-' for stdin (default).")
    decompress_p.add_argument("--meta", required=True,
                               help="Path to the metadata sidecar JSON produced by "
                                    "`ltc compress --meta-out`.")

    gain_p = sub.add_parser("gain", help="Print cumulative token savings.")
    gain_p.add_argument("--db", default=None,
                         help="Path to the stats ledger (default: ltc_stats.db "
                              "next to this repo).")
    gain_p.add_argument("--session-id", default=None,
                         help="Restrict the total to one session id instead of "
                              "all-time global savings.")
    gain_p.add_argument("--breakdown", action="store_true",
                         help="Also print per-compressor and per-command "
                              "splits, and the identity rate (the share of "
                              "commands that compressed to nothing).")
    gain_p.add_argument("--top", type=int, default=10, metavar="N",
                         help="How many commands to list under --breakdown "
                              "(default: 10).")

    return parser


def _run_compress(args: argparse.Namespace) -> int:
    content = _read_input(args.file)
    result = compress_payload(content, args.type, command=args.source_command)

    meta_path = args.meta_out or _default_meta_path(args.file)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(result.metadata, f, indent=2)

    sys.stdout.write(result.compressed)

    if args.log_stats:
        stats_store.record(args.log_stats, result, session_id=args.session_id,
                           command=args.source_command)

    if args.stats:
        d = result.as_dict()
        print(
            f"compressor={d['compressor']} "
            f"original_tokens={d['original_tokens']} "
            f"compressed_tokens={d['compressed_tokens']} "
            f"tokens_saved={d['tokens_saved']} "
            f"reduction={d['reduction']:.2%} "
            f"reversible={d['reversible']} "
            f"meta={meta_path}",
            file=sys.stderr,
        )
    return 0


def _run_decompress(args: argparse.Namespace) -> int:
    compressed = _read_input(args.file)
    with open(args.meta, "r", encoding="utf-8") as f:
        metadata = json.load(f)
    original = _decompress(compressed, metadata)
    sys.stdout.write(original)
    return 0


def _default_db_path() -> str:
    import os
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ltc_stats.db"
    )


def _print_breakdown(data: dict, label: str, top: int) -> None:
    """Human-readable report. Written for a terminal, not for parsing."""
    print(f"ltc savings ({label})")
    print(f"  {data['saved_tokens']} tokens saved of {data['original_tokens']} "
          f"({data['reduction']:.0%}) across {data['calls']} commands")
    # The number that says whether ltc is working at all: everything else can
    # look healthy while most commands quietly fall through to identity.
    print(f"  identity rate: {data['identity_rate']:.0%} "
          f"({data['identity_calls']} of {data['calls']} compressed to nothing)")
    print(f"  lossy calls:   {data['lossy_calls']}")

    if data["compressors"]:
        print("\n  by compressor")
        for row in data["compressors"]:
            print(f"    {row['name']:<10} {row['calls']:>4} calls  "
                  f"{row['saved_tokens']:>8} saved  {row['reduction']:>5.0%}")

    if data["commands"]:
        print(f"\n  top {min(top, len(data['commands']))} commands by savings")
        for row in data["commands"]:
            print(f"    {row['label']:<20} {row['calls']:>4} calls  "
                  f"{row['saved_tokens']:>8} saved  {row['reduction']:>5.0%}")


def _run_gain(args: argparse.Namespace) -> int:
    db_path = args.db or _default_db_path()
    label = f"session {args.session_id}" if args.session_id else "all-time"

    if args.breakdown:
        data = stats_store.breakdown(db_path, session_id=args.session_id,
                                     top_commands=args.top)
        if data is None:
            print(f"no stats available ({db_path} unreadable)")
            return 1
        if data["calls"] == 0:
            print(f"no compressions recorded ({label})")
            return 0
        _print_breakdown(data, label, args.top)
        return 0

    if args.session_id:
        total = stats_store.session_saved(db_path, args.session_id)
    else:
        total = stats_store.total_saved(db_path)
    if total is None:
        print(f"no stats available ({db_path} unreadable)")
        return 1
    print(f"{total} tokens saved ({label})")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compress":
        return _run_compress(args)
    if args.command == "decompress":
        return _run_decompress(args)
    if args.command == "gain":
        return _run_gain(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
