#!/usr/bin/env python3

import unittest

# Import rename_by_voice.py from the parent dir for in-situ testing
# From https://codeolives.com/2020/01/10/python-reference-module-in-parent-directory/
import sys
import os
import inspect
currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.append(parentdir)
import rename_by_voice


class Test_grok_year(unittest.TestCase):

    def check_impl(self, expected_year, word_str, expected_rem=""):
        """Check that the given string word_str decodes to the given expected_year,
        with the given remaining words joined into a string passed in as expected_rem.
        """
        word_list = word_str.split()
        got_year = rename_by_voice.grok_year(word_list)
        got_rem = " ".join(word_list)
        self.assertEqual(got_year, expected_year)
        self.assertEqual(got_rem, expected_rem)

    def check(self, expected_year, word_str):
        self.check_impl(expected_year, word_str)
        self.check_impl(expected_year, word_str + " with stuff", "with stuff")

    def test_1900(self):
        self.check(1900, "one thousand nine hundred")
        self.check(1900, "nineteen hundred")
        self.check(1900, "nineteen oh oh")

    def test_2000(self):
        self.check(2000, "two thousand")
        self.check(2000, "twenty oh oh")

    def test_2001(self):
        self.check(2001, "two thousand one")
        self.check(2001, "two thousand and one")
        self.check(2001, "twenty oh one")

    def test_2009(self):
        self.check(2009, "two thousand nine")
        self.check(2009, "two thousand and nine")
        self.check(2009, "twenty oh nine")

    def test_2010(self):
        self.check(2010, "two thousand ten")
        self.check(2010, "two thousand and ten")
        self.check(2010, "twenty ten")

    def test_2011(self):
        self.check(2011, "two thousand eleven")
        self.check(2011, "two thousand and eleven")
        self.check(2011, "twenty eleven")
        self.check(2011, "twenty hundred eleven")

    def test_2019(self):
        self.check(2019, "two thousand nineteen")
        self.check(2019, "two thousand and nineteen")
        self.check(2019, "twenty nineteen")

    def test_2020(self):
        self.check(2020, "two thousand twenty")
        self.check(2020, "two thousand and twenty")
        self.check(2020, "twenty twenty")

    def test_2021(self):
        # Sometimes PocketSphinx mishears "one" as "why"
        self.check(2021, "two thousand twenty why")
        self.check(2021, "two thousand and twenty one")
        self.check(2021, "twenty twenty one")

    def test_2022(self):
        self.check(2022, "two thousand twenty two")
        self.check(2022, "two thousand and twenty two")
        self.check(2022, "twenty twenty two")

    def test_2029(self):
        self.check(2029, "two thousand twenty nine")
        self.check(2029, "two thousand and twenty nine")
        self.check(2029, "twenty twenty nine")

    def test_2100(self):
        self.check(2100, "two thousand one hundred")
        self.check(2100, "two thousand and one hundred")
        self.check(2100, "twenty one hundred")
        self.check(2100, "twenty one oh oh")

    def test_2101(self):
        self.check(2101, "two thousand one hundred one")
        self.check(2101, "two thousand and one hundred and one")
        self.check(2101, "twenty one hundred one")
        self.check(2101, "twenty one hundred and one")
        self.check(2101, "twenty one oh one")

    def test_2119(self):
        self.check(2119, "two thousand one hundred nineteen")
        self.check(2119, "two thousand and one hundred and nineteen")
        self.check(2119, "twenty one hundred nineteen")
        self.check(2119, "twenty one hundred and nineteen")
        self.check(2119, "twenty one nineteen")

    def test_2120(self):
        self.check(2120, "two thousand one hundred twenty")
        self.check(2120, "two thousand and one hundred and twenty")
        self.check(2120, "twenty one hundred twenty")
        self.check(2120, "twenty one hundred and twenty")
        self.check(2120, "twenty one twenty")

    def test_2121(self):
        self.check(2121, "two thousand one hundred twenty one")
        self.check(2121, "two thousand and one hundred and twenty one")
        self.check(2121, "twenty one hundred twenty one")
        self.check(2121, "twenty one hundred and twenty one")
        self.check(2121, "twenty one twenty one")

    def test_2129(self):
        self.check(2129, "two thousand one hundred twenty nine")
        self.check(2129, "two thousand and one hundred and twenty nine")
        self.check(2129, "twenty one twenty nine")


if __name__ == '__main__':
    unittest.main()
