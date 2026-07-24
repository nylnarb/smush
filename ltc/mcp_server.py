"""MCP server exposing ltc's compress/decompress as explicitly-invoked tools.

Transport: stdio only (no network port). This is the *explicit-invocation*
transport — an agent calls `compress`/`decompress`/`stats` deliberately, on
its own judgment, the same way it would call any other tool. It is NOT an
auto-proxy: nothing here intercepts payloads automatically before they reach
a model. A future Layer-3 auto-proxy would sit elsewhere and call
`ltc.compress_payload` directly without going through this server.

Requires the optional `mcp` dependency (`pip install ltc[mcp]` / `pip install
mcp`). The core `ltc` package itself stays stdlib-only; only this module
imports `mcp`.
"""

from __future__ import annotations

from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from . import compress_payload, decompress as _decompress, SUPPORTED_TYPES

mcp = FastMCP("ltc")


@mcp.tool()
def compress(content: str, content_type: str = "auto") -> Dict[str, Any]:
    """Compress a text payload before it enters an LLM context window.

    Args:
        content: Raw text to compress (JSON, log/traceback, file listing, etc).
        content_type: One of "auto", "json", "log", "listing". "auto" sniffs
            the content type; pass an explicit type if you already know it.

    Returns:
        A dict with the compressed text plus the metadata needed to reverse
        it (`decompress`), and token counts / reduction stats.
    """
    result = compress_payload(content, content_type)
    return result.as_dict()


@mcp.tool()
def decompress(compressed: str, metadata: Dict[str, Any]) -> str:
    """Reverse a previous `compress` call using the metadata it returned.

    Args:
        compressed: The `compressed` string from a prior `compress` call.
        metadata: The `metadata` dict from that same `compress` call.

    Returns:
        The original, uncompressed text.
    """
    return _decompress(compressed, metadata)


@mcp.tool()
def stats(content: str, content_type: str = "auto") -> Dict[str, Any]:
    """Report compression stats for a payload without needing the full text back.

    Useful for deciding whether compression is worth applying, or for
    building the kind of before/after report used in the measurement harness.

    Args:
        content: Raw text to analyze.
        content_type: One of "auto", "json", "log", "listing".

    Returns:
        A dict with original_tokens, compressed_tokens, tokens_saved,
        reduction, compressor, and reversible — but not the compressed text
        itself.
    """
    result = compress_payload(content, content_type)
    return {
        "compressor": result.compressor,
        "original_tokens": result.original_tokens,
        "compressed_tokens": result.compressed_tokens,
        "tokens_saved": result.tokens_saved,
        "reduction": round(result.reduction, 4),
        "reversible": result.reversible,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
