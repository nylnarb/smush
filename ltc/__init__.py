"""ltc — local token compressor.

A dependency-free (stdlib-only) toolkit for shrinking large text blobs before
they enter an LLM context window.

Design contract (do not break):
  * The core is transport-agnostic. Nothing in this package may import or know
    about MCP, HTTP, sockets, or any specific caller. Layer 2 (MCP) and any
    future Layer 3 (proxy) sit *on top* of this package and call the single
    entry point `compress_payload`.
  * No network access, ever. No file writes outside an explicit caller request.
  * No global mutable state. Every public function is pure: same input -> same
    output, no side effects.
  * A compressor may discard content, but never silently. If `decompress` will
    not reproduce the input, the result must carry `reversible=False` and name
    what it dropped in `CompressResult.dropped`. The json/log/listing codecs
    are exactly reversible; the pytest/git codecs are not, by design, and the
    listing codec becomes lossy only on `ls -l` column elision.
"""

from .result import CompressResult
from .core import compress_payload, decompress, SUPPORTED_TYPES

__all__ = [
    "CompressResult",
    "compress_payload",
    "decompress",
    "SUPPORTED_TYPES",
]

__version__ = "0.1.0"
