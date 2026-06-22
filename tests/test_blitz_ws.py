"""Tests du flux Blitzortung WS : décodage LZW + parsing d'impacts."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.blitz_ws import decode, parse_message, parse_strike  # noqa: E402
from blitz.config import HomeConfig  # noqa: E402


def lzw_compress(s: str) -> str:
    """Compression LZW standard (réciproque de blitz_ws.decode), pour les tests."""
    table = {chr(i): i for i in range(256)}
    size = 256
    w = ""
    out: list[int] = []
    for ch in s:
        wc = w + ch
        if wc in table:
            w = wc
        else:
            out.append(table[w])
            table[wc] = size
            size += 1
            w = ch
    if w:
        out.append(table[w])
    return "".join(chr(c) for c in out)


class DecodeTests(unittest.TestCase):
    def test_ascii_identity(self):
        # Sans code >= 256, le décodage rend la chaîne inchangée (chemin littéral).
        s = '{"lat":44.2,"lon":4.7}'
        self.assertEqual(decode(s), s)

    def test_roundtrip(self):
        for s in ['{"time":1700000000000000000,"lat":44.2,"lon":4.7,"sig":[1,2,3]}',
                  "ABABABABABA", "tobeornottobeortobeornot", "aaaaaaaaaa"]:
            self.assertEqual(decode(lzw_compress(s)), s)


class ParseTests(unittest.TestCase):
    def test_parse_message_plain_json(self):
        self.assertEqual(parse_message('{"a":1}'), {"a": 1})

    def test_parse_message_compressed(self):
        payload = json.dumps({"lat": 44.2, "lon": 4.7, "time": 1700000000000000000})
        self.assertEqual(parse_message(lzw_compress(payload)), json.loads(payload))

    def test_parse_message_garbage(self):
        self.assertIsNone(parse_message("\x00\x01 not json"))

    def test_parse_strike_fields(self):
        home = HomeConfig(lat=44.243318, lon=4.716102)
        now = 1700000010.0
        data = {"lat": 44.25, "lon": 4.72, "time": 1700000000_000000000, "sig": [1, 2, 3, 4], "delay": 2.5}
        p = parse_strike(data, home, now)
        self.assertEqual(p["mds"], 4)                 # len(sig)
        self.assertAlmostEqual(p["delay"], 2.5)
        self.assertAlmostEqual(p["ts"], 1700000000.0)
        self.assertAlmostEqual(p["latency"], 10.0, delta=0.01)
        self.assertLess(p["dist"], 5.0)               # ~1 km de HOME

    def test_parse_strike_missing_coords(self):
        self.assertIsNone(parse_strike({"time": 1}, HomeConfig(), 1.0))


if __name__ == "__main__":
    unittest.main()
