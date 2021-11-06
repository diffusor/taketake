#!/usr/bin/env python3

import unittest

# Import taketake from the parent dir for in-situ testing
# From https://codeolives.com/2020/01/10/python-reference-module-in-parent-directory/
import sys
import os
import re
import inspect
import datetime
currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.append(parentdir)

import taketake
import asyncio
import tempfile
import shutil
import time
import subprocess
import argparse
import contextlib
import functools
from pathlib import Path
import dataclasses
import json

keeptemp = int(os.environ.get("TEST_TAKETAKE_KEEPTEMP", "0"))
dontskip = int(os.environ.get("TEST_TAKETAKE_DONTSKIP", "0"))
min_xdelta_target_size_for_match = 19
testflac = "testdata/audio.20210318-2020-Thu.timestamp-wrong-weekday-Monday.flac"
testpath = os.path.dirname(os.path.abspath(__file__))
testflacpath = os.path.join(testpath, testflac)
flacsize = os.path.getsize(testflacpath)
flacwavsize = 1889324
flacaudioinfo = taketake.AudioInfo(
        duration_s=10.710204,
        extra_speech=[],
        parsed_timestamp=datetime.datetime(2021, 3, 18, 20, 20),
        recognized_speech="twenty twenty monday march eighteenth two thousand twenty one",
        speech_range=taketake.TimeRange(duration=4.507420000000001, start=1.64045),
    )

#===========================================================================
# File helpers
#===========================================================================

