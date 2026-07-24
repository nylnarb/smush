"""Interactive chat REPL that compresses every turn with ltc before sending it
to either Ollama or Claude — same script, pick the backend with a flag, so two
terminal windows can each run it side by side with no file edits needed.

Not part of the ltc package (ltc stays stdlib-only, no-network) — this is a
standalone harness script, same pattern as ollama_demo.py.

Usage:
    python scripts/chat.py --backend ollama --model ornith:latest
    python scripts/chat.py --backend claude --model claude-sonnet-5

Env:
    ANTHROPIC_API_KEY   required for --backend claude

Each line you type is compressed via ltc.compress_payload(..., "auto") before
being sent as the prompt/message. Type 'exit' or Ctrl-C to quit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ltc import compress_payload

OLLAMA_URL = "http://localhost:11434/api/generate"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_VERSION = "2023-06-01"


def ask_ollama(model: str, prompt: str) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["response"]


def ask_claude(model: str, prompt: str, api_key: str) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        CLAUDE_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": CLAUDE_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        return "".join(block["text"] for block in data["content"] if block["type"] == "text")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["ollama", "claude"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--stats", action="store_true", help="Print ltc compression stats per turn.")
    args = parser.parse_args()

    api_key = None
    if args.backend == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
            return 1

    print(f"[chat] backend={args.backend} model={args.model} (type 'exit' to quit)")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in ("exit", "quit"):
            return 0
        if not line.strip():
            continue

        result = compress_payload(line, "auto")
        if args.stats:
            print(
                f"[ltc] {result.compressor} {result.original_tokens}->"
                f"{result.compressed_tokens} tokens ({result.reduction:.1%})",
                file=sys.stderr,
            )

        try:
            if args.backend == "ollama":
                answer = ask_ollama(args.model, result.compressed)
            else:
                answer = ask_claude(args.model, result.compressed, api_key)
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)
            continue

        print(answer.strip())


if __name__ == "__main__":
    raise SystemExit(main())
