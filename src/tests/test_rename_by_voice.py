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

class check_word_list_grok(unittest.TestCase):
    """Base class for classes that test specific grok functions.

    The derived classes contain the actual test cases.
    self.grok_fn should be set to a function that takes a word_list and returns a value.
    """

    def check_impl(self, word_str, expected_rem=""):
        """Checks that the given string word_str decodes to self.expected_value,
        with the given remaining words joined into a string passed in as expected_rem.
        """
        word_list = word_str.split()
        got_value = self.grok_fn(word_list)
        got_rem = " ".join(word_list)
        self.assertEqual(got_value, self.expected_value)
        self.assertEqual(got_rem, expected_rem)

    def check(self, word_str):
        self.check_impl(word_str)
        self.check_impl(word_str + " with stuff", "with stuff")


class Test_grok_digit_pair(check_word_list_grok):
    def grok_fn(self, word_list):
        return rename_by_voice.grok_digit_pair(word_list)

    def test_0(self):
        self.expected_value = 0
        self.check("")
        self.check("zero")
        self.check("oh")
        self.check("oh oh")

    def test_1(self):
        self.expected_value = 1
        self.check("one")
        self.check("oh one")
        self.check("zero one")

    def test_9(self):
        self.expected_value = 9
        self.check("nine")
        self.check("oh nine")
        self.check("zero nine")

    def test_10(self):
        self.expected_value = 10
        self.check("ten")

    def test_19(self):
        self.expected_value = 19
        self.check("nineteen")

    def test_20(self):
        self.expected_value = 20
        self.check("twenty")

    def test_21(self):
        self.expected_value = 21
        self.check("twenty one")

    def test_59(self):
        self.expected_value = 59
        self.check("fifty nine")


