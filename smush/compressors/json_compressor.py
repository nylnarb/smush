"""JSON compressor — the biggest win is arrays of uniform objects.

Strategy
--------
Given parsed JSON, we walk the structure and apply three reversible transforms:

1. Columnar tables. An array of >= 2 objects is collapsed from row-oriented
   ``[{"id":1,"name":"a"}, {"id":2,"name":"b"}]`` into a header-plus-rows node
   ``{<T>:1, "k":["id","name"], "r":[[1,"a"],[2,"b"]]}``. Repeated keys are
   written once instead of once per row — the dominant cost in real payloads.

2. Null / absent handling. In columnar form a JSON ``null`` stays ``null`` (one
   token, key no longer repeated). A key that is *absent* from a given object is
   written as a short absent-sentinel so that "missing" and "explicitly null"
   remain distinguishable on reversal.

3. Long-string elision. A string longer than THRESHOLD chars is replaced by a
   readable prefix plus an elision marker (e.g. ``"Traceback (most rec…<E>0"``).
   The full original is stored in ``metadata["elisions"]`` and restored exactly
   on decompress. The LLM still sees a useful prefix; the codec stays lossless.

Reversibility
-------------
All three transforms are losslessly reversible *given the metadata*. The one
guarantee that makes this robust: we pick a sentinel string S that does not
occur anywhere in the input text, then build every marker as S + suffix. Because
S is absent from the original, no marker can ever collide with real data, so
reversal is unambiguous. S is recorded in the metadata.

"Reversible" here means the *parsed* JSON round-trips (equal data structure),
not that byte-level whitespace/formatting is preserved — reparsing necessarily
normalizes formatting. The round-trip tests assert structural equality.

The transforms are applied recursively, so nested arrays-of-objects also become
nested tables.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from ..result import CompressResult
from ..tokens import estimate_tokens

NAME = "json"
VERSION = 1

# --- tuning knobs -----------------------------------------------------------
# A long string is elided only if it exceeds THRESHOLD chars; we keep the first
# HEAD chars as a human/LLM-readable prefix. Kept conservative so we never elide
# something small enough that the marker would cost more than it saves.
_STR_THRESHOLD = 120
_STR_HEAD = 64
# Minimum array length to bother building a columnar table.
_MIN_TABLE_ROWS = 2


# --- sentinel selection -----------------------------------------------------
def _pick_sentinel(text: str) -> str:
    """Return a short string that is NOT a substring of `text`.

    Deterministic: tries "~~", then "~~0", "~~1", ... The '~' character is rare
    in JSON payloads, so this almost always succeeds on the first try.
    """
    base = "~~"
    if base not in text:
        return base
    i = 0
    while True:
        cand = base + str(i)
        if cand not in text:
            return cand
        i += 1


# --- packing (compress) -----------------------------------------------------
class _Packer:
    def __init__(self, sentinel: str):
        self.s = sentinel
        self.table_key = sentinel + "t"   # marks a columnar-table node
        self.absent = sentinel + "a"      # a row cell whose key was absent
        self.elide_prefix = sentinel + "e"  # elided-string marker prefix
        self.elisions: List[str] = []

    def pack(self, node: Any) -> Any:
        if isinstance(node, list):
            return self._pack_list(node)
        if isinstance(node, dict):
            return {k: self.pack(v) for k, v in node.items()}
        if isinstance(node, str):
            return self._pack_str(node)
        # int / float / bool / None -> unchanged (and reversible as-is)
        return node

    def _pack_str(self, s: str) -> str:
        if len(s) <= _STR_THRESHOLD:
            return s
        idx = len(self.elisions)
        self.elisions.append(s)
        # prefix + marker; marker carries the index into metadata["elisions"]
        return s[:_STR_HEAD] + self.elide_prefix + str(idx)

    def _pack_list(self, items: List[Any]) -> Any:
        if self._is_table(items):
            return self._pack_table(items)
        return [self.pack(x) for x in items]

    @staticmethod
    def _is_table(items: List[Any]) -> bool:
        return (
            len(items) >= _MIN_TABLE_ROWS
            and all(isinstance(x, dict) for x in items)
        )

    def _pack_table(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        # Union of keys in first-seen order (deterministic).
        keys: List[str] = []
        seen = set()
        for obj in rows:
            for k in obj:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        packed_rows: List[List[Any]] = []
        for obj in rows:
            row = []
            for k in keys:
                if k in obj:
                    row.append(self.pack(obj[k]))
                else:
                    row.append(self.absent)
            packed_rows.append(row)
        return {self.table_key: 1, "k": keys, "r": packed_rows}


# --- unpacking (decompress) -------------------------------------------------
class _Unpacker:
    def __init__(self, sentinel: str, elisions: List[str]):
        self.s = sentinel
        self.table_key = sentinel + "t"
        self.absent = sentinel + "a"
        self.elisions = elisions
        # Matches a trailing elision marker: <sentinel>e<index> at end of string.
        self._elide_re = re.compile(re.escape(sentinel + "e") + r"(\d+)$")

    def unpack(self, node: Any) -> Any:
        if isinstance(node, dict):
            if node.get(self.table_key) == 1 and "k" in node and "r" in node:
                return self._unpack_table(node)
            return {k: self.unpack(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self.unpack(x) for x in node]
        if isinstance(node, str):
            return self._unpack_str(node)
        return node

    def _unpack_str(self, s: str) -> str:
        m = self._elide_re.search(s)
        if m:
            idx = int(m.group(1))
            return self.elisions[idx]
        return s

    def _unpack_table(self, node: Dict[str, Any]) -> List[Dict[str, Any]]:
        keys: List[str] = node["k"]
        rows: List[List[Any]] = node["r"]
        out: List[Dict[str, Any]] = []
        for row in rows:
            obj: Dict[str, Any] = {}
            for k, v in zip(keys, row):
                if isinstance(v, str) and v == self.absent:
                    continue  # key was absent in the original object
                obj[k] = self.unpack(v)
            out.append(obj)
        return out


# --- public API -------------------------------------------------------------
def _identity_result(text: str, reason: str) -> CompressResult:
    """Fallback when the input is not compressible JSON: pass through, lossless."""
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
    """Compress a JSON document. Falls back to identity if not valid JSON."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return _identity_result(text, f"not valid JSON: {exc.__class__.__name__}")

    sentinel = _pick_sentinel(text)
    packer = _Packer(sentinel)
    packed = packer.pack(data)
    # Compact separators: no spaces after ',' or ':' — pure token savings.
    compressed = json.dumps(packed, separators=(",", ":"), ensure_ascii=False)

    metadata: Dict[str, Any] = {
        "compressor": NAME,
        "version": VERSION,
        "sentinel": sentinel,
        "elisions": packer.elisions,
    }
    return CompressResult(
        compressed=compressed,
        metadata=metadata,
        original_tokens=estimate_tokens(text),
        compressed_tokens=estimate_tokens(compressed),
        compressor=NAME,
        reversible=True,
    )


def decompress(compressed: str, metadata: Dict[str, Any]) -> str:
    """Reverse `compress`, returning JSON text with equal parsed structure."""
    if metadata.get("compressor") == "identity":
        return compressed
    sentinel: str = metadata["sentinel"]
    elisions: List[str] = metadata.get("elisions", [])
    packed = json.loads(compressed)
    data = _Unpacker(sentinel, elisions).unpack(packed)
    # Re-serialize in a stable, readable form. Formatting (not data) may differ
    # from the original input; that is inherent to any parse/reserialize codec.
    return json.dumps(data, ensure_ascii=False)
