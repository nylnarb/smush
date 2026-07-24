"""Compressor implementations.

Each compressor module exposes two pure functions:

    compress(text: str) -> CompressResult
    decompress(compressed: str, metadata: dict) -> str

and nothing else the core relies on. Compressors never do I/O and never touch
global state. The core registry in ltc.core wires them to content types.
"""