class Test_grok_time_words(check_word_list_grok):
    def grok_fn(self, word_list):
        hour, minute, second, rest = rename_by_voice.grok_time_words(word_list)
        self.assertEqual(word_list, rest)
        return f"{hour} {minute} {second}"

    def test_0_0_0(self):
        self.expected_value = "0 0 0"
        self.check("zero minutes")
        self.check("zero minutes and zero seconds")
        self.check("zero seconds")
        self.check("zero hours and zero seconds")
        self.check("zero hundred")
        self.check("zero hundred hours")
        self.check("zero hundred oh oh")
        self.check("oh oh oh oh oh oh")
        self.check("zero hours zero minutes and zero seconds")
        self.check("zero hours and zero minutes zero seconds")
        self.check("zero hours and zero minutes oh oh")

    # ones
    def test_0_0_1(self):
        self.expected_value = "0 0 1"
        self.check("zero hundred and one second")
        self.check("oh oh oh oh oh one")
        self.check("zero hours zero minutes and one second")
        self.check("zero hours and zero minutes oh one")

    def test_0_1_0(self):
        self.expected_value = "0 1 0"
        self.check("zero hundred oh one")
        self.check("zero hundred hours oh one")
        self.check("one minute")
        self.check("one minute and")
        self.check("one minute and zero seconds")
        self.check("one minute oh oh")
        self.check("oh oh one minute oh oh")
        self.check("oh oh oh one oh oh")
        self.check("zero zero oh one oh zero")

    def test_0_1_1(self):
        self.expected_value = "0 1 1"
        self.check("zero hundred oh one oh one")
        self.check("zero hundred hours oh one and one second")
        self.check("one minute one second")
        self.check("one minute and one second")
        self.check("one minute oh one")
        self.check("oh oh one minute zero one")
        self.check("oh oh oh one oh one")

    def test_1_0_0(self):
        self.expected_value = "1 0 0"
        self.check("one hour zero minutes")
        self.check("one hour zero minutes and zero seconds")
        self.check("one hour zero seconds")
        self.check("one hour and zero seconds")
        self.check("one hundred")
        self.check("one hundred hours")
        self.check("one oh clock")
        self.check("one o'clock")
        self.check("one hundred oh oh")
        self.check("oh one oh oh oh oh")
        self.check("one hour and zero minutes zero seconds")
        self.check("one hour and zero minutes oh oh")

    def test_1_0_1(self):
        self.expected_value = "1 0 1"
        self.check("one hour zero minutes and one second")
        self.check("one hour one second")
        self.check("one hour and one second")
        self.check("one hundred hours and one second")
        self.check("oh one oh oh oh one")
        self.check("one hour and zero minutes one second")
        self.check("one hour and zero minutes oh one")

    def test_1_1_0(self):
        self.expected_value = "1 1 0"
        self.check("one hundred hours oh one")
        self.check("one oh clock oh one")
        self.check("one o'clock oh one")
        self.check("one o'clock oh one and zero seconds")
        self.check("one hour one minute and zero seconds")
        self.check("one hour one minute")
        self.check("one hundred hours and one minute")
        self.check("oh one oh one oh oh")
        self.check("one hour and one minute zero seconds")
        self.check("one hour and one minute oh oh")

    def test_1_1_1(self):
        self.expected_value = "1 1 1"
        self.check("one hundred hours oh one oh one")
        self.check("one oh clock oh one and one second")
        self.check("one o'clock oh one oh one")
        self.check("one o'clock oh one and one second")
        self.check("one hour one minute and one second")
        self.check("one hour one minute one second")
        self.check("one hundred hours and one minute and one second")
        self.check("oh one oh one oh one")
        self.check("one hour and one minute one second")
        self.check("one hour and one minute oh one")

    # nineteens
    def test_0_0_19(self):
        self.expected_value = "0 0 19"
        self.check("zero hundred and nineteen seconds")
        self.check("oh oh oh oh nineteen")
        self.check("zero hours zero minutes and nineteen seconds")
        self.check("zero hours and zero minutes nineteen")

    def test_0_19_0(self):
        self.expected_value = "0 19 0"
        self.check("zero hundred nineteen")
        self.check("zero hundred hours nineteen")
        self.check("nineteen minutes")
        self.check("nineteen minutes and")
        self.check("nineteen minutes and zero seconds")
        self.check("nineteen minutes oh oh")
        self.check("oh oh nineteen minutes oh oh")
        self.check("oh oh nineteen oh oh")
        self.check("zero zero nineteen oh zero")
        self.check("zero nineteen oh zero")

    def test_0_19_19(self):
        self.expected_value = "0 19 19"
        self.check("zero hundred nineteen nineteen")
        self.check("zero hundred hours nineteen and nineteen seconds")
        self.check("nineteen minutes nineteen seconds")
        self.check("nineteen minutes and nineteen seconds")
        self.check("nineteen minutes nineteen")
        self.check("oh oh nineteen minutes nineteen")
        self.check("oh oh nineteen nineteen")

    def test_19_0_0(self):
        self.expected_value = "19 0 0"
        self.check("nineteen hour zero minutes")
        self.check("nineteen hour zero minutes and zero seconds")
        self.check("nineteen hour zero seconds")
        self.check("nineteen hour and zero seconds")
        self.check("nineteen hundred")
        self.check("nineteen hundred hours")
        self.check("nineteen oh clock")
        self.check("nineteen o'clock")
        self.check("nineteen hundred oh oh")
        self.check("nineteen oh oh oh oh")
        self.check("nineteen hour and zero minutes zero seconds")
        self.check("nineteen hour and zero minutes oh oh")

    def test_19_0_19(self):
        self.expected_value = "19 0 19"
        self.check("nineteen hour zero minutes and nineteen seconds")
        self.check("nineteen hour nineteen seconds")
        self.check("nineteen hour and nineteen seconds")
        self.check("nineteen hundred hours and nineteen seconds")
        self.check("nineteen oh oh nineteen")
        self.check("nineteen hour and zero minutes nineteen seconds")
        self.check("nineteen hour and zero minutes nineteen")

    def test_19_19_0(self):
        self.expected_value = "19 19 0"
        self.check("nineteen hundred hours nineteen")
        self.check("nineteen oh clock nineteen")
        self.check("nineteen o'clock nineteen")
        self.check("nineteen o'clock nineteen and zero seconds")
        self.check("nineteen hour nineteen minutes and zero seconds")
        self.check("nineteen hour nineteen minutes")
        self.check("nineteen hundred hours and nineteen minutes")
        self.check("nineteen nineteen oh oh")
        self.check("nineteen hour and nineteen minutes zero seconds")
        self.check("nineteen hour and nineteen minutes oh oh")

    def test_19_19_19(self):
        self.expected_value = "19 19 19"
        self.check("nineteen hundred hours nineteen nineteen")
        self.check("nineteen oh clock nineteen and nineteen seconds")
        self.check("nineteen o'clock nineteen nineteen")
        self.check("nineteen o'clock nineteen and nineteen seconds")
        self.check("nineteen hour nineteen minutes and nineteen seconds")
        self.check("nineteen hour nineteen minutes nineteen seconds")
        self.check("nineteen hundred hours and nineteen minutes and nineteen seconds")
        self.check("nineteen nineteen nineteen")
        self.check("nineteen hour and nineteen minutes nineteen seconds")
        self.check("nineteen hour and nineteen minutes nineteen")

    # twenty threes
    def test_0_0_23(self):
        self.expected_value = "0 0 23"
        self.check("zero hundred and twenty three seconds")
        self.check("oh oh oh oh twenty three")
        self.check("zero hours zero minutes and twenty three seconds")
        self.check("zero hours and zero minutes twenty three")

    def test_0_23_0(self):
        self.expected_value = "0 23 0"
        self.check("zero hundred twenty three")
        self.check("zero hundred hours twenty three")
        self.check("zero hundred hours and twenty three")
        self.check("twenty three minutes")
        self.check("twenty three minutes and")
        self.check("twenty three minutes and zero seconds")
        self.check("twenty three minutes oh oh")
        self.check("oh oh twenty three minutes oh oh")
        self.check("oh oh twenty three oh oh")
        self.check("zero zero twenty three oh zero")
        self.check("zero twenty three oh zero")
        self.check("zero twenty three")

    def test_0_23_23(self):
        self.expected_value = "0 23 23"
        self.check("zero hundred twenty three twenty three")
        self.check("zero hundred hours twenty three and twenty three seconds")
        self.check("twenty three minutes twenty three seconds")
        self.check("twenty three minutes and twenty three seconds")
        self.check("twenty three minutes twenty three")
        self.check("oh oh twenty three minutes twenty three")
        self.check("oh oh twenty three twenty three")

    def test_23_0_0(self):
        self.expected_value = "23 0 0"
        self.check("twenty three hour zero minutes")
        self.check("twenty three hour zero minutes and zero seconds")
        self.check("twenty three hour zero seconds")
        self.check("twenty three hour and zero seconds")
        self.check("twenty three hundred")
        self.check("twenty three hundred hours")
        self.check("twenty three oh clock")
        self.check("twenty three o'clock")
        self.check("twenty three hundred oh oh")
        self.check("twenty three oh oh oh oh")
        self.check("twenty three hour and zero minutes zero seconds")
        self.check("twenty three hour and zero minutes oh oh")

    def test_23_0_23(self):
        self.expected_value = "23 0 23"
        self.check("twenty three hour zero minutes and twenty three seconds")
        self.check("twenty three hour twenty three seconds")
        self.check("twenty three hour and twenty three seconds")
        self.check("twenty three hundred hours and twenty three seconds")
        self.check("twenty three oh oh twenty three")
        self.check("twenty three hour and zero minutes twenty three seconds")
        self.check("twenty three hour and zero minutes twenty three")

    def test_23_23_0(self):
        self.expected_value = "23 23 0"
        self.check("twenty three hundred hours twenty three")
        self.check("twenty three oh clock twenty three")
        self.check("twenty three o'clock twenty three")
        self.check("twenty three o'clock twenty three and zero seconds")
        self.check("twenty three hour twenty three minutes and zero seconds")
        self.check("twenty three hour twenty three minutes")
        self.check("twenty three hundred hours and twenty three minutes")
        self.check("twenty three twenty three oh oh")
        self.check("twenty three hour and twenty three minutes zero seconds")
        self.check("twenty three hour and twenty three minutes oh oh")

    def test_23_23_23(self):
        self.expected_value = "23 23 23"
        self.check("twenty three hundred hours twenty three twenty three")
        self.check("twenty three oh clock twenty three and twenty three seconds")
        self.check("twenty three o'clock twenty three twenty three")
        self.check("twenty three o'clock twenty three and twenty three seconds")
        self.check("twenty three hour twenty three minutes and twenty three seconds")
        self.check("twenty three hour twenty three minutes twenty three seconds")
        self.check("twenty three hundred hours and twenty three minutes and twenty three seconds")
        self.check("twenty three twenty three twenty three")
        self.check("twenty three hour and twenty three minutes twenty three seconds")
        self.check("twenty three hour and twenty three minutes twenty three")

    # twenty threes and ones
    def test_0_1_23(self):
        self.expected_value = "0 1 23"
        self.check("zero hundred one minute and twenty three seconds")
        self.check("oh oh oh one twenty three")
        self.check("zero hours one minute and twenty three seconds")
        self.check("zero hundred hours one minute and twenty three seconds")
        self.check("zero hours and one minute twenty three")

    def test_1_0_23(self):
        self.expected_value = "1 0 23"
        self.check("one hundred zero minutes and twenty three seconds")
        self.check("oh one oh oh twenty three")
        self.check("one hour zero minutes and twenty three seconds")
        self.check("one hundred hours zero minutes and twenty three seconds")
        self.check("one hour and zero minutes twenty three")

    def test_1_1_23(self):
        self.expected_value = "1 1 23"
        self.check("one hundred one minute and twenty three seconds")
        self.check("oh one oh one twenty three")
        self.check("one hour one minute and twenty three seconds")
        self.check("one hundred hours one minute and twenty three seconds")
        self.check("one hour and one minute twenty three")

    def test_0_23_1(self):
        self.expected_value = "0 23 1"
        self.check("zero hundred twenty three oh one")
        self.check("zero hundred hours twenty three zero one")
        self.check("zero hundred hours and twenty three and one")
        self.check("twenty three minutes and one second")
        self.check("twenty three minutes oh one")
        self.check("oh oh twenty three minutes oh one")
        self.check("oh oh twenty three oh one")
        self.check("zero zero twenty three oh one")
        self.check("zero twenty three oh one")

    def test_1_23_0(self):
        self.expected_value = "1 23 0"
        self.check("one hundred twenty three")
        self.check("one hundred hours twenty three")
        self.check("one hundred hours and twenty three")
        self.check("one hour twenty three minutes")
        self.check("one hundred twenty three minutes and")
        self.check("one hour twenty three minutes and zero seconds")
        self.check("one hour twenty three minutes oh oh")
        self.check("oh one twenty three minutes oh oh")
        self.check("oh one twenty three oh oh")
        self.check("zero one twenty three oh zero")
        self.check("one twenty three oh zero")
        self.check("one twenty three")
        self.check("one twenty three and zero seconds")

    def test_1_23_1(self):
        self.expected_value = "1 23 1"
        self.check("one hundred twenty three oh one")
        self.check("one hundred hours twenty three oh one")
        self.check("one hundred hours and twenty three and one")
        self.check("one hundred twenty three minutes and one second")
        self.check("one hour twenty three minutes and one second")
        self.check("one hour twenty three minutes oh one")
        self.check("oh one twenty three minutes oh one")
        self.check("oh one twenty three oh one")
        self.check("zero one twenty three oh one")
        self.check("one twenty three oh one")
        self.check("one twenty three and one second")


    def test_1_23_23(self):
        self.expected_value = "1 23 23"
        self.check("one hundred twenty three twenty three")
        self.check("one hundred hours twenty three and twenty three seconds")
        self.check("one hour twenty three minutes twenty three seconds")
        self.check("one hour twenty three minutes and twenty three seconds")
        self.check("one hour twenty three minutes twenty three")
        self.check("oh one twenty three minutes twenty three")
        self.check("oh one twenty three twenty three")
        self.check("one twenty three twenty three")
        self.check("one twenty three and twenty three seconds")

    def test_23_0_1(self):
        self.expected_value = "23 0 1"
        self.check("twenty three hour zero minutes and one second")
        self.check("twenty three hours zero minutes one second")
        self.check("twenty three hundred hours zero minutes one second")
        self.check("twenty three hours one second")
        self.check("twenty three hours and one second")
        self.check("twenty three hundred and one second")
        self.check("twenty three hundred hours one second")
        self.check("twenty three hundred hours oh oh oh one")
        self.check("twenty three hundred oh oh oh one")
        self.check("twenty three oh clock and one second")
        self.check("twenty three oh oh oh one")
        self.check("twenty three hour and zero minutes one second")
        self.check("twenty three hour and zero minutes oh one")

    def test_23_1_0(self):
        self.expected_value = "23 1 0"
        self.check("twenty three hours one minute")
        self.check("twenty three hours one minute and zero seconds")
        self.check("twenty three hours one minute zero seconds")
        self.check("twenty three hundred oh one")
        self.check("twenty three hundred hours and one minute")
        self.check("twenty three oh one oh clock")
        self.check("twenty three oh one o'clock")
        self.check("twenty three hundred oh one")
        self.check("twenty three oh one oh oh")
        self.check("twenty three hours and one minute zero seconds")
        self.check("twenty three hours and one minute oh oh")

    def test_23_1_1(self):
        self.expected_value = "23 1 1"
        self.check("twenty three hour one minute and one second")
        self.check("twenty three hours one minute one second")
        self.check("twenty three hundred hours one minute one second")
        self.check("twenty three hours one minute and one second")
        self.check("twenty three hundred one minute and one second")
        self.check("twenty three hundred hours oh one oh one")
        self.check("twenty three hundred oh one oh one")
        self.check("twenty three oh one and one second")
        self.check("twenty three oh one oh one")
        self.check("twenty three oh one o'clock oh one")
        self.check("twenty three oh one oh clock oh one")
        self.check("twenty three hours and one minute one second")
        self.check("twenty three hours and one minute oh one")

    def test_23_1_23(self):
        self.expected_value = "23 1 23"
        self.check("twenty three hour one minute and twenty three seconds")
        self.check("twenty three hour one minute twenty three seconds")
        self.check("twenty three hour one minute and twenty three seconds")
        self.check("twenty three hundred hours one minute and twenty three seconds")
        self.check("twenty three hour and one minute twenty three seconds")
        self.check("twenty three hour and one minute twenty three")
        self.check("twenty three oh one twenty three")
        self.check("twenty three oh one twenty three seconds")
        self.check("twenty three oh one and twenty three seconds")

    def test_23_23_1(self):
        self.expected_value = "23 23 1"
        self.check("twenty three hundred hours twenty three oh one")
        self.check("twenty three oh clock twenty three and one second")
        self.check("twenty three o'clock twenty three oh one")
        self.check("twenty three o'clock twenty three and one second")
        self.check("twenty three hour twenty three minutes and one second")
        self.check("twenty three hour twenty three minutes one second")
        self.check("twenty three hundred hours and twenty three minutes oh one")
        self.check("twenty three twenty three oh one")
        self.check("twenty three hour and twenty three minutes one second")
        self.check("twenty three hour and twenty three minutes oh one")


