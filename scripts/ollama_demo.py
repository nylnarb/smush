"""Demo: measure smush compression against a real local model via Ollama.

Not part of the smush package (smush stays stdlib-only, no-network) — this is a
standalone harness script that talks to a locally running `ollama serve` over
localhost HTTP. It sends the same question with (a) the raw payload and (b)
the smush-compressed payload as context, and checks the model gives an
equivalent answer either way, to sanity-check that compression doesn't
destroy information the model actually needs.

Usage:
    ollama serve                      # in another terminal, if not running
    python scripts/ollama_demo.py [MODEL]

Defaults to MODEL=ornith:latest.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smush import compress_payload

OLLAMA_URL = "http://localhost:11434/api/generate"


def ask(model: str, prompt: str) -> str:
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["response"]


SAMPLE_LOG = """Traceback (most recent call last):
  File "/usr/lib/python3.11/site-packages/flask/app.py", line 1478, in wsgi_app
    response = self.full_dispatch_request()
  File "/usr/lib/python3.11/site-packages/flask/app.py", line 880, in full_dispatch_request
    rv = self.handle_user_exception(e)
  File "/usr/lib/python3.11/site-packages/flask/app.py", line 878, in full_dispatch_request
    rv = self.dispatch_request()
  File "app/routes/orders.py", line 42, in get_order
    order = db.session.query(Order).filter_by(id=order_id).one()
sqlalchemy.orm.exc.NoResultFound: No row was found when one was required
""" * 3 + 'processing order 88221\n' * 40


def run(model: str) -> None:
    result = compress_payload(SAMPLE_LOG, "log")
    print(f"model: {model}")
    print(f"compressor: {result.compressor}  reduction: {result.reduction:.1%}  "
          f"tokens: {result.original_tokens} -> {result.compressed_tokens}")

    question = "\n\nIn one short sentence, what exception was ultimately raised and in which file?"

    print("\n--- asking with RAW payload ---")
    raw_answer = ask(model, SAMPLE_LOG + question)
    print(raw_answer.strip())

    print("\n--- asking with COMPRESSED payload ---")
    compressed_answer = ask(model, result.compressed + question)
    print(compressed_answer.strip())


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "ornith:latest"
    run(model)
