#!/usr/bin/env python3
"""Unit tests for AlbumMatcher — the fuzzy Last.fm ↔ collection matching.

Run:  python3 tests/test_matcher.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from update_lastplayed import AlbumMatcher, _title_keys  # noqa: E402


def make_matcher():
    albums = [
        {"artist": "Feeder", "title": "Swim (blue)"},
        {"artist": "Feeder", "title": "Swim (red)"},
        {"artist": "Green Day", "title": "Dookie (green case)"},
        {"artist": "Green Day", "title": "Nimrod (Australia)"},
        {"artist": "Aphex Twin", "title": "Cheetah"},
        {"artist": "Lemonheads", "title": "Come on Feel"},
        {"artist": "Crackout", "title": "Volume"},
        {"artist": "RipOffs", "title": "Ripped Off"},
        {"artist": "Angine de Poitrine", "title": "Vol.2"},
        {"artist": "Angine de Poitrine", "title": "Vol.1"},
        {"artist": "Balkan Beat Box", "title": "Nu*Med"},
        {"artist": "Battles", "title": "Juice B Crypt"},
        {"artist": "Meat Wave", "title": "Align Hex"},
        {"artist": "Culture Abuse", "title": "Dreams of Nothing"},
        {"artist": "We Are the Physics", "title": "Are Okay at Music"},
        {"artist": "Bichkraft", "title": "Shadoof/ Шадуф"},
        {"artist": "Marnie Stern", "title": "This is It and I am It and...."},
        {"artist": "CoryaYo", "title": "Rated R"},
        {"artist": "The Beatles", "title": "Revolver"},
    ]
    return AlbumMatcher(albums)


class TestAlbumMatcher(unittest.TestCase):
    def setUp(self):
        self.m = make_matcher()

    def assertMatches(self, artist, album, expected_title_fragment):
        result = self.m.match(artist, album)
        self.assertIsNotNone(result, f"{artist} — {album!r} did not match")
        self.assertIn(expected_title_fragment, result)

    def test_exact(self):
        self.assertMatches("The Beatles", "Revolver", "revolver")
        self.assertMatches("Beatles", "Revolver", "revolver")  # article stripped

    def test_pressing_variants(self):
        # Last.fm plain title matches a "(blue)" pressing in the collection
        self.assertMatches("Feeder", "Swim", "swim")
        self.assertMatches("Green Day", "Dookie", "dookie")
        self.assertMatches("Green Day", "Nimrod.", "nimrod")

    def test_ep_and_promo_suffixes(self):
        self.assertMatches("Aphex Twin", "Cheetah EP", "cheetah")
        self.assertMatches("Crackout", "Volume Promo", "volume")

    def test_prefix_titles(self):
        self.assertMatches("Lemonheads", "Come On Feel The Lemonheads", "come on feel")
        self.assertMatches("The Lemonheads", "come on feel the lemonheads", "come on feel")
        self.assertMatches("RipOffs", "Ripped Off (Live at KBC 2004)", "ripped off")
        self.assertMatches("Marnie Stern",
                           "This Is It and I Am It and You Are It and So Is That",
                           "this is it")

    def test_suffix_titles(self):
        self.assertMatches("Culture Abuse", "Day Dreams of Nothing", "dreams of nothing")

    def test_roman_numerals(self):
        self.assertMatches("Angine de Poitrine", "vol.II", "vol2")
        self.assertMatches("Angine de Poitrine", "Vol II", "vol2")
        self.assertMatches("Angine de Poitrine", "Vol.1", "vol1")

    def test_punctuation_as_space(self):
        self.assertMatches("Balkan Beat Box", "nu med", "numed")

    def test_okay_ok(self):
        self.assertMatches("We Are The Physics", "We Are The Physics Are OK At Music",
                           "are okay at music")

    def test_ascii_fallback(self):
        # Mojibake on the Last.fm side still matches via the ASCII-only variant
        self.assertMatches("Bichkraft", "Shadoof (whatever)", "shadoof")

    def test_typo_tolerance(self):
        self.assertMatches("Battles", "Juice B Crypts", "juice b crypt")
        self.assertMatches("Meat Wave", "Malign Hex", "align hex")

    def test_no_false_positives(self):
        # "RA" must not match "Rated R"
        self.assertIsNone(self.m.match("CoryaYo", "RA"))
        # Unknown artist never matches
        self.assertIsNone(self.m.match("Some Band", "Swim"))
        # Different album by a known artist doesn't get pulled in
        self.assertIsNone(self.m.match("The Beatles", "Abbey Road"))

    def test_title_keys_roman_expansion(self):
        keys = _title_keys("Vol.2")
        self.assertIn("vol2", keys)
        self.assertIn("volii", keys)
        self.assertIn("vol ii", keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
