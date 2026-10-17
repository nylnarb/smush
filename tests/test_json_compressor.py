"""Tests for the JSON compressor.

Run: python -m unittest discover -s tests    (from the repo root)
No third-party test dependency — stdlib unittest only.
"""

import json
import unittest

from smush import compress_payload, decompress
from smush.compressors import json_compressor as jc


def roundtrip(text: str):
    """Compress then decompress via the public entry points."""
    res = compress_payload(text, "json")
    back = decompress(res.compressed, res.metadata)
    return res, back


def assert_json_equal(test: unittest.TestCase, original: str, restored: str):
    """Round-trip is defined as structural (parsed) equality, not byte equality."""
    test.assertEqual(json.loads(original), json.loads(restored))


# --- realistic fixtures -----------------------------------------------------

USERS = json.dumps([
    {"id": 1, "name": "Alice", "email": "alice@example.com", "active": True},
    {"id": 2, "name": "Bob", "email": "bob@example.com", "active": False},
    {"id": 3, "name": "Carol", "email": "carol@example.com", "active": True},
    {"id": 4, "name": "Dave", "email": "dave@example.com", "active": True},
])

# Ragged: varying keys, explicit nulls, and a null-everywhere column.
RAGGED = json.dumps([
    {"id": 1, "name": "Alice", "note": None, "middle": None},
    {"id": 2, "name": "Bob", "extra": "x", "middle": None},
    {"id": 3, "name": "Carol", "middle": None},
])

LONG = "Traceback (most recent call last):\n" + ("  frame line here\n" * 40)
WITH_LONG_STRINGS = json.dumps([
    {"id": 1, "log": LONG, "level": "error"},
    {"id": 2, "log": "short", "level": "info"},
])

NESTED = json.dumps({
    "team": "platform",
    "members": [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"},
    ],
    "meta": {"count": 2},
})


class TestRoundTrip(unittest.TestCase):
    def test_uniform_array(self):
        res, back = roundtrip(USERS)
        assert_json_equal(self, USERS, back)
        self.assertEqual(res.compressor, "json")
        self.assertTrue(res.reversible)

    def test_ragged_array_preserves_missing_vs_null(self):
        res, back = roundtrip(RAGGED)
        original = json.loads(RAGGED)
        restored = json.loads(back)
        self.assertEqual(original, restored)
        # object 0 has no "extra" key; object 1 does -> must stay distinct
        self.assertNotIn("extra", restored[0])
        self.assertIn("extra", restored[1])
        # explicit null must survive as null, not become "absent"
        self.assertIsNone(restored[0]["note"])

    def test_long_strings_restore_exactly(self):
        res, back = roundtrip(WITH_LONG_STRINGS)
        restored = json.loads(back)
        self.assertEqual(restored[0]["log"], LONG)  # exact, lossless
        self.assertGreaterEqual(len(res.metadata["elisions"]), 1)

    def test_nested_array_of_objects(self):
        res, back = roundtrip(NESTED)
        assert_json_equal(self, NESTED, back)

    def test_scalar_and_empty_inputs(self):
        for text in ['"just a string"', "42", "true", "null", "{}", "[]", "[1,2,3]"]:
            res, back = roundtrip(text)
            assert_json_equal(self, text, back)


class TestCompression(unittest.TestCase):
    def test_uniform_array_actually_shrinks(self):
        res = compress_payload(USERS, "json")
        self.assertLess(res.compressed_tokens, res.original_tokens)
        self.assertGreater(res.reduction, 0.0)

    def test_keys_written_once(self):
        res = compress_payload(USERS, "json")
        # "email" appears 4x in source rows, once in the columnar header.
        self.assertEqual(res.compressed.count("email"), 1)

    def test_long_string_prefix_visible_in_output(self):
        res = compress_payload(WITH_LONG_STRINGS, "json")
        # A readable prefix of the elided string remains in the compressed text.
        self.assertIn("Traceback (most recent", res.compressed)
        # ...but the full 40-line body does not.
        self.assertNotIn("frame line here\n  frame line here", res.compressed)


class TestDeterminism(unittest.TestCase):
    def test_repeatable(self):
        a = compress_payload(USERS, "json")
        b = compress_payload(USERS, "json")
        self.assertEqual(a.compressed, b.compressed)
        self.assertEqual(a.metadata, b.metadata)


class TestRobustness(unittest.TestCase):
    def test_invalid_json_falls_back_to_identity(self):
        text = "this is not json { , ]"
        res = compress_payload(text, "json")
        self.assertEqual(res.compressor, "identity")
        self.assertEqual(res.compressed, text)
        self.assertEqual(decompress(res.compressed, res.metadata), text)

    def test_sentinel_collision_is_avoided(self):
        # Input already contains the default "~~" sentinel; packer must pick
        # a different one and still round-trip.
        payload = json.dumps([
            {"id": 1, "tag": "~~"},
            {"id": 2, "tag": "~~0 value"},
        ])
        res, back = roundtrip(payload)
        assert_json_equal(self, payload, back)
        self.assertNotEqual(res.metadata["sentinel"], "")

    def test_auto_sniff_detects_json(self):
        res = compress_payload(USERS, "auto")
        self.assertEqual(res.compressor, "json")

    def test_string_containing_marker_lookalike(self):
        # A real value that looks like an elision marker must not be corrupted.
        payload = json.dumps([{"id": 1, "v": "~~e0"}, {"id": 2, "v": "ok"}])
        res, back = roundtrip(payload)
        assert_json_equal(self, payload, back)


class TestSentinelPicker(unittest.TestCase):
    def test_returns_absent_substring(self):
        text = "~~ ~~0 ~~1 payload"
        s = jc._pick_sentinel(text)
        self.assertNotIn(s, text)


if __name__ == "__main__":
    unittest.main()
