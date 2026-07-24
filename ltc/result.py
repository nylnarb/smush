"""The CompressResult value object returned by every compressor.

CompressResult is a plain, JSON-serializable data carrier. It intentionally
knows nothing about how it will be transported (CLI, MCP, a future proxy). The
`metadata` field carries everything decompression needs to reverse the
transform, so a CompressResult is fully self-describing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List


@dataclass(frozen=True)
class CompressResult:
    """Result of compressing one payload.

    Attributes:
        compressed:        The compressed text that would go into the LLM.
        metadata:          Everything needed to reverse the transform. Always
                           JSON-serializable. Must contain at least
                           ``{"compressor": <name>, "version": <int>}`` so that
                           `decompress` is self-contained.
        original_tokens:   Estimated token count of the input (heuristic; see
                           ltc.tokens). Not an exact tokenizer count.
        compressed_tokens: Estimated token count of `compressed`.
        compressor:        Name of the compressor that produced this result
                           (e.g. "json", "identity").
        reversible:        True if `decompress(compressed, metadata)` reproduces
                           the original payload (semantically — for JSON that
                           means an equal parsed structure, not byte-identical
                           formatting). False means content was discarded and is
                           NOT recoverable from `compressed` + `metadata`.
        dropped:           Human-readable descriptions of what a lossy transform
                           threw away, e.g. "41 passing test names". Empty for
                           reversible results. This is the whole safety
                           mechanism for lossy codecs: a lossy result must never
                           be silent about being lossy, so a reader can always
                           tell what is missing and re-run the command to get it.
    """

    compressed: str
    metadata: Dict[str, Any]
    original_tokens: int
    compressed_tokens: int
    compressor: str
    reversible: bool = True
    dropped: List[str] = field(default_factory=list)

    @property
    def lossy(self) -> bool:
        """True if this result discarded content. The inverse of `reversible`.

        Named separately because callers ask the question both ways, and
        ``if not result.reversible`` reads as a double negative at the call
        sites that matter most (the lossy footer, the harness's round-trip
        gate).
        """
        return not self.reversible

    @property
    def tokens_saved(self) -> int:
        return max(0, self.original_tokens - self.compressed_tokens)

    @property
    def reduction(self) -> float:
        """Fraction of tokens removed, in [0.0, 1.0]. 0.0 if input was empty."""
        if self.original_tokens <= 0:
            return 0.0
        return self.tokens_saved / self.original_tokens

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-serializable dict of the whole result (for MCP/CLI output)."""
        d = asdict(self)
        d["reduction"] = round(self.reduction, 4)
        d["tokens_saved"] = self.tokens_saved
        d["lossy"] = self.lossy
        return d