class FileAssertions():
    def __init__(self, *args, **kwargs):
        super.__init__(self, *args, **kwargs)
        self.addTypeEqualityFunc(Path, self.assertPathEqual)

    def assertDataclassesEqual(self, a, b, msg=None):
        self.assertEqual(a.__class__, b.__class__, msg)
        self.assertEqual(dataclasses.asdict(a), dataclasses.asdict(b), msg)

    def assertPathEqual(self, a:Path, b:Path, msg:str=None):
        self.assertEqual(str(a), str(b), msg)

    def mlfmt(self, b):
        if isinstance(b, bytes):
            lines = b.decode().splitlines()
            return "\n    ".join(lines)
        elif b is None:
            return ""
        else:
            return repr(b)

    def poutfmt(self, p):
        return f"\n  cmd: '{' '.join(p.args)}'" \
                f"\n  stdout:  {self.mlfmt(p.stdout)}" \
                f"\n  stderr:  {self.mlfmt(p.stderr)}"

    def assertIsDir(self, p:Path):
        assert p.is_dir(), f"Not a directory: {str(p)}"

    def assertNoFile(self, p:Path, msg=None):
        self.assertFalse(p.exists(), f"File exists: {str(p)}")

    def assertSymlinkTo(self, p:Path, expected:Path):
        assert p.is_symlink(), f"Not a symlink: {str(p)}"
        target = p.readlink()
        self.assertPathEqual(target, expected, f"\n  Wrong target in symlink {str(p)}")

    def assertEqualFiles(self, f1, f2):
        p = subprocess.run(("cmp", f1, f2), capture_output=True)
        if p.returncode != 0:
            raise AssertionError(f"Files mismatch:\n  {f1}\n  {f2}{self.poutfmt(p)}")

    def assertNotEqualFiles(self, f1, f2):
        p = subprocess.run(("cmp", f1, f2), capture_output=True)
        if p.returncode == 0:
            raise AssertionError(f"Files match:\n  {f1}\n  {f2}{self.poutfmt(p)}")

    def assertFileType(self, f, typestring):
        """Run the file(1) command on f and check its string against typestring,
        which should not include the 'file: ' portion of the output"""
        p = subprocess.run(("file", f), capture_output=True, text=True, check=True)
        try:
            self.assertEqual(p.stdout.strip(), f"{f}: {typestring}")
        except AssertionError as e:
            e.args = (f"Bad file type; see + line below for expected type:\n  {e.args[0]}", *e.args[1:])
            raise

    def assertExitCode(self, p, exitcode=0):
        if p.returncode != exitcode:
            raise AssertionError(f"Expected exit code {exitcode} != {p.returncode}:{self.poutfmt(p)}")

    def assertMd5FileGood(self, md5file):
        p = subprocess.run(("md5sum", "-c", md5file), capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(f"md5sum check failed:{self.poutfmt(p)}")

    def assertMd5FileBad(self, md5file):
        p = subprocess.run(("md5sum", "-c", md5file), capture_output=True, text=True)
        if p.returncode == 0:
            raise AssertionError(f"md5sum check unexpectedly passed:{self.poutfmt(p)}")

    def gen_xdelta_from_flac(self, flac, wav, xdelta):
        flac_p, xdelta_p = asyncio.run(
                taketake.encode_xdelta_from_flac_to_wav(flac, wav, xdelta))
        self.assertExitCode(flac_p, 0)
        self.assertExitCode(xdelta_p, 0)

    def check_xdelta(self, xdelta_file, source_file, target_file):
        """Pull out the size from the file itself and run the checker"""
        if not isinstance(source_file, int):
            source_file = os.path.getsize(source_file)
        if not isinstance(target_file, int):
            target_file = os.path.getsize(target_file)
        asyncio.run(taketake.check_xdelta(xdelta_file, source_file, target_file))


class TempdirFixture(unittest.TestCase):
    def setUp(self):
        timestamp = time.strftime("%Y%m%d-%H%M%S-%a")
        self.tempdir = tempfile.mkdtemp(
                prefix=f'{self.__class__.__name__}.{timestamp}.')
        #print("Tempdir:", self.tempdir)

    def tearDown(self):
        cleandir(self.tempdir)

    def tempfile(self, fname):
        return os.path.join(self.tempdir, fname)


class CdTempdirFixture(TempdirFixture):
    """Changes dirictory into the tempdir for execution of each test.
    Also store the Config so we don't get carried config state between tests.
    """
    def setUp(self):
        super().setUp()
        self.origdir = os.getcwd()
        os.chdir(self.tempdir)
        self.saved_config = dict(**taketake.Config.__dict__)

    def tearDown(self):
        for k, v in self.saved_config.items():
            if not k.startswith('_'):
                setattr(taketake.Config, k, v)
        os.chdir(self.origdir)
        super().tearDown()


# from https://stackoverflow.com/a/170174
@contextlib.contextmanager
def cd(newdir):
    olddir = os.getcwd()
    try:
        os.chdir(newdir)
        yield
    finally:
        os.chdir(olddir)

def raises_nodir(dirname):
    """Decorates test function, injects Path(dirname) as an argument"""
    def decorator(function):
        @functools.wraps(function)
        def wrapper(self, *args, **kwargs):
            with self.assertRaisesRegex(taketake.TaketakeRuntimeError,
                    f"Dest dir does not exist! '{dirname}'"):
                return function(self, Path(dirname), *args, **kwargs)
        return wrapper
    return decorator

def pathlist(s):
    return [Path(word) for word in s.split()]

def fmtpaths(paths):
    return " ".join(str(p) for p in paths)

def make_md5sum_file(fname, md5file):
    with open(md5file, "w") as f:
        subprocess.run(("md5sum", "-b", fname), stdout=f, check=True, text=True)

def encode_xdelta(source, input, xdelta):
    assert xdelta.endswith(".xdelta")
    subprocess.run(("xdelta3", "-f", "-s", source, input, xdelta),
            capture_output=True, check=True, text=True)

def cleandir(d):
    if keeptemp:
        subprocess.run(("ls", "-d", d))
        subprocess.run(("ls", "-al", d))
    else:
        shutil.rmtree(d)

#===========================================================================
# Speech recognition and parsing tests
#===========================================================================

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


class Test0_grok_digit_pair(check_word_list_grok):
    def grok_fn(self, word_list):
        return taketake.grok_digit_pair(word_list)

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


class Test0_grok_time_words(check_word_list_grok):
    def grok_fn(self, word_list):
        hour, minute, second, rest = taketake.grok_time_words(word_list)
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


class Test0_grok_year(check_word_list_grok):
    def grok_fn(self, word_list):
        return taketake.grok_year(word_list)

    def test_1900(self):
        self.expected_value = 1900
        self.check("one thousand nine hundred")
        self.check("nineteen hundred")
        self.check("nineteen oh oh")

    def test_2000(self):
        self.expected_value = 2000
        self.check("two thousand")
        self.check("twenty oh oh")
        self.check("twenty hundred")

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


class Test0_grok_date_words(check_word_list_grok):
    def grok_fn(self, word_list):
        year, month, day, day_of_week, rest = taketake.grok_date_words(word_list)
        self.assertEqual(word_list, rest)
        return f"{year} {month} {day} {day_of_week}"

    def test_2021_1_1_friday(self):
        self.expected_value = "2021 1 1 friday"
        self.check("january first friday twenty twenty one")
        self.check("friday january first twenty twenty one")

    def test_2021_1_1_None(self):
        self.expected_value = "2021 1 1 None"
        self.check("january first twenty twenty one")


class Test0_TimestampGrokError(unittest.TestCase):
    def check(self, text):
        with self.assertRaisesRegex(taketake.TimestampGrokError,
                                    self.regex):
            print(taketake.words_to_timestamp(text))

    def test_no_month(self):
        self.regex = "^Failed to find a month name in "
        self.check("")
        self.check("foo")
        self.check("uh the man")
        self.check("one eight fifty nine")
        # Known example cases
        self.check('man a man man who')
        self.check('in')
        self.check('lou in the and hang on and on')
        self.check('the known and why')
        self.check("by hand now you when ryan and the it only one hundred and one they do and and and let and knew who the more dead and am now you are now i'm i'm i'm")
        self.check('you do')
        self.check('the')
        self.check('oh')

    def test_no_day_of_month(self):
        self.regex = "^word_list is empty, no day of month found"
        self.check("may")
        self.check("5 oh clock august")

    def test_no_nth(self):
        self.regex = "^Could not find Nth-like ordinal in"
        self.check("eighteen twenty one may twenty twenty one")
        self.check("eighteen twenty one thirteenth of may twenty twenty one")
        # Known example cases
        self.check("you mean there are or power or come to new you to do to move them our earnings and no there you didn't do to in june it no you didn't do didn't know ah there are so than it june")

    def test_bad_month_day(self):
        self.regex = r"^Parsed month day \d+ from .* is out of range"
        self.check("may forty first nineteen thirteen")
        self.check("5 oh clock august thirty fourth twenty two oh five")

    def test_no_year(self):
        self.regex = "^Could not find year in"
        self.check("may first blah")
        self.check("may first man")
        self.check("may first")
        self.check("5 oh clock august fourth")

    def test_year_parse_failure(self):
        self.regex = "^Expected 'thousand' after \d+ parsing year from"
        self.check("may first one")

    def test_year_parse_failure(self):
        self.regex = "^Year parse error: missing second doublet after"
        self.check("may first twenty one")

    def test_year_out_of_range(self):
        self.regex = "^Parsed year \d+ from '.*' is out of range"
        self.check("may first twenty four thousand")
        self.check("may first eighteen oh four")
        self.check("5 oh clock august fourth thirty oh one")
        self.check("5 oh clock august fourth five")
        self.check("may first oh")

    def test_None(self):
        self.regex = "^Given text is None$"
        self.check(None)


class Test0_words_to_timestamp(unittest.TestCase):
    def check_impl(self, text, expect, expected_rem=""):
        """Checks that the given string text decodes to self.expected_value,
        with the given remaining words joined into a string passed in as expected_rem.
        """
        got_value, extra = taketake.words_to_timestamp(text)
        got_rem = " ".join(extra)
        self.assertEqual(got_value, expect)
        self.assertEqual(got_rem, expected_rem)

    def check(self, text, *expect_ymdhms):
        dt = datetime.datetime(*expect_ymdhms)
        self.check_impl(text, dt)
        self.check_impl(text + " with stuff", dt, "with stuff")

    def test_contrived_examples(self):
        self.check("zero oh one wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 0, 1, 0)
        self.check("zero fifty one wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 0, 51, 0)
        self.check("zero hundred wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 0, 0, 0)
        self.check("five oh clock wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 5, 0, 0)
        self.check("five oclock wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 5, 0, 0)
        self.check("zero five wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 5, 0)
        self.check("five oh five wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 5, 5, 0)
        self.check("nineteen hundred wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 19, 0, 0)
        self.check("nineteen hundred hours wednesday may nineteenth twenty twenty one",
                   2021, 5, 19, 19, 0, 0)

    def test_known_examples(self):
        # The actual example had monday due to hand-construction of the timestamp
        self.check("twenty twenty thursday march eighteenth two thousand twenty why",
                   2021, 3, 18, 20, 20, 0)
        self.check("eleven fifteen sunday march twenty first two thousand twenty one",
                   2021, 3, 21, 11, 15, 0)
        self.check("thirteen hundred hours sunday march twenty first two thousand twenty one",
                   2021, 3, 21, 13, 0, 0)
        self.check("twenty one forty one thursday march twenty fifth two thousand twenty one",
                   2021, 3, 25, 21, 41, 0)

    def test_no_time(self):
        self.check("may first nineteen thirteen",
                   1913, 5, 1)
        self.check("5 oh clock august fourth twenty two oh five",
                   2205, 8, 4, 5)


class Test0_format_duration(unittest.TestCase):
    def check(self, s, expect, expect_colons=None):
        with self.subTest(s=s, expect=expect):
            formatted = taketake.format_duration(s)
            self.assertEqual(formatted, expect)
        if expect_colons is not None:
            with self.subTest(s=s, expect_colons=expect_colons):
                formatted = taketake.format_duration(s, style="colons")
                self.assertEqual(formatted, expect_colons)


    def test_seconds(self):
        self.check(0, "0s", '0:00:00')
        self.check(0.5, "0s", '0:00:00.5')
        self.check(1.49, "1s", '0:00:01.49')
        self.check(1.5, "2s", '0:00:01.5')
        self.check(59, "59s", '0:00:59')

    def test_minutes_no_seconds(self):
        self.check(60, "1m", '0:01:00')
        self.check(60+60, "2m", '0:02:00')
        self.check(60*59, "59m", '0:59:00')

    def test_minutes_with_seconds(self):
        self.check(61, "1m1s", '0:01:01')
        self.check(60+59, "1m59s", '0:01:59')
        self.check(60+60+1, "2m1s", '0:02:01')
        self.check(60*60-1, "59m59s", '0:59:59')

    def test_hours_no_minutes_no_seconds(self):
        self.check(3600, "1h", '1:00:00')
        self.check(3600*2, "2h", '2:00:00')
        self.check(3600*60, "60h", '60:00:00')

    def test_hours_no_minutes_with_seconds(self):
        self.check(3600+1, "1h1s", '1:00:01')
        self.check(3600+59, "1h59s", '1:00:59')
        self.check(3600*2+1, "2h1s", '2:00:01')
        self.check(3600*60+59, "60h59s", '60:00:59')

    def test_hours_with_minutes_no_seconds(self):
        self.check(3600+1*60, "1h1m", '1:01:00')
        self.check(3600+59*60, "1h59m", '1:59:00')
        self.check(3600*2+1*60, "2h1m", '2:01:00')
        self.check(3600*60+59*60, "60h59m", '60:59:00')

    def test_hours_with_minutes_and_seconds(self):
        self.check(3600+1*60+1, "1h1m1s", '1:01:01')
        self.check(3600+59*60+1, "1h59m1s", '1:59:01')
        self.check(3600+59*60+59, "1h59m59s", '1:59:59')
        self.check(3600*2+1*60+1, "2h1m1s", '2:01:01')
        self.check(3600*2+1*60+59, "2h1m59s", '2:01:59')
        self.check(3600*60+59*60+1, "60h59m1s", '60:59:01')
        self.check(3600*60+59*60+59, "60h59m59s", '60:59:59')


    def test_TimeRange_str(self):
        for start, dur, expect in (
                (0, 0, '[0:00:00-0:00:00](0s)'),
                (0, 1, '[0:00:00-0:00:01](1s)'),
                (1, 0, '[0:00:01-0:00:01](0s)'),
                (192167, 0, '[53:22:47-53:22:47](0s)'),
                (192167.1531, 135870.2123, '[53:22:47.15-91:07:17.37](37h44m30s)'),
                ):
            with self.subTest(start=start, dur=dur, expect=expect):
                self.assertEqual(f"{taketake.TimeRange(start, dur)}", expect)

class Test0_format_dest_filename(unittest.TestCase):
    def test_format_dest_filename(self):
        xinfo = taketake.TransferInfo(
                source_wav=Path("wow.wav"),
                wav_abspath=Path("foo/wow008.wav"),
                dest_dir=Path("dest"),
                wav_progress_dir=Path("dest/baz"),
                source_link=Path(),
                instrument="foobuzz",
                audioinfo=taketake.AudioInfo(
                    duration_s=16391,
                    extra_speech="these are extra words".split(),
                    ),
                timestamp=datetime.datetime.fromtimestamp(0,
                    tz=datetime.timezone(datetime.timedelta(0))),
                )

        for extra in "", "bark", "these are extra words":
            with self.subTest(extra_speech=extra):
                extra_list = extra.split()
                xinfo.audioinfo.extra_speech = extra_list
                expect = "-".join(extra_list)
                if expect:
                    expect += "."
                self.assertEqual(taketake.format_dest_filename(xinfo),
                        f"piano.19700101-000000-Thu.{expect}4h33m11s.foobuzz.wow")

class Test0_parse_timestamp(unittest.TestCase):
    def test_parse_timestamp(self):
        for in_str in (
                "20210113-125657",
                "19301012-005638",
                "21351201-230002",
                ):
            for sep in "_", " ":
                s = in_str.replace("-", sep)

                for day in "Wed Mon wed sat tue".split():
                    with_seconds = s
                    with self.subTest(with_seconds=with_seconds):
                        out = taketake.parse_timestamp(with_seconds).strftime(f"%Y%m%d{sep}%H%M%S")
                        self.assertEqual(out, with_seconds)

                    with_seconds_and_day = f"{with_seconds}{sep}{day}"
                    with self.subTest(with_seconds_and_day=with_seconds_and_day):
                        out = taketake.parse_timestamp(with_seconds_and_day).strftime(f"%Y%m%d{sep}%H%M%S")
                        self.assertEqual(out, with_seconds)

                    no_seconds = s[:-2]
                    with self.subTest(no_seconds=no_seconds):
                        out = taketake.parse_timestamp(no_seconds).strftime(f"%Y%m%d{sep}%H%M%S")
                        self.assertEqual(out, no_seconds+"00")

                    no_seconds_and_day = f"{no_seconds}{sep}{day}"
                    with self.subTest(no_seconds_and_day=no_seconds_and_day):
                        out = taketake.parse_timestamp(no_seconds_and_day).strftime(f"%Y%m%d{sep}%H%M%S")
                        self.assertEqual(out, no_seconds+"00")

    def test_parse_timestamp_bad(self):
        for s in (
                "x20210113-125657",
                "21351201-230002x",
                "21351201:230002",
                "211201T2300",
                "19301012-005638-Wxd",
                "19301012-005638-Monday",
                "19301012-005638-Mo",
                "19301012-005638-",
                "",
                "foo",
                "2021-11-01 11:42 -0700 Mon",
                "2021-11-01 11:42",
                ):
            with self.subTest(s=s):
                out = taketake.parse_timestamp(s)
                self.assertEqual(out, None)

#===========================================================================
# Test1 - Queues and Steppers
#===========================================================================

class Test1_stepper(unittest.IsolatedAsyncioTestCase):
    def test_make_queues_empty(self):
        d = taketake.make_queues("")
        self.assertEqual(d, {})

    async def test_make_queues(self):
        names = " a b a_queue "
        d = taketake.make_queues(names)
        self.assertEqual(list(d.keys()), names.split())
        for n in names.split():
            self.assertEqual(getattr(d, n), d[n])
            self.assertIsInstance(d[n], asyncio.Queue)
            self.assertEqual(d[n].name, n)

    async def test_pre_sync(self):
        d = taketake.make_queues("coms coms_sync")
        runlist = []

        async def finisher(stepper):
            runlist.append("finisher")
            await stepper.pre_sync()
            runlist.append("done")

        async def goer(stepper):
            runlist.append("goer")
            for i in range(2):
                runlist.append(f"-{i}")
                await asyncio.sleep(0)
                await stepper.put(i)
                await asyncio.sleep(0)
                runlist.append(f"+{i}")
                await asyncio.sleep(0)
            runlist.append(f"-None")
            await asyncio.sleep(0)
            await stepper.put(None)
            await asyncio.sleep(0)
            runlist.append(f"+None")
            await asyncio.sleep(0)

        await asyncio.gather(
                finisher(taketake.Stepper("finisher",
                    sync_from=d.coms_sync, pull_from=d.coms)),
                goer(taketake.Stepper("goer",
                    send_to=d.coms, sync_to=d.coms_sync)),
                )

        self.assertEqual(runlist,
                ['finisher', 'goer', '-0', '+0', '-1', '+1', '-None', '+None', 'done'])

    async def test_join(self):
        d = taketake.make_queues("q1 q2 q3")
        num_tokens = 3

        async def joiner(stepper):
            r = ""
            while (token := await stepper.get()) is not None:
                r += str(token)
            return r

        async def sender(name, stepper):
            for i in range(num_tokens):
                await stepper.put(i)
            await stepper.put(None)
            return i

        senders = []
        for i, q in enumerate(d.values()):
            senders.append(sender(f"s{i}", taketake.Stepper(send_to=q)))

        r = await asyncio.gather(
                joiner(taketake.Stepper(pull_from=d.values())),
                *senders)

        self.assertEqual(r, ['012'] + [num_tokens-1] * len(d))

    async def test_queue_desync_error(self):
        #taketake.Config.debug = True
        d = taketake.make_queues("q1 q2")
        runlist = []
        def log(msg):
            runlist.append(msg)

        async def joiner(stepper):
            log("-j")
            while (token := await stepper.get()) is not None:
                log(f"j{token}")
            log("+j")

        async def sender(name, stepper):
            log(f"-{name}")
            await stepper.put(name)
            log(f"1{name}")
            await stepper.put(None)
            log(f"+{name}")

        senders = []
        for i, q in enumerate(d.values()):
            senders.append(sender(f"s{i}", taketake.Stepper(name=q.name, send_to=q)))

        with self.assertRaisesRegex(
                taketake.Stepper.DesynchronizationError,
                "Mismatching tokens between token-queues detected in Stepper\(joiner\).*"
                "\n.*"
                "\n  Tokens that matched across queues: \[\]"
                "\n  Mismatches in q1: \['s0'\]"
                "\n  Mismatches in q1: \['s1'\]"):

            await asyncio.gather(
                    joiner(taketake.Stepper(name="joiner", pull_from=d.values())),
                    *senders)

        self.assertEqual(" ".join(runlist),
                "-j -s0 1s0 +s0 -s1 1s1 +s1")

    async def test_queue_pre_sync_error(self):
        async def badsrc(stepper):
            await stepper.sync_to[0].put("dup")

        async def sink(stepper):
            await stepper.get()

        network = taketake.StepNetwork("net")
        network.add(badsrc, sync_to=sink)
        network.add(sink, sync_from=badsrc)

        with self.assertRaisesRegex(
                taketake.Stepper.PreSyncTokenError,
                "Got non-end token 'dup' from sync_from queues badsrc->sink"):
            await network.execute()

    async def test_queue_dup_token_error(self):
        async def dupsrc(stepper):
            await stepper.put("dup")
            await stepper.put("dup")
            await stepper.put(None)

        async def consrc(stepper):
            await stepper.put("confound")
            await stepper.put("nomatch")
            await stepper.put(None)

        @taketake.StepNetwork.stepped
        async def sink(token, stepper): ...

        network = taketake.StepNetwork("net")
        network.add(dupsrc, send_to=sink)
        network.add(consrc, send_to=sink)
        network.add(sink, pull_from=[dupsrc, consrc])

        with self.assertRaisesRegex(
                taketake.Stepper.DuplicateTokenError,
                "Duplicate token dup from token-queue dupsrc->sink detected"):
            await network.execute()

    async def test_send_post(self):
        #taketake.Config.debug = True
        d = taketake.make_queues("q1 q2 end")
        runlist = []
        def log(msg):
            runlist.append(msg)

        async def joiner(stepper):
            i = 0
            log("-j")
            stepper.log(f"** j{i} waiting for tokens**")
            while (token := await stepper.get()) is not None:
                i += 1
                log(f"j{i}")
                stepper.log(f"** got token={token} : j{i} **")
                self.assertEqual(token, i)
            log("jNone")
            await stepper.put(None)
            log("+j")

        async def finisher(stepper):
            log("-f")
            await asyncio.wait_for(stepper.pre_sync(), timeout=1)
            log("+f")

        async def sender(name, stepper):
            log(f"-{name}")
            await stepper.put(1)
            log(f"1{name}")
            await asyncio.sleep(0.0005) # Allow the joiner to run
            await stepper.put(2)
            log(f"2{name}")
            await stepper.put(None)
            log(f"+{name}")

        r = await asyncio.gather(
                finisher(taketake.Stepper(name="finisher", sync_from=d.end)),
                joiner(taketake.Stepper(name="joiner",
                    pull_from=[d.q1, d.q2],
                    sync_to=d.end,
                    )),
                sender("q1", taketake.Stepper(name="q1", send_to=d.q1)),
                sender("q2", taketake.Stepper(name="q2", send_to=d.q2)))

        self.assertEqual(" ".join(runlist),
                #"-f -j -q1 1q1 -q2 1q2 2q1 +q1 2q2 +q2 j1 j2 jNone +j +f")
                "-f -j -q1 1q1 -q2 1q2 j1 2q1 +q1 2q2 +q2 j2 jNone +j +f")

    async def test_step_network(self):
        """Build a network that uses all the features and run it."""
        worklist = "a b".split()
        runlist = []

        def update(stepper):
            runlist.append(f"{stepper.name}:{stepper.value}")

        async def src1(stepper):
            for e in worklist:
                await stepper.put(e)
                runlist.append(f"src1:{e}")
            await stepper.put(None)

        async def src2(stepper):
            for e in worklist:
                await stepper.put(e)
                runlist.append(f"src2:{e}")
            await stepper.put(None)

        @taketake.StepNetwork.stepped
        async def w1(token, stepper): update(stepper)
        @taketake.StepNetwork.stepped
        async def w2(token, stepper): update(stepper)
        @taketake.StepNetwork.stepped
        async def w3(token, stepper): update(stepper)
        @taketake.StepNetwork.stepped
        async def w4(token, stepper): update(stepper)

        async def f1(token, stepper): update(stepper)
        async def f2(token, stepper): update(stepper)

        network = taketake.StepNetwork("net")
        network.add(src1, send_to=w1, sync_to=[w1, w2])
        network.add(src2, send_to=[w1, w2, w3], sync_to=w3)

        network.add(w1, sync_from=src1, pull_from=[src1, src2],
                             sync_to=w4,     send_to=w4)

        network.add(w2, sync_from=src1, pull_from=src2,
                                             send_to=w4)

        network.add(w3, sync_from=src2, pull_from=src2,
                                             send_to=w4)

        network.add(w4, pull_from=[w1, w2, w3], sync_from=w1)
        await network.execute()

        self.assertEqual(" ".join(runlist),
        "src1:a src1:b src2:a src2:b w1:a w2:a w3:a w1:b w2:b w3:b w4:a w4:b")

    async def test_step_network_pipeline(self):
        worklist = "a b c".split()
        runlist = []
        def update(stepper):
            if not hasattr(stepper, "seen_tokens"):
                stepper.seen_tokens = set()
            stepper.seen_tokens.add(stepper.value)

        async def src(stepper):
            for e in worklist:
                await stepper.put(e)
                runlist.append(f"src:{e}")
            await stepper.put(None)
        @taketake.StepNetwork.stepped
        async def w1(token, stepper): update(stepper)
        @taketake.StepNetwork.stepped
        async def w2(token, stepper): update(stepper)
        @taketake.StepNetwork.stepped
        async def w3(token, stepper): update(stepper)
        @taketake.StepNetwork.stepped
        async def w4(token, stepper): update(stepper)

        network = taketake.StepNetwork("net")
        network.add_pipeline(src, w1, w2, w3, w4)
        await network.execute()
        self.assertEqual(" ".join(runlist), "src:a src:b src:c")
        for w in w1, w2, w3, w4:
            with self.subTest(w=w.__name__):
                self.assertEqual(w._stepper.seen_tokens, set(worklist))


    async def test_add_step_with_no_source(self):
        @taketake.StepNetwork.stepped
        async def s(): pass

        network = taketake.StepNetwork("net")
        with self.assertRaisesRegex(AssertionError,
                "step s needs a pull_from source$"):
            network.add(s)

    async def test_add_step_with_sync_but_no_source(self):
        @taketake.StepNetwork.stepped
        async def s(): pass
        @taketake.StepNetwork.stepped
        async def s2(): pass
        network = taketake.StepNetwork("net")
        with self.assertRaisesRegex(AssertionError,
                "step s needs a pull_from source$"):
            network.add(s, sync_from=s2)

    async def test_add_step_identical_sources(self):
        @taketake.StepNetwork.stepped
        async def s(): pass
        @taketake.StepNetwork.stepped
        async def s2(): pass
        network = taketake.StepNetwork("net")
        with self.assertRaisesRegex(AssertionError,
                "Already added dest side of Link\(s2->s\):"):
            network.add(s, pull_from=[s2, s2])

    async def test_add_step_missing_source(self):
        async def p(): pass
        @taketake.StepNetwork.stepped
        async def s1(): pass
        @taketake.StepNetwork.stepped
        async def s2(): pass

        network = taketake.StepNetwork("net")
        network.add(p, send_to=s1)
        with self.assertRaisesRegex(AssertionError,
                r"Already added p, but it was missing p:send_to=s2 for token-type Link\(p->s2\) in StepNetwork\(net\)$"):
            network.add(s2, pull_from=p)

    async def test_execute_missing_source(self):
        async def p(): pass
        @taketake.StepNetwork.stepped
        async def s1(): pass
        @taketake.StepNetwork.stepped
        async def s2(): pass

        network = taketake.StepNetwork("net")
        network.add(p, send_to=[s1, s2])
        network.add(s1, pull_from=p)
        with self.assertRaisesRegex(AssertionError,
                "missing s2:pull_from=p for token-type Link\(p->s2\) in StepNetwork\(net\)$"):
            await network.execute()

    async def test_execute_selfloop_src(self):
        async def p(): pass
        @taketake.StepNetwork.stepped
        async def s(): pass
        network = taketake.StepNetwork("net")
        network.add(p, send_to=s)
        with self.assertRaisesRegex(AssertionError,
                r"Self-loops are disallowed: s:send_to=s for token-type Link\(s->s\) in StepNetwork\(net\)$"):
            network.add(s, pull_from=p, send_to=s)

    async def test_execute_cycle(self):
        async def p(): pass
        @taketake.StepNetwork.stepped
        async def s1(): pass
        @taketake.StepNetwork.stepped
        async def s2(): pass
        @taketake.StepNetwork.stepped
        async def s3(): pass

        network = taketake.StepNetwork("net")
        network.add(p, send_to=s1)
        network.add(s1, pull_from=p, sync_from=s3, send_to=s2)
        network.add(s2, pull_from=s1, send_to=s3)
        network.add(s3, pull_from=s2, sync_to=s1)

        with self.assertRaisesRegex(taketake.StepNetwork.HasCycle,
                "found backedge s3->s1:s1->s2->s3"):
            await network.execute()


#===========================================================================
# Test2 - JSON encode/decode
#===========================================================================

class Test2_json_AudioInfo(TempdirFixture, FileAssertions):
    def setUp(self):
        super().setUp()
        self.ai = taketake.AudioInfo(
                duration_s=34.5,
                speech_range=taketake.TimeRange(start=3.01, duration=4),
                recognized_speech=None,
                parsed_timestamp=datetime.datetime.now(),
                extra_speech="foobar",
        )
        self.jsonfile = Path(self.tempdir) / "test2.json"

    def test_json_dumps_loads(self):
        s = json.dumps(self.ai, cls=taketake.TaketakeJsonEncoder)
        decoded_ai = json.loads(s, object_hook=taketake.taketake_json_decode)
        self.assertDataclassesEqual(self.ai, decoded_ai, f"\n* JSON encode = {s}")

    def test_path_write_read_json(self):
        taketake.write_json(self.jsonfile, self.ai)
        decoded_ai = taketake.read_json(self.jsonfile)
        self.assertDataclassesEqual(self.ai, decoded_ai,
                f"\n* JSON file contents: {self.jsonfile.read_text()}")

#===========================================================================
# Test3 - Read-only external commands, no tempdirs
#===========================================================================

class Test3_ext_commands_read_only(unittest.TestCase):
    """Test ExtCmd commands that don't modify the filesystem"""

    def test_duration_flac(self):
        duration = taketake.get_file_duration(testflacpath)
        self.assertAlmostEqual(duration, 10.710204, places=3)

    def test_duration_no_file(self):
        fpath = tempfile.mktemp(dir=testpath)
        with self.assertRaisesRegex(taketake.SubprocessError,
                f'(?s)Got bad exit code 1 from.*{fpath}: No such file or directory'):
            taketake.get_file_duration(fpath)

    def test_detect_silence(self):
        """This one is a bit fragile, as ffmpeg silencedetect float output is janky"""
        silences = taketake.detect_silence(testflacpath)
        self.assertEqual(silences, [taketake.TimeRange(start=0.0, duration=1.84045),
            taketake.TimeRange(start=5.94787, duration=1.91383),
            taketake.TimeRange(start=10.117, duration=0.593175)])

    def test_flac_wav_size(self):
        size = asyncio.run(taketake.get_flac_wav_size(testflacpath))
        self.assertEqual(size, flacwavsize)


#===========================================================================
# Test6 - longer external commands
#===========================================================================

class Test6_args(CdTempdirFixture):
    def setUp(self):
        super().setUp()
        self.base_args = dict(
                _dest=None,
                debug=False,
                no_act=False,
                do_prompt=True,
                prefix=None,
                keep_wavs=False,
                skip_copyback=False,
                skip_tests=False,
                skip_speech_to_text=False,
                continue_from=None,
                dest=Path(),
                fallback_timestamp='mtime',
                instrument='inst1',
                sources=[],
                wavs=[])
        self.cmdline_suffix = "-i inst1"

    def mkdir_progress(self, tag, subdir="."):
        name = Path(taketake.Config.progress_dir_fmt.format(tag))
        Path(self.tempdir, subdir, name).mkdir()
        return name

    def check_args(self, cmdline, *expected_errors, **kwargs):
        argparser = taketake.process_args(cmdline.split()
                + self.cmdline_suffix.split())
        errors = argparser.errors
        fmterr = taketake.format_errors

        if expected_errors:
            # Check errors only
            remaining_errors = set(errors)
            unmatched_errpats = []
            for errpat in expected_errors:
                for errstr in remaining_errors:
                    if re.search(errpat, errstr):
                        remaining_errors.remove(errstr)
                        break
                else:
                    unmatched_errpats.append(errpat)
            self.assertFalse(remaining_errors or unmatched_errpats,
                msg=f"\nExtra argparse errors:{fmterr(remaining_errors)}"
                    f"\nUnused error patterns:{fmterr(unmatched_errpats)}")

        else:
            # Ensure there are no errors
            self.assertEqual(len(errors), 0,
                    msg=f"\nUnexpected argparse errors:{fmterr(errors)}")
            # Check args
            args = vars(argparser.args)
            self.base_args.update(kwargs)
            self.assertEqual(args, self.base_args) # got != expected

    def check_args_with_prepended_src(self, cmdline, *args, **kwargs):
        """Wrap call to check_args but add a src argument.

        This function creates the directory, passes the argument, and fills
        in the sources and wavs expected args keywords.

        Note - if dest is specified positionally (not with -t), then it must come first on the cmdline passed to this function.
        """

        src = Path("srcwav")
        src.touch()
        self.check_args(f"{src} {cmdline}",
                *args,
                sources=[src],
                wavs=[src],
                **kwargs)

    def test_no_args(self):
        self.check_args("",
                "No DEST_PATH specified!",
                "No SOURCE_WAVs specified to transfer!")

    def test_no_args_1c(self):
        p1 = self.mkdir_progress("foo")
        self.check_args("",
                "No DEST_PATH specified!",
                "No SOURCE_WAVs specified to transfer!")

    def test_no_args_2c(self):
        self.mkdir_progress("foo")
        self.mkdir_progress("bar")
        self.check_args("",
                "No DEST_PATH specified!",
                "No SOURCE_WAVs specified to transfer!")

    def test_dest_in_positionals(self):
        d = Path("dest_foo")
        d.mkdir()
        self.check_args_with_prepended_src(f"{d}",
                dest=d)

    def test_dest_in_positionals_1c_in_parent(self):
        """Progress dir shouldn't be detected since it's not in dest."""
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo")
        self.check_args_with_prepended_src(f"{d}",
                dest=d)

    def test_dest_in_positionals_1c_in_dest(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        self.check_args(str(d),
                dest=Path(d),
                continue_from=d/p1)

    def test_dest_in_positionals_2c_in_dest(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        p2 = self.mkdir_progress("bar", d)
        self.check_args(str(d),
                "Too many progress directories found in DEST_PATH",
                "No SOURCE_WAVs specified to transfer!")

    def test_dest_in_option(self):
        d = Path("dest_foo")
        d.mkdir()
        self.check_args_with_prepended_src(f"-t {d}", dest=d)

    def test_dest_in_option_nodir(self):
        d = Path("dest_foo")
        self.check_args(f"-t {d}",
                "Specified DEST_PATH does not exist!",
                "No SOURCE_WAVs specified to transfer!")

    def test_two_positionals(self):
        d = Path("dest_foo")
        d.mkdir()
        sources = pathlist("wav_foo")
        [s.touch() for s in sources]
        self.check_args(f"{fmtpaths(sources)} {d}",
                sources=sources,
                wavs=sources,
                dest=d)

    def test_two_wavs_and_target(self):
        d = Path("dest_foo")
        d.mkdir()
        sources = pathlist("wav_foo1 wav_foo2")
        [s.touch() for s in sources]
        self.check_args(f"{fmtpaths(sources)} --target {d}",
                sources=sources,
                wavs=sources,
                dest=d)

    def test_wav_not_exist(self):
        d = Path("dest_foo")
        d.mkdir()
        sources = pathlist("wav_foo")
        self.check_args(f"{fmtpaths(sources)} {d}",
                "SOURCE_WAV not found")

    def test_progress_wav_not_dir(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo")
        path_to_source = d / p1 / source
        path_to_source.touch()
        self.check_args(f"{source} {d}",
                f"temp wavfile exists in progress dir but is not a directory! {path_to_source}")

    def test_progress_wav_src_link_not_found(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo")
        path_to_source = d / p1 / source
        path_to_source.mkdir()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.touch()
        self.check_args(f"{source} {d}",
                f"temp wavfile tracker is not a symlink! {linkback}")

    def test_progress_wav_src_link_to_wrong_file(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo")
        path_to_source = d / p1 / source
        path_to_source.mkdir()

        wrongsource = Path("foo")
        wrongsource.touch()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.symlink_to(wrongsource.resolve())
        self.check_args(f"{source} {d}",
                f"wav progress symlink resolves to a different file than the specified SOURCE_WAV file!")

    def test_progress_wav_src_link_to_wrong_file_wavext(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo.wav")
        path_to_source = d / p1 / source
        path_to_source.mkdir()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.symlink_to("foo")
        self.check_args(f"{source} {d}",
                f"wav progress symlink resolves to a different file than the specified SOURCE_WAV file!")

    def test_progress_wav_src_link_to_correct_file(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo")
        source.touch()
        path_to_source = d / p1 / source
        path_to_source.mkdir()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.symlink_to(source.resolve())
        self.check_args(f"{source} {d}",
                continue_from=d/p1,
                dest=d,
                sources=[source],
                wavs=[source])

    def test_progress_wav_src_link_to_correct_file_wavext(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo.wav")
        source.touch()
        path_to_source = d / p1 / source
        path_to_source.mkdir()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.symlink_to(source.resolve())
        self.check_args(f"{source} {d}",
                continue_from=d/p1,
                dest=d,
                sources=[source],
                wavs=[source])

    def test_progress_wav_only_good(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo.wav")
        source.touch()
        path_to_source = d / p1 / source
        path_to_source.mkdir()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.symlink_to(source.resolve())
        self.check_args(f"-c {d/p1}",
                continue_from=d/p1,
                dest=d,
                sources=[],
                wavs=[source.resolve()])

    def test_progress_wav_noexist(self):
        d = Path("dest_foo")
        d.mkdir()
        p1 = self.mkdir_progress("foo", d)
        source = Path("wav_foo.wav")
        path_to_source = d / p1 / source
        path_to_source.mkdir()
        linkback = path_to_source / taketake.Config.source_wav_linkname
        linkback.symlink_to(source.resolve())
        self.check_args(f"-c {d/p1}",
                continue_from=d/p1,
                dest=d,
                sources=[],
                wavs=[source.resolve()])

    def inject_dir_among_wavs(self, i):
        d = Path("dest_foo")
        d.mkdir()
        sdir = Path("sdir")
        sdir.mkdir()
        sources = pathlist("wav0 wav1 wav2")
        sources.insert(i, sdir)
        self.check_args(f"{fmtpaths(sources)} {d}",
                "When transfering from a whole directory,"
                " no other SOURCE_WAV parameters should be specified. *"
                r'\n *Found SOURCE_WAV directory: sdir *'
                r'\n *other SOURCE_WAVs: \[wav0 wav1 wav2\] *')

    def test_dir_among_wavs0(self):
        self.inject_dir_among_wavs(0)

    def test_dir_among_wavs1(self):
        self.inject_dir_among_wavs(1)

    def test_dir_among_wavs2(self):
        self.inject_dir_among_wavs(2)

    def test_dir_among_wavs3(self):
        self.inject_dir_among_wavs(3)

    def test_dir_among_wavs4(self):
        self.inject_dir_among_wavs(4)

    def test_multiple_missing_wavs(self):
        d = Path("dest_foo")
        d.mkdir()
        sources = pathlist("wav0 wav1 wav2")
        self.check_args(f"{fmtpaths(sources)} {d}",
                *(f"SOURCE_WAV not found: wav{i}" for i in range(3)))

    def test_progress_dir_nodir(self):
        self.check_args(f"-c nodir",
                "PROGRESS_DIR does not exist! Got: --continue nodir")

    def test_progress_dir_and_dest_nodir(self):
        self.check_args(f"a_dest_dir -c nodir",
                "PROGRESS_DIR does not exist! Got: --continue nodir",
                "--continue was specified, but so was DEST_PATH: a_dest_dir")


    def test_progress_dir_and_source_and_dest_nodir(self):
        self.check_args(f"foosrc foodest -c nodir",
                "--continue was specified, but so were SOURCE_WAVs: foosrc",
                "--continue was specified, but so was DEST_PATH: foodest",
                "PROGRESS_DIR does not exist! Got: --continue nodir",
                "temp wavfile exists in progress dir but is not a directory! nodir/foosrc")

    def test_duplicate_wavnames(self):
        self.check_args(f"a/1.wav b/1.wav c/2.wav c/2.wav dest",
                "Specified DEST_PATH does not exist!",
                *(["SOURCE_WAV not found"] * 3),
                r"Duplicate wavfiles names specified!"
                r" *\n *1.wav -> a/1.wav, b/1.wav"
                r" *\n *2.wav -> c/2.wav, c/2.wav",
                )

    def test_instrument_not_specified(self):
        d = Path("dest_foo")
        d.mkdir()
        self.cmdline_suffix = "" # Remove the default -i inst1 args
        self.check_args_with_prepended_src(f"{d}",
                r"No 'instrmnt.txt' file found in SOURCE_WAV directory '.'.")

    def test_instrument_in_file(self):
        inst = "model-3"
        d = Path("dest_foo")
        d.mkdir()
        instfpath = Path(taketake.Config.instrument_fname)
        instfpath.write_text(f" {inst}\n")
        self.cmdline_suffix = "" # Remove the default -i inst1 args
        self.check_args_with_prepended_src(f"{d}",
                dest=d,
                instrument=inst)

    def test_instrument_in_file_doesnt_match(self):
        inst = "model-3"
        d = Path("dest_foo")
        d.mkdir()
        instfpath = Path(taketake.Config.instrument_fname)
        instfpath.write_text(f" {inst}\n")
        self.check_args_with_prepended_src(f"{d}",
                f"Specified --instrument '{self.base_args['instrument']}' doesn't "
                f"match contents of '{instfpath}': '{inst}'")

    def test_no_act_arg(self):
        d = Path("dest_foo")
        d.mkdir()
        self.check_args_with_prepended_src(f"{d} -n",
                no_act=True,
                dest=d)

    def test_debug_arg(self):
        d = Path("dest_foo")
        d.mkdir()
        self.check_args_with_prepended_src(f"{d} -d",
                debug=True,
                dest=d)


class Test6_fallback_timestamp(TempdirFixture):
    def setUp(self):
        super().setUp()
        self.tempfile = Path(self.tempdir)/'foobarfile'

    def get_stamp(self, mode):
        return taketake.get_fallback_timestamp(self.tempfile, mode)

    def test_fallback_timestamp_passthrough(self):
        self.assertEqual(self.get_stamp("foo"), "foo")

    def test_fallback_timestamp_now(self):
        """This is a race condition; could mismatch if the second changes"""
        now = taketake.inject_timestamp("{}")
        self.assertEqual(self.get_stamp("now"), now)

    def test_fallback_timestamp_filetime_now(self):
        """This is a race condition; could mismatch if the second changes"""
        now = taketake.inject_timestamp("{}")
        self.tempfile.touch()
        for mode in "mca":
            with self.subTest(mode=mode):
                self.assertEqual(self.get_stamp(f"{mode}time"), now)

    def test_fallback_timestamp_filetime_atime_ctime(self):
        self.tempfile.touch()
        atime, mtime = 1698710698, 209687106
        os.utime(self.tempfile, times=(atime, mtime))
        for mode, seconds in (
                ("atime", atime),
                ("mtime", mtime),
                ):
            ts = taketake.inject_timestamp("{}",
                    when=datetime.datetime.fromtimestamp(seconds))
            with self.subTest(mode=mode, seconds=seconds, ts=ts):
                self.assertEqual(self.get_stamp(mode), ts)


class Test6_ext_commands_tempdir(TempdirFixture, FileAssertions):
    def test_timestamp_update(self):
        """Check our timestamp handling assumptions.

        Note that the read back from stat and ls may not have as much
        resolution as the original timestamp on some filesystems.  In that
        case, we may need to round the original timestamp a bit to get the
        test to pass, which is okay.
        """

        tfmt = taketake.Config.timestamp_fmt_with_seconds
        tstr = "20210526-131148-Wed"
        dt = datetime.datetime.strptime(tstr, tfmt)
        pstr = dt.strftime(tstr + " %z")

        fpath = self.tempfile("foo")
        with open(fpath, 'w') as f:
            print(f"{tfmt=}\n{tstr=}\n{dt=}\n{pstr=}", file=f)

        taketake.set_mtime(fpath, dt)

        # Check that stating the file from Python matches the timestamp
        mtime_after = os.stat(fpath).st_mtime
        dt_after = datetime.datetime.fromtimestamp(mtime_after)
        self.assertEqual(dt, dt_after)

        # Checking our assumptions that strftime round-trips the strptime
        tstr_after = dt_after.strftime(tfmt)
        self.assertEqual(tstr, tstr_after)

        # Check that ls agrees on the timestamp as well
        p = subprocess.run(("ls", "-l", "--time-style", "+" + tfmt, fpath),
                capture_output=True, text=True, check=True)
        self.assertRegex(p.stdout.strip(),
                fr" {tstr} {fpath}")


    def test_flac_decode_encode(self):
        wavpath = self.tempfile("test.wav")
        flacpath = f"{wavpath}.flac"
        wavpath2 = f"{flacpath}.wav"
        flacpath2 = f"{wavpath2}.flac"
        asyncio.run(taketake.flac_decode(testflacpath, wavpath))
        asyncio.run(taketake.flac_encode(wavpath, flacpath))
        asyncio.run(taketake.flac_decode(flacpath, wavpath2))
        asyncio.run(taketake.flac_encode(wavpath2, flacpath2))

        self.assertEqualFiles(wavpath, wavpath2)
        self.assertEqualFiles(flacpath, flacpath2)
        self.assertNotEqualFiles(wavpath, flacpath)

        wavsize = os.path.getsize(wavpath)
        self.assertEqual(wavsize, flacwavsize)
        self.assertGreater(wavsize/5, os.path.getsize(flacpath))

        wavtypestr = "RIFF (little-endian) data, WAVE audio, Microsoft PCM, 16 bit, stereo 44100 Hz"
        self.assertFileType(wavpath, wavtypestr)
        self.assertFileType(wavpath2, wavtypestr)
        self.assertFileType(flacpath,
                "FLAC audio bitstream data, 16 bit, stereo, 44.1 kHz, 472320 samples")


    def test_par2(self):
        wavpath = self.tempfile("test.wav")
        asyncio.run(taketake.flac_decode(testflacpath, wavpath))
        asyncio.run(taketake.par2_create(wavpath, 2, 5))

        #subprocess.run(("ls", "-al", os.path.dirname(wavpath)))
        asyncio.run(taketake.par2_verify(wavpath))

        # Punch a hole in the wav file to ensure the par2 verify now fails
        p = subprocess.run(("fallocate", "--punch-hole", "--offset", "4096", "--length", "4096", wavpath), check=True)
        with self.assertRaisesRegex(taketake.SubprocessError,
                f'(?s)Got bad exit code 1 from par2.*{wavpath}.* - damaged.*Repair is possible'):
            asyncio.run(taketake.par2_verify(wavpath))

        # Make sure par2 can also repair the file
        asyncio.run(taketake.par2_repair(wavpath))
        asyncio.run(taketake.par2_verify(wavpath))


    def test_flush(self):
        def get_cached_pages_for_flacfile():
            p = subprocess.run(("fincore", "-nb", testflacpath),
                    capture_output=True, text=True, check=True)
            bytes, pages, fsize, fname = p.stdout.split()
            return int(pages)

        with open(testflacpath, "rb") as f:
            data = f.read()

        self.assertGreater(len(data), 100000)

        num_pages_cached_pre = get_cached_pages_for_flacfile()
        self.assertGreater(num_pages_cached_pre, 1) # 67 4K pages
        taketake.flush_fs_caches(testflacpath)

        num_pages_cached_post = get_cached_pages_for_flacfile()
        self.assertEqual(num_pages_cached_post, 0)


class Test7_check_xdelta_basic(TempdirFixture, FileAssertions):
    runcount = 50

    def test_xdelta_good(self):
        xdelta = self.tempfile("test.xdelta")
        encode_xdelta(testflacpath, testflacpath, xdelta)
        self.check_xdelta(xdelta, flacsize, flacsize)
        return xdelta

    def test_xdelta_good_many_asyncio_loops(self):
        xdelta = self.test_xdelta_good()
        for x in range(self.runcount):
            asyncio.run(taketake.check_xdelta(xdelta, flacsize, flacsize))

    def test_xdelta_good_many_in_same_loop(self):
        xdelta = self.test_xdelta_good()
        async def check_xdelta_many():
            for x in range(self.runcount):
                await taketake.check_xdelta(xdelta, flacsize, flacsize)

        asyncio.run(check_xdelta_many())

    def test_xdelta_good_many_in_parallel(self):
        xdelta = self.test_xdelta_good()
        num_workers = 8
        max_checks = self.runcount
        num_checks = 0

        async def xdelta_checker():
            nonlocal num_checks
            while num_checks < max_checks:
                #await asyncio.sleep(0.0001)
                await taketake.check_xdelta(xdelta, flacsize, flacsize)
                num_checks += 1

        async def check_xdelta_many():
            tasks = []
            for i in range(num_workers):
                tasks.append(asyncio.create_task(xdelta_checker()))
            await asyncio.gather(*tasks)

        asyncio.run(check_xdelta_many())
        self.assertGreaterEqual(num_checks, max_checks)

class XdeltaStringBase(TempdirFixture, FileAssertions):
    teststrings = ["asnt3709oiznat2f-i.",
            "x"*min_xdelta_target_size_for_match,
            "\n"*min_xdelta_target_size_for_match,
            "a"*40,
            "b"*80,
            "the quick red fox jumped over the lazy brown dog"]

    def set_file_contents(self, fname, s):
        with open(fname, "w") as f:
            f.write(s)

    def assertXdeltaMatch(self, s):
        xdelta = self.tempfile("test.xdelta")
        source = self.tempfile("source")
        target = self.tempfile("target")

        self.set_file_contents(source, s)
        self.set_file_contents(target, s)

        encode_xdelta(source, target, xdelta)
        try:
            self.check_xdelta(xdelta, source, target)
        except taketake.XdeltaMismatch:
            raise AssertionError(f"Xdelta reports string mismatches itself: '{s}'")

    def assertXdeltaMismatch(self, source_string, target_string):
        xdelta = self.tempfile("test.xdelta")
        source = self.tempfile("source")
        target = self.tempfile("target")

        self.set_file_contents(source, source_string)
        self.set_file_contents(target, target_string)

        encode_xdelta(source, target, xdelta)
        with self.assertRaises(taketake.XdeltaMismatch):
            self.check_xdelta(xdelta, source, target)


class Test7_check_xdelta_string_match(XdeltaStringBase):
    def test_xdelta_fails_matching_short_strings(self):
        """The xdelta file contains the entirety of the data if it's small enough.

        In that case, there's no good way to determine if the file actually
        matches using just the xdelta.  It's best to compare the files
        manually in that case.
        """
        for i in range(min_xdelta_target_size_for_match):
            with self.subTest(i=i):
                self.assertXdeltaMismatch("x"*i, "x"*i)

    def test_xdelta_matching_strings(self):
        for s in self.teststrings:
            with self.subTest(s=s):
                self.assertXdeltaMatch(s)

class Test7_check_xdelta_string_mismatch(XdeltaStringBase):
    def test_xdelta_mismatch_vs_empty(self):
        for s in self.teststrings:
            with self.subTest(s=s, type="first_empty"):
                self.assertXdeltaMismatch("", s)
            with self.subTest(s=s, type="second_empty"):
                self.assertXdeltaMismatch(s, "")

    def test_xdelta_mismatch_vs_newline(self):
        for s in self.teststrings:
            with self.subTest(s=s, type="first_is_newline"):
                self.assertXdeltaMismatch("\n", s)
            with self.subTest(s=s, type="second_is_newline"):
                self.assertXdeltaMismatch(s, "\n")

    def test_xdelta_mismatch_vs_reverse(self):
        for i, s1 in enumerate(self.teststrings):
            s2 = self.teststrings[-i-1]
            if s1 != s2:
                with self.subTest(s1=s1, s2=s2):
                    self.assertXdeltaMismatch(s1, s2)

    def test_xdelta_mismatch_one_more_byte(self):
        for s in self.teststrings:
            with self.subTest(s=s, type="at_end_of_first"):
                self.assertXdeltaMismatch(s+"_", s)
            with self.subTest(s=s, type="at_start_of_first"):
                self.assertXdeltaMismatch("_"+s, s)
            with self.subTest(s=s, type="in_middle_of_first"):
                idx = len(s) // 2
                self.assertXdeltaMismatch(s[:idx]+"_"+s[idx:], s)

            with self.subTest(s=s, type="at_end_of_second"):
                self.assertXdeltaMismatch(s, s+"_")
            with self.subTest(s=s, type="at_start_of_second"):
                self.assertXdeltaMismatch(s, "_"+s)
            with self.subTest(s=s, type="in_middle_of_second"):
                idx = len(s) // 2
                self.assertXdeltaMismatch(s, s[:idx]+"_"+s[idx:])

    def test_xdelta_mismatch_one_changed_byte(self):
        for s in self.teststrings:
            with self.subTest(s=s, type="at_end_of_first"):
                self.assertXdeltaMismatch(s[:-1]+"_", s)
            with self.subTest(s=s, type="at_start_of_first"):
                self.assertXdeltaMismatch("_"+s[1:], s)
            with self.subTest(s=s, type="in_middle_of_first"):
                idx = len(s) // 2
                self.assertXdeltaMismatch(s[:idx-1]+"_"+s[idx:], s)

            with self.subTest(s=s, type="at_end_of_second"):
                self.assertXdeltaMismatch(s, s[:-1]+"_")
            with self.subTest(s=s, type="at_start_of_second"):
                self.assertXdeltaMismatch(s, "_"+s[1:])
            with self.subTest(s=s, type="in_middle_of_second"):
                idx = len(s) // 2
                self.assertXdeltaMismatch(s, s[:idx-1]+"_"+s[idx:])


class Test7_xdelta_flac_decoder(unittest.TestCase, FileAssertions):
    """Test taketake's wrapping of xdelta3.

    Each test corrupts a wav file in some way, then the tearDown checks it can
    be repaired.  Failures will be reported from tearDown() - this design
    keeps the code clean, though it doesn't strictly adhere to the philosophy
    of unit testing.
    """

    pagesize = 4096

    @classmethod
    def setUpClass(cls):
        """Create a long-lived tempdir and decode the flac into it."""
        timestamp = time.strftime("%Y%m%d-%H%M%S-%a")
        cls.main_tempdir = tempfile.mkdtemp(
                prefix=f'{cls.__name__}.{timestamp}.')
        cls.wavpath_src = os.path.join(cls.main_tempdir, "src.wav")

        asyncio.run(taketake.flac_decode(testflacpath, cls.wavpath_src))
        cls.wavsize = os.path.getsize(cls.wavpath_src)

        cls.wavpath_md5 = cls.wavpath_src + ".md5"
        make_md5sum_file(cls.wavpath_src, cls.wavpath_md5)

    @classmethod
    def tearDownClass(cls):
        cleandir(cls.main_tempdir)


    def setUp(self):
        """Copy the class fixture's wav into a test specific test dir"""
        self.test_tempdir = os.path.join(self.main_tempdir, self._testMethodName)
        os.mkdir(self.test_tempdir)

        self.wavpath_test = os.path.join(self.test_tempdir, "test.wav")
        self.wavpath_test_md5 = self.wavpath_test + ".md5"

        shutil.copyfile(self.wavpath_src, self.wavpath_test)
        make_md5sum_file(self.wavpath_test, self.wavpath_test_md5)

    def tearDown(self):
        """Verify the original wav was not corrupted, then that the test
        corrupted the copied wav, then verify it can be repaired
        """

        # Verify the original decoded wav was not corrupted
        self.assertMd5FileGood(self.wavpath_md5)

        # Verify the test's copy of the wav was indeed corrupted
        self.assertMd5FileBad(self.wavpath_test_md5)

        # Generate an xdelta patch to the stdout of the decoded flac,
        # using the corrupted wav file as the source
        wavpath_test_xdelta = self.wavpath_test + ".xdelta"
        self.gen_xdelta_from_flac(testflacpath, self.wavpath_test, wavpath_test_xdelta)

        # Ensure that check_xdelta() discovers that the files mismatch
        with self.assertRaises(taketake.XdeltaMismatch):
            self.check_xdelta(wavpath_test_xdelta, self.wavsize, self.wavpath_test)

        # Apply the xdelta patch to the corrupted wav file to generate a
        # repaired wav file
        wavpath_repaired = os.path.join(self.test_tempdir, "repaired.wav")
        p = subprocess.run(("xdelta3", "-d", "-s", self.wavpath_test,
            wavpath_test_xdelta, wavpath_repaired),
            capture_output=True, text=True, check=True)
        self.assertEqual(p.stdout, "")
        self.assertIn(p.stderr, ["", "xdelta3: warning: output window 0 does not copy source\n"])

        # Check that the repaired wav equals the src wav
        self.assertEqualFiles(self.wavpath_src, wavpath_repaired)

        # Generate a new xdelta for the repaired wav and check it matches
        wavpath_repaired_xdelta = wavpath_repaired + ".xdelta"
        self.gen_xdelta_from_flac(testflacpath, wavpath_repaired, wavpath_repaired_xdelta)
        self.check_xdelta(wavpath_repaired_xdelta, self.wavsize, wavpath_repaired)

        #subprocess.run(("xdelta3", "printdelta", wavpath_test_xdelta))
        cleandir(self.test_tempdir)


    def fallocate(self, cmd):
        """Call fallocate with the given command on the test wavfile"""
        # Unfortunately fallocate --posix doesn't work - the file remains the
        # same as before
        subprocess.run(("fallocate", *cmd.split(), self.wavpath_test),
                check=True)

    def test_insert_1st_page(self):
        self.fallocate("--insert-range --offset 0 --length 4096")

    def test_insert_2nd_page(self):
        self.fallocate("--insert-range --offset 4096 --length 4096")

    def test_remove_1st_page(self):
        self.fallocate("--collapse-range --offset 0 --length 4096")

    def test_remove_2nd_page(self):
        self.fallocate("--collapse-range --offset 4096 --length 4096")

    def test_truncate_file(self):
        newsize = 0
        os.truncate(self.wavpath_test, newsize)
        self.assertEqual(os.path.getsize(self.wavpath_test), newsize)

    def test_truncate_to_1_byte(self):
        newsize = 1
        os.truncate(self.wavpath_test, newsize)
        self.assertEqual(os.path.getsize(self.wavpath_test), newsize)

    def test_truncate_to_1_page(self):
        newsize = self.pagesize
        os.truncate(self.wavpath_test, newsize)
        self.assertEqual(os.path.getsize(self.wavpath_test), newsize)

    def test_truncate_last_byte(self):
        newsize = self.wavsize - 1
        os.truncate(self.wavpath_test, newsize)
        self.assertEqual(os.path.getsize(self.wavpath_test), newsize)

    def test_truncate_last_page(self):
        newsize = self.wavsize - self.pagesize
        os.truncate(self.wavpath_test, newsize)
        self.assertEqual(os.path.getsize(self.wavpath_test), newsize)

    def test_corrupt_first_byte(self):
        with open(self.wavpath_test, "rb+") as f:
            f.seek(0, os.SEEK_SET)
            f.write(b"x")

    def test_corrupt_early_byte(self):
        with open(self.wavpath_test, "rb+") as f:
            f.seek(1516, os.SEEK_SET)
            f.write(b"x")

    def test_corrupt_last_byte(self):
        with open(self.wavpath_test, "rb+") as f:
            f.seek(-1, os.SEEK_END)
            f.write(b"x")

    def test_corrupt_near_end_byte(self):
        with open(self.wavpath_test, "rb+") as f:
            f.seek(-12986, os.SEEK_END)
            f.write(b"x")

    def test_add_byte(self):
        with open(self.wavpath_test, "ab") as f:
            f.write(b"x")

# File corruption automation:
# dd if=/dev/zero of=filepath bs=1 count=1024 seek=2048 conv=notrunc
# see https://unix.stackexchange.com/q/222359
#
# Or use fallocate:
#   --punch-hole to deallocate blocks (effectively replacing them with zeros),
#       making the file more sparse
#   --collapse-range to cut out blocks or any range of bytes
#   --insert-range to inject blocks of zeros, shifting everything after
#   --posix to force the operation even if it would be inefficient due
#       to lack of underlying filesystem support.

#===========================================================================
# File processing integration tests
#===========================================================================

class Test8_tasks(unittest.IsolatedAsyncioTestCase, CdTempdirFixture, FileAssertions):
    """Test task processing in taketake"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #self.maxDiff=None

    @unittest.skipUnless(dontskip, "Takes 1s per wav")
    async def test_empty_runtasks(self):
        taketake.Config.act = False
        dest = Path("dest_foo")
        dest.mkdir()
        src = Path("src")
        src.mkdir()

        wavpaths = []
        for w in "take1.wav take2.wav".split():
            wavpaths.append(src/w)
            await taketake.flac_decode(testflacpath, wavpaths[-1])
        # Note: this hits that dratted loop deprecation warning.
        # To suppress it, we'd need to make this function non-async
        # so that the warning ignore can be set up properly.
        #taketake.Config.debug = True
        await taketake.run_tasks(args=argparse.Namespace(
                continue_from=None,
                dest=Path("dest_foo"),
                wavs=wavpaths,
                instrument="foobar",
                do_prompt=False,
            ))

    @unittest.skipUnless(dontskip, "Takes 0.75s per subtest")
    def test_process_speech(self):
        for start, duration, expect in [
                (1, 3.2, 'twenty twenty monday march eight'),
                (2, 5.5, 'a twenty monday march eighteenth two thousand twenty one'),
                ]:
            r = taketake.TimeRange(start, duration)
            with self.subTest(r=r):
                s = taketake.process_speech(testflacpath, r)
                self.assertEqual(s, expect)

    @unittest.skipUnless(dontskip, "Takes 1s per subtest")
    def test_extract_timestamp_from_audio(self):
        audioinfo = taketake.AudioInfo(duration_s=30)
        taketake.extract_timestamp_from_audio(Path(testflacpath), audioinfo)
        audioinfo_expect = taketake.AudioInfo(
            duration_s=30,
            extra_speech=[],
            parsed_timestamp=datetime.datetime(2021, 3, 18, 20, 20),
            recognized_speech='twenty twenty monday march eighteenth two thousand twenty one',
            speech_range={'duration': 4.507420000000001, 'start': 1.64045},
            )
        self.assertDataclassesEqual(audioinfo, audioinfo_expect)


    def test_find_audio_span(self):
        # TODO our flac is too short for scan-to to work
        for scan_to, start, duration in [
                (0, 1.640, 4.507),
                (6, 1.640, 4.507),
                (10, 1.640, 4.507),
                ]:
            expect = taketake.TimeRange(start, duration)
            r = taketake.find_likely_audio_span(testflacpath, scan_to)
            for endpoint in dataclasses.fields(taketake.TimeRange):
                with self.subTest(scan_to=scan_to, endpoint=endpoint.name):
                    self.assertAlmostEqual(
                            getattr(r, endpoint.name),
                            getattr(expect, endpoint.name),
                            places=3)

class DummyStepper:
    def __init__(self, tokens=None):
        if tokens is None:
            tokens=[]
        elif isinstance(tokens, int):
            tokens=list(range(tokens))
            tokens.append(None)
        self.tokens = list(reversed(tokens))
        self.output = []
        self.loglist = []

    async def get(self):
        return self.tokens.pop()

    async def put(self, token):
        self.output.append(token)

    def log(self, *args, sep=" "):
        self.loglist.append(sep.join(args))


class StepSetupBase(unittest.IsolatedAsyncioTestCase,
        CdTempdirFixture, FileAssertions):

    def setUp(self):
        super().setUp()
        td = Path(self.tempdir)
        self.srcdir = td/"src"
        self.srcdir.mkdir()
        self.destdir = td/"dest"
        self.destdir.mkdir()
        self.wavpaths = [self.srcdir/w for w in ("w1.wav", "w2.wav")]
        self.cmdargs = argparse.Namespace(
                continue_from=None,
                dest=self.destdir,
                wavs=self.wavpaths,
                do_prompt=False,
                instrument="foobaz",
        )
        self.stepper = DummyStepper()
        self.maxDiff = None

    def mk_xinfo(self, wpath, progress_dir):
        return taketake.TransferInfo(
                source_wav=wpath,
                wav_abspath=Path(os.path.abspath(wpath)),
                dest_dir=self.destdir,
                wav_progress_dir=progress_dir / wpath.name,
                source_link=progress_dir / wpath.name
                    / taketake.Config.source_wav_linkname,
                instrument="foobaz",
            )


class Test8_step_setup(StepSetupBase):
    """Common infrastructure for testing steps"""

    async def do_step_setup_test(self):
        worklist = []
        progress_dir = self.cmdargs.continue_from
        if not progress_dir:
            progress_dir = self.destdir / taketake.inject_timestamp(
                    taketake.Config.progress_dir_fmt)

        await taketake.Step.setup(self.cmdargs, worklist, self.stepper)

        with self.subTest(phase="check_stepper"):
            self.assertEqual(self.stepper.output,
                    list(range(len(self.wavpaths))) + [None])

        with self.subTest(phase="progress_dir"):
            if taketake.Config.act:
                self.assertIsDir(progress_dir)
            else:
                self.assertNoFile(progress_dir)

        for i, wpath in enumerate(self.wavpaths):
            expected_xinfo = self.mk_xinfo(wpath, progress_dir)
            w = wpath.name

            with self.subTest(i=i, w=w, phase="worklist-check"):
                self.assertDataclassesEqual(worklist[i], expected_xinfo)

            with self.subTest(i=i, w=w, phase="wav_progress_dir"):
                if taketake.Config.act:
                    self.assertIsDir(expected_xinfo.wav_progress_dir)
                else:
                    self.assertNoFile(expected_xinfo.wav_progress_dir)

            with self.subTest(i=i, w=w, phase="source_link"):
                if taketake.Config.act:
                    self.assertSymlinkTo(expected_xinfo.source_link,
                            expected_xinfo.wav_abspath)
                else:
                    self.assertNoFile(expected_xinfo.source_link)

    async def test_step_setup_act_no_progressdir(self):
        await self.do_step_setup_test()

    async def test_step_setup_no_act_no_progressdir(self):
        self.cmdargs.no_act = True
        taketake.Config.act = False
        await self.do_step_setup_test()

    async def test_step_setup_act_with_progressdir(self):
        progress_dir = self.destdir / "a_mocked_out_progress_dir"
        progress_dir.mkdir()
        self.cmdargs.continue_from = progress_dir
        await self.do_step_setup_test()

    async def test_step_setup_no_act_with_progressdir(self):
        self.cmdargs.no_act = True
        taketake.Config.act = False
        progress_dir = self.destdir / "a_mocked_out_progress_dir"
        self.cmdargs.continue_from = progress_dir
        await self.do_step_setup_test()


class Test8_step_listen(StepSetupBase):
    def setUp(self):
        super().setUp()
        self.num_wavs = 12

        self.wavpaths = [self.srcdir/f"w{w}.wav" for w in range(self.num_wavs)]
        self.stepper = DummyStepper(len(self.wavpaths))
        self.progress_dir = self.destdir / taketake.inject_timestamp(
                taketake.Config.progress_dir_fmt)
        self.progress_dir.mkdir()

        self.worklist = []
        for w in self.wavpaths:
            self.worklist.append(self.mk_xinfo(w, self.progress_dir))
            self.worklist[-1].wav_progress_dir.mkdir()

    async def run_and_check_step_listen(self, ai_expect=flacaudioinfo):
        #taketake.Config.debug = True
        await taketake.Step.listen(self.cmdargs, self.worklist, stepper=self.stepper)
        # Ensure all the dumped AudioInfo are as expected
        for xinfo in self.worklist:
            with self.subTest(w=xinfo.source_wav.name):
                self.assertDataclassesEqual(xinfo.audioinfo, ai_expect)
                loaded_ai = taketake.read_json(xinfo.wav_progress_dir / taketake.Config.audioinfo_fname)
                self.assertDataclassesEqual(loaded_ai, ai_expect)

        self.assertEqual(len(self.stepper.output), self.num_wavs+1)
        self.assertEqual(set(self.stepper.output), set(range(self.num_wavs)) | set([None]))

    @unittest.skipUnless(dontskip, "Takes 4s for 12 wavs with 6 workers.")
    async def test_step_listen_recognize(self):
        await taketake.flac_decode(testflacpath, self.wavpaths[0])
        for w in self.wavpaths[1:]:
            w.symlink_to(self.wavpaths[0])
        await self.run_and_check_step_listen()

    async def test_step_listen_load_json(self):
        for xinfo in self.worklist:
            taketake.write_json(xinfo.wav_progress_dir / taketake.Config.audioinfo_fname,
                    flacaudioinfo)
        await self.run_and_check_step_listen()

    async def test_step_listen_wrong_dataclass_type(self):
        bad_aifile_index = 3
        for xinfo in self.worklist:
            taketake.write_json(xinfo.wav_progress_dir
                    / taketake.Config.audioinfo_fname,
                    flacaudioinfo)
        taketake.write_json(self.worklist[bad_aifile_index].wav_progress_dir
                / taketake.Config.audioinfo_fname,
                self.worklist[bad_aifile_index])
        with self.assertRaisesRegex(taketake.InvalidProgressFile,
                rf"listen_to_wav\({self.wavpaths[bad_aifile_index].name}\)\[{bad_aifile_index}\] "
                rf"got unexpected data from {taketake.Config.audioinfo_fname}"
                rf"\n.*"
                rf"\n *Dump: TransferInfo"
                ):
            await self.run_and_check_step_listen()

    @unittest.skipUnless(dontskip, "Takes 4s for 12 wavs with 6 workers.")
    async def test_step_listen_bad_timestamp(self):
        p = subprocess.run(("ffmpeg", "-i", testflacpath, "-ss", "3", "-t", "2",
            self.wavpaths[0]), capture_output=True, text=True)
        for w in self.wavpaths[1:]:
            w.symlink_to(self.wavpaths[0])
        await self.run_and_check_step_listen(taketake.AudioInfo(duration_s=2.0))


class Test8_step_reorder(unittest.IsolatedAsyncioTestCase,
        CdTempdirFixture, FileAssertions):

    def setUp(self):
        super().setUp()
        self.numitems = 10

    def init_worklist(self):
        self.worklist = [taketake.TransferInfo(
                source_wav=f"w{i}.wav",
                wav_abspath=f"w{i}.wav",
                dest_dir=Path(),
                wav_progress_dir=Path(),
                source_link=Path(),
                audioinfo=taketake.AudioInfo(),
                instrument="foobuzz",
                )
            for i in range(self.numitems)]

    def timestamp(self, i):
        self.worklist[i].audioinfo.parsed_timestamp=datetime.datetime.fromtimestamp(i)

    async def check(self, expected_order):
        self.stepper = DummyStepper(len(self.worklist))
        await taketake.Step.reorder(cmdargs=None, worklist=self.worklist, stepper=self.stepper)
        self.assertEqual(self.stepper.output, list(expected_order) + [None])

    async def test_step_reorder_empty_worklist(self):
        self.worklist = []
        await self.check([])

    async def test_step_reorder_one_token_no_timestamp(self):
        self.numitems = 1
        self.init_worklist()
        await self.check([0])

    async def test_step_reorder_one_token_with_timestamp(self):
        self.numitems = 1
        self.init_worklist()
        self.timestamp(0)
        await self.check([0])

    async def test_step_reorder_all_timestamps(self):
        self.init_worklist()
        for i in range(len(self.worklist)):
            self.timestamp(i)
        await self.check(range(self.numitems))

    async def test_step_reorder_missing_timestamp_at_i(self):
        for i in range(1, self.numitems):
            with self.subTest(i=i):
                self.init_worklist()
                for j in range(self.numitems):
                    if j == i:
                        continue
                    self.timestamp(j)
                await self.check(range(self.numitems))

    async def test_step_reorder_timestamps_start_at_i(self):
        for i in range(0, self.numitems):
            with self.subTest(i=i):
                self.init_worklist()
                for j in range(self.numitems):
                    if j < i:
                        continue
                    self.timestamp(j)

                    expect = list(range(i, -1, -1))
                    expect.extend(range(i+1, self.numitems))
                    await self.check(expect)


    async def test_step_reorder_timestamp_only_at_i(self):
        for i in range(self.numitems):
            with self.subTest(i=i):
                self.init_worklist()
                self.timestamp(i)

                expect = list(range(i, -1, -1))
                expect.extend(range(i+1, self.numitems))
                await self.check(expect)

if __name__ == '__main__':
    unittest.main()