class Test_grok_year(check_word_list_grok):
    def grok_fn(self, word_list):
        return rename_by_voice.grok_year(word_list)

    def test_1900(self):
        self.expected_value = 1900
        self.check("one thousand nine hundred")
        self.check("nineteen hundred")
        self.check("nineteen oh oh")

    def test_2000(self):
        self.expected_value = 2000
        self.check("two thousand")
        self.check("twenty oh oh")

    def test_2001(self):
        self.expected_value = 2001
        self.check("two thousand one")
        self.check("two thousand and one")
        self.check("twenty oh one")

    def test_2009(self):
        self.expected_value = 2009
        self.check("two thousand nine")
        self.check("two thousand and nine")
        self.check("twenty oh nine")

    def test_2010(self):
        self.expected_value = 2010
        self.check("two thousand ten")
        self.check("two thousand and ten")
        self.check("twenty ten")

    def test_2011(self):
        self.expected_value = 2011
        self.check("two thousand eleven")
        self.check("two thousand and eleven")
        self.check("twenty eleven")
        self.check("twenty hundred eleven")

    def test_2019(self):
        self.expected_value = 2019
        self.check("two thousand nineteen")
        self.check("two thousand and nineteen")
        self.check("twenty nineteen")

    def test_2020(self):
        self.expected_value = 2020
        self.check("two thousand twenty")
        self.check("two thousand and twenty")
        self.check("twenty twenty")

    def test_2021(self):
        self.expected_value = 2021
        # Sometimes PocketSphinx mishears "one" as "why"
        self.check("two thousand twenty why")
        self.check("two thousand and twenty one")
        self.check("twenty twenty one")

    def test_2022(self):
        self.expected_value = 2022
        self.check("two thousand twenty two")
        self.check("two thousand and twenty two")
        self.check("twenty twenty two")

    def test_2029(self):
        self.expected_value = 2029
        self.check("two thousand twenty nine")
        self.check("two thousand and twenty nine")
        self.check("twenty twenty nine")

    def test_2100(self):
        self.expected_value = 2100
        self.check("two thousand one hundred")
        self.check("two thousand and one hundred")
        self.check("twenty one hundred")
        self.check("twenty one oh oh")

    def test_2101(self):
        self.expected_value = 2101
        self.check("two thousand one hundred one")
        self.check("two thousand and one hundred and one")
        self.check("twenty one hundred one")
        self.check("twenty one hundred and one")
        self.check("twenty one oh one")

    def test_2119(self):
        self.expected_value = 2119
        self.check("two thousand one hundred nineteen")
        self.check("two thousand and one hundred and nineteen")
        self.check("twenty one hundred nineteen")
        self.check("twenty one hundred and nineteen")
        self.check("twenty one nineteen")

    def test_2120(self):
        self.expected_value = 2120
        self.check("two thousand one hundred twenty")
        self.check("two thousand and one hundred and twenty")
        self.check("twenty one hundred twenty")
        self.check("twenty one hundred and twenty")
        self.check("twenty one twenty")

    def test_2121(self):
        self.expected_value = 2121
        self.check("two thousand one hundred twenty one")
        self.check("two thousand and one hundred and twenty one")
        self.check("twenty one hundred twenty one")
        self.check("twenty one hundred and twenty one")
        self.check("twenty one twenty one")

    def test_2129(self):
        self.expected_value = 2129
        self.check("two thousand one hundred twenty nine")
        self.check("two thousand and one hundred and twenty nine")
        self.check("twenty one twenty nine")


class Test_grok_date_words(check_word_list_grok):
    def grok_fn(self, word_list):
        year, month, day, day_of_week, rest = rename_by_voice.grok_date_words(word_list)
        self.assertEqual(word_list, rest)
        return f"{year} {month} {day} {day_of_week}"

    def test_2021_1_1_monday(self):
        self

if __name__ == '__main__':
    unittest.main()
