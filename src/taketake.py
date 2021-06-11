#!/usr/bin/env python3.9

# TODO - duration, renaming, touch, flac conversion, par2, copy, verify

"""Rename file[s] based on the spoken information at the front of the file.

When using TalkyTime to timestamp a recording, this eases management of the
recorded files.

Supports .wav and .flac.

Setup:
    $ python3.9 -m pip install --user SpeechRecognition PocketSphinx word2number prompt_toolkit

Silence detection:
    $ ffmpeg -i in.flac -af silencedetect=noise=-50dB:d=1 -f null -

par2 parity archive creation:
    $ par2 create -s4096 -r5 -n2 -u in.flac

Run tests:
    $ tests/test_rename_by_voice.py
"""

# Silence detection example:
#
#...
#Input #0, flac, from 'in.flac':
#  Duration: 01:00:45.08, start: 0.000000, bitrate: 279 kb/s
#    Stream #0:0: Audio: flac, 44100 Hz, stereo, s16
#Stream mapping:
#  Stream #0:0 -> #0:0 (flac (native) -> pcm_s16le (native))
#Press [q] to stop, [?] for help
#Output #0, null, to 'pipe:':
#  Metadata:
#    encoder         : Lavf58.45.100
#    Stream #0:0: Audio: pcm_s16le, 44100 Hz, stereo, s16, 1411 kb/s
#    Metadata:
#      encoder         : Lavc58.91.100 pcm_s16le
#[silencedetect @ 0x564be015b400] silence_start: 0
#[silencedetect @ 0x564be015b400] silence_end: 9.67576 | silence_duration: 9.67576
#[silencedetect @ 0x564be015b400] silence_start: 14.4735
#[silencedetect @ 0x564be015b400] silence_end: 60.8099 | silence_duration: 46.3364
#[silencedetect @ 0x564be015b400] silence_start: 194.373
#[silencedetect @ 0x564be015b400] silence_end: 199.932 | silence_duration: 5.55898
#...

# par2 considerations:
#   $ par2 create -s4096 -r5 -n2 -u in.flac
#   -> makes two par2 volumes of equal size, each 5% of the full size, using 4096b blocks
#   (then remove in.flac.par2 - it's redundant with the vol par2)
#   -> Want block size in multiples of 4096 to match disk blocks
#   -> But there is a limit of how many blocks: probably 32K of them for the full file
#   -> par2 exits with non-zero code if it has an error

# flush FS caches for copy verification:
# $ sync
# $ echo 3 > /proc/sys/vm/drop_caches
# https://linuxhint.com/clear_cache_linux/
# But we need root...
# sudo sh -c "/bin/echo 3 > /proc/sys/vm/drop_caches"
#
# -> To get around needing the admin password for this:
# $ visudo /etc/sudoers.d/drop_caches
# YOURUSERNAME     ALL = NOPASSWD: /sbin/sysctl vm.drop_caches=3
# $ sudo /sbin/sysctl vm.drop_caches=3

import asyncio
import time
import sys
import os
import itertools
import subprocess
import datetime
from dataclasses import dataclass, field
from typing import List

import speech_recognition
from word2number import w2n
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings

class Config:
    silence_threshold_dbfs = -55    # Audio above this threshold is not considered silence
    silence_min_duration_s = 0.5    # Silence shorter than this is not detected
    file_scan_duration_s = 90       # -t (time duration).  Note -ss is startseconds
    min_talk_duration_s = 2.5       # Only consider non-silence intervals longer than this for timestamps
    max_talk_duration_s = 15        # Do recognition on up to this many seconds of audio at most
    talk_attack_s = 0.2             # Added to the start offset to avoid clipping the start of talking
    talk_release_s = 0.2            # Added to the duration to avoid clipping the end of talking
    epsilon_s = 0.01                # When comparing times, consider +/- epsilon_s the same

    timestamp_fmt_no_seconds   = "%Y%m%d-%H%M-%a"
    timestamp_fmt_with_seconds = "%Y%m%d-%H%M%S-%a"

class ExtCmdListMeta(type):
    def __getattr__(cls, name):
        return cls.cmds[name]

class ExtCmd(metaclass=ExtCmdListMeta):
    """Class for describing external commands."""
    cmds = {}

    def __init__(self, name, doc, template, **kwargs):
        self.name = name
        self.doc = doc
        self.template = template
        self.params = kwargs
        ExtCmd.cmds[name] = self

    def construct_args(self, **kwargs):
        """Returns a list of parameters constructed from the kwargs injected into the command template."""
        return [arg.format(**kwargs) for arg in self.template.split()]


ExtCmd(
    "ffmpeg_silence_detect",
    "Detects spans of silence in a media file.",
    "ffmpeg -t {length} -i {file} -af silencedetect=noise={threshold}dB:d={duration} -f null -",

    file="The media files to process",
    length="Number of seconds to process, starting at 0 or -ss",
    threshold="dBfs decibels below which the audio is considered silent",
    duration="The minimum duration in seconds for a span to remain below the threshold for the span to be considered silence.",
)

ExtCmd(
    "get_media_duration",
    "Returns duration of the given file in seconds as a float.",
    "ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {file}",

    file="The media file to query",
)

ExtCmd(
    "play_media_file",
    "Launch an interactive GUI app to play the media file starting at the first non-silence.",
    "mpv --osd-level=3 --osd-duration=3600000 --osd-playing-msg='{file}\\n->{suggestion}' --player-operation-mode=pseudo-gui --loop=inf {file} --start={start}",

    file="The media file to play",
    suggestion="The suggested filename for renaming",
    start="Starting time in seconds (float) for the first non-silence",
)

ExtCmd(
    "flac_encode",
    "Encodes wav file to flac.",
    "flac --replay-gain {infile} -o {outfile}",

    infile="The input wav file",
    outfile="The output flac file",
)

ExtCmd(
    "flac_decode",
    "Decodes flac file to wav file.",
    "flac -d {infile} -o {outfile}",

    infile="The input flac file",
    outfile="The output wav file",
)

ExtCmd(
    "par2_create",
    "Constructs par2 volume files for the given file.",
    "par2 create -s{blocksize} -r{redundance} -n{numfiles} -u {infile}",

    infile="The input file to generate par2 volumes for",
    blocksize="The number of bytes for each block.  Multiples of 4K is good for disks.",
    redundance="The percent file size to target for each par2 file.",
    numfiles="Number of par2 volume files to generate.",
)

ExtCmd(
    "par2_verify",
    "Verifies the file(s) covered by the given par2 file.",
    "par2 verify {parfile}",

    parfile="The par2 file to check",
)


@dataclass
class TimeRange:
    start: float
    duration: float


# Exceptions
class InvalidMediaFile(RuntimeError):
    pass

#============================================================================
# Audio file processing
#============================================================================

def flac_encode(wav_fpath, flac_fpath):
    #flac --preserve-modtime 7d29t001.WAV -o 7d29t001.flac
    pass

def flac_decode(flac_fpath, wav_fpath):
    pass

def par2_create(f, num_par2_files, percent_redundancy):
    pass

def par2_verify(f):
    pass

def flush_fs():
    pass

def set_mtime():
    # os.utime( dt.timestamp )
    pass

async def get_file_duration(fpath):
    """Use ffprobe to determine how many seconds the file identified by fpath plays for."""
    args = ExtCmd.get_media_duration.construct_args(file=fpath)

    proc = await asyncio.create_subprocess_exec(*args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

    (stdout, stderr) = await proc.communicate()

    def exmsg():
        return f" from '{' '.join(args)}':\n  stdout: '{stdout}'\n  stderr: '{stderr}'"

    if proc.returncode:
        raise InvalidMediaFile(f"Got exit {proc.returncode} {exmsg()}")

    if stderr:
        raise InvalidMediaFile(f"Got extra stderr from '{' '.join(args)}':"
                f"\n  stdout: '{stdout}'"
                f"\n  stderr: '{stderr}'")

    try:
        duration = float(stdout)
    except ValueError as e:
        raise InvalidMediaFile(f"Could not parse duration stdout from '{' '.join(args)}':\n  '{stdout}'") from e

    return duration


async def detect_silence(fpath):
    """Use ffmpeg silencedetect to find all silent segments.

    Return a list of TimeRange objects identifying the spans of silence."""

    args = ExtCmd.ffmpeg_silence_detect.construct_args(
            file=fpath,
            length=Config.file_scan_duration_s,
            threshold=Config.silence_threshold_dbfs,
            duration=Config.silence_min_duration_s)

    proc = await asyncio.create_subprocess_exec(*args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

    (stdout, stderr) = await proc.communicate()

    def exmsg():
        return f" from '{' '.join(args)}':\n  stderr: '{stderr}'"

    if proc.returncode:
        raise InvalidMediaFile(f"Got exit {proc.returncode} {exmsg()}")

    detected_lines = [line for line in stderr.splitlines() if line.startswith(b'[silencedetect')]

    offsets = [float(line.split()[-1]) for line in detected_lines if b"silence_start" in line]
    durations = [float(line.split()[-1]) for line in detected_lines if b"silence_end" in line]

    return list(TimeRange(start, duration) for start, duration in zip(offsets, durations))


def invert_silences(silences, file_scan_duration_s):
    """Return a list of TimeRange objects that represent non-silence.

    file_scan_duration_s should be capped at the actual duration of the file
    to avoid spurious extra ranges being added at the end.
    """

    non_silences = []
    prev_silence_end = 0.0

    # Add on an entry starting at the file_scan_duration_s to catch any
    # non-silent end bits.
    for r in itertools.chain(silences, (TimeRange(file_scan_duration_s, 0.0),)):
        if r.start > prev_silence_end + Config.epsilon_s:
            non_silences.append(TimeRange(prev_silence_end, r.start - prev_silence_end))
        prev_silence_end = r.start + r.duration

    return non_silences


class NoSuitableAudioSpan(RuntimeError):
    pass

async def find_likely_audio_span(finfo, file_scan_duration_s):
    """Searches the first file_scan_duration_s seconds of the file represented
    by finfo for regions of silence.

    Fills in the silences, non_silences, and speech_range fields of finfo.

    The speech_range field is set to the TimeRange of the first non-silent
    span of audio that is considered long enough, expanded a bit to cover any
    attack or decay in the speech.

    Raises NoSuitableAudioSpan if no likely candidate was found.
    """

    finfo.silences = await detect_silence(finfo.fpath)

    finfo.non_silences = invert_silences(finfo.silences, file_scan_duration_s)

    for r in finfo.non_silences:
        duration = r.duration
        if duration >= Config.min_talk_duration_s:
            # Expand the window a bit to allow for attack and decay below the
            # silence threshold.
            r.start = max(0, r.start - Config.talk_attack_s)
            duration += Config.talk_attack_s + Config.talk_release_s
            duration = min(duration, Config.max_talk_duration_s)
            finfo.speech_range = TimeRange(r.start, duration)
            return

    raise NoSuitableAudioSpan(f"Could not find any span of audio greater than "
                              f"{Config.min_talk_duration_s}s in file '{f}'")


#============================================================================
# Speech recognition and parsing
#============================================================================

def process_speech(finfo):
    """Uses the PocketSphinx speech recognizer to decode the spoken timestamp
    and any notes.

    Returns the resulting text, or None if no transcription could be determined.
    """
    recognizer = speech_recognition.Recognizer()

    with speech_recognition.AudioFile(finfo.fpath) as audio_file:
        speech_recording = recognizer.record(audio_file,
                                             offset=finfo.speech_range.start,
                                             duration=finfo.speech_range.duration)
    try:
        finfo.orig_speech = recognizer.recognize_sphinx(speech_recording)
    except speech_recognition.UnknownValueError as e:
        pass


def reverse_hashify(s):
    """Splits the given string s and returns a hash mapping the words to their
    word position in the string"""

    d = {}
    for i, word in enumerate(s.split()):
        d[word] = i
    return d


class TimestampWords:
    days = reverse_hashify("sunday monday tuesday wednesday thursday friday saturday sunday")
    months = reverse_hashify("january february march april may june july august september october november december")
    corrections = {"why": "one", "oh": "zero"}
    ordinals = reverse_hashify(
        "zeroth    first    second  third      fourth     fifth     sixth     seventh     eighth     ninth "
        "tenth     eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth "
        "twentieth 21st     22nd    23rd       24th       25th      26th      27th        28th       29th "
        "thirtieth")
    ordinal_suffixes = reverse_hashify("th st nd rd")


class TimestampGrokError(RuntimeError):
    pass


def to_num(word):
    if word in TimestampWords.corrections:
        word = TimestampWords.corrections[word]

    try:
        return w2n.word_to_num(word)
    except ValueError:
        return None


def pop_optional_words(word_list, opt_words):
    """Pops off the given words in the order specified, skipping those that
    aren't present.

    Arg word_list is a list of words being parsed.
    Arg opt_words is a space-separated string of words to consider.
    """
    popped = []
    for word in opt_words.split():
        if word_list and word_list[0] == word:
            popped.append(word_list.pop(0))

    return " ".join(popped)


def grok_digit_pair(word_list):
    """Parses the given 1 or 2 digit doublet of timey numbers.

    If no number is found, the list is not modified and 0 is returned.
    This allows for datestamps with missing timestamps.
    """
    value = 0
    if word_list:
        next_num = to_num(word_list[0])
        if next_num is not None:
            value = next_num
            word_list.pop(0)
            if word_list and (value == 0 or value >= 20):
                next_num = to_num(word_list[0])
                if next_num is not None and next_num < 10:
                    value += next_num
                    word_list.pop(0)
    #print(" * got", value)
    return value


def grok_time_words(word_list):
    """Returns a triplet of (hour, minutes, seconds, extra) from the word_list

    The final list contains any unparsed words."""

    done = False

    # Parse hour
    hour = grok_digit_pair(word_list)
    if pop_optional_words(word_list, "second seconds"):
        second = hour
        hour = 0
        done = True

    if not done and pop_optional_words(word_list, "minute minutes"):
        minute = hour
        hour = 0
        pop_optional_words(word_list, "and")

    else:
        pop_optional_words(word_list, "hundred hour hours oh clock oclock o'clock and")

        # Parse minute
        minute = grok_digit_pair(word_list)
        if pop_optional_words(word_list, "second seconds"):
            second = minute
            minute = 0
            done = True
        else:
            pop_optional_words(word_list, "oh clock oclock o'clock minute minutes and")

    if not done:
        # Parse seconds
        second = grok_digit_pair(word_list)
        pop_optional_words(word_list, "second seconds")

    return hour, minute, second, list(word_list)


def grok_day_of_month(word_list):
    """Pop out the day of month from the word_list and return the resulting int.

    The final word popped will be an ordinal type, like first, second, twentieth.
    If such a word isn't found, None is returned and no words are popped.
    """

    day = None
    idx = 0
    if not word_list:
        raise TimestampGrokError(f"word_list is empty, no day of month found")

    day = to_num(word_list[idx])
    if day is None:
        # Assume the word is probably an "Nth"-style ordinal
        # and allow adding the "Nth" for the case where day <= 20
        day = 0
    else:
        idx += 1

    if len(word_list) > idx and word_list[idx] in TimestampWords.ordinals:
        day += TimestampWords.ordinals[word_list[idx]]
        idx += 1
    else:
        raise TimestampGrokError(f"Could not find Nth-like ordinal in {' '.join(word_list)}")

    # Sanity check the day
    if day < 1 or day > 31:
        raise TimestampGrokError(f"Parsed month day {day} from '{' '.join(word_list[:idx])}' is out of range")

    # Success, pop the words we used
    for i in range(idx):
        word_list.pop(0)

    return day


def grok_year(word_list):
    """Pop out the year from the word_list and return the resulting int.

    We expect a year in the 19xx-2999 range.
    Otherwise the word_list is not modified and None is returned.
    """

    year = None
    idx = 0

    def cur_word():
        return word_list[idx] if len(word_list) > idx else None

    year = to_num(cur_word())
    if year is None:
        raise TimestampGrokError(f"Could not find year in '{' '.join(word_list)}'")

    idx += 1
    if 1 <= year <= 3:
        # need a "thousand"
        if cur_word() == "thousand":
            idx += 1
            year *= 1000
        else:
            raise TimestampGrokError(f"Expected 'thousand' after {year} parsing year from '{' '.join(word_list)}'")

        if cur_word() == "and":
            idx += 1

        # parse hundreds or digit pair
        num = to_num(cur_word())
        if num is not None:
            idx += 1
            # could be hundreds, 10s, or ones
            if num < 10:
                # could be the final digit, or followed by "hundred"
                if cur_word() == "hundred":
                    idx += 1
                    year += num * 100
                    if cur_word() == "and":
                        idx += 1

                    # tens and ones
                    num = to_num(cur_word())
                    if num is not None:
                        idx += 1
                        year += num
                        num = to_num(cur_word())
                        if num is not None and num < 10:
                            idx += 1
                            year += num

                else:
                    year += num

            elif 10 <= num < 20:
                year += num

            elif num < 30:
                year += num
                num = to_num(cur_word())
                if num is not None and num < 10:
                    idx += 1
                    year += num

            else:
                pass # probably this is not a year digit.  Like for 2000

    elif 19 <= year <= 29:
        # Parse as a pair-of-digit-doublets style year (e.g. "twenty twenty one")
        num = to_num(cur_word())
        if year > 19 and num is not None and num < 10:
            idx += 1
            year += num

        year *=100
        more_required = True
        if cur_word() == "hundred":
            idx += 1
            more_required = False
        if cur_word() == "and":
            idx += 1

        # parse digit pair
        num = to_num(cur_word())
        if num is not None:
            idx += 1

            if num == 0 or 10 <= num < 30:
                year += num
                num = to_num(cur_word())
                if num is not None and num < 10:
                    idx += 1
                    year += num

            else:
                year += num

        elif more_required:
            raise TimestampGrokError(f"Year parse error: missing second doublet after {year} in '{' '.join(word_list)}'")

    # Sanity check the year
    if year is not None and (year < 1900 or year > 2999):
        raise TimestampGrokError(f"Parsed year {year} from '{' '.join(word_list[:idx])}' is out of range")

    # Success, pop the words we used
    for i in range(idx):
        word_list.pop(0)

    return year


def grok_date_words(word_list):
    """Parses out the (year, month, day, and day_of_week)"""
    year, month, day, day_of_week = (None,) * 4

    # Optional Day-of-week might come first
    if word_list and word_list[0] in TimestampWords.days:
        day_of_week = word_list.pop(0)

    if word_list and word_list[0] in TimestampWords.months:
        month = TimestampWords.months[word_list.pop(0)] + 1
    else:
        assert False, f"Should have found a month name in '{' '.join(word_list)}'"

    # Parse day-of-month:
    day = grok_day_of_month(word_list)

    # Optional Day-of-week might come in between the monthday and the year
    if word_list and word_list[0] in TimestampWords.days:
        day_of_week = word_list.pop(0)

    # Parse the year
    year = grok_year(word_list)

    if day_of_week is not None:
        # Sanity check that the day of week lines up with the year/month/day
        date = datetime.date(year=year, month=month, day=day)
        calc_weekday = date.strftime("%A").lower()
        if calc_weekday != day_of_week:
            print(f"*** Warning: Calculated weekday '{calc_weekday}'"
                  f" doesn't match parsed weekday '{day_of_week}'")

    return year, month, day, day_of_week, list(word_list)


def words_to_timestamp(text):
    """Converts the given text to a feasible timestamp, followed by any
    remaining comments or notes encoded in the time string.

    Returns a pair of (datetime, str) containing the timestamp and any comments.
    """
    # Sample recognized text for this TalkyTime setup:
    #   format:  ${hour}:${minute}, ${weekday}. ${month} ${day}, ${year}
    #   example: 19:38, Wednesday. May 19, 2021

    if text is None:
        raise TimestampGrokError(f"Given text is None")

    words = text.split()

    time_words = []
    day_of_week = None
    month = 0
    date_words = []

    # Find the day of week name or the month name
    # This separates the timestamp from the month, day, and year
    for i, word in enumerate(words):
        if word in TimestampWords.days or word in TimestampWords.months:
            time_words = words[:i]
            date_words = words[i:]
            break
    else:
        raise TimestampGrokError(f"Failed to find a month name in '{text}'")

    #print(f"  Time: {time_words}")
    hour, minute, second, extra = grok_time_words(time_words)
    #print(f"-> {hour:02d}:{minute:02d}:{second:02d} (extra: {extra})")

    #print(f"  Date: {date_words}")
    year, month, day, day_of_week, extra = grok_date_words(date_words)
    #print(f"-> {year}-{month}-{day} {day_of_week} (extra: {extra})")
    #print(f"-> {year:04d}-{month:02d}-{day:02d} {day_of_week} (extra: {extra})")

    return datetime.datetime(year, month, day, hour, minute, second), extra


#============================================================================
# File processing
#============================================================================

async def process_timestamp_from_audio(finfo):
    """Returns a timestamp and extra string data from the beginning speech in f.

    Raises NoSuitableAudioSpan or TimestampGrokError if processing fails.
    """

    # Only scan the first bit of the file to avoid transfering a lot of data.
    # This means we can prompt the user for any corrections sooner.
    scan_duration = min(finfo.duration_s, Config.file_scan_duration_s)

    await find_likely_audio_span(finfo, scan_duration)
    print(f"Speechinizer: {finfo.orig_filename!r} - processing {finfo.speech_range.duration:.2f}s "
          f"of audio starting at offset {finfo.speech_range.start:.2f}s")
    await asyncio.to_thread(process_speech, finfo)
    finfo.parsed_timestamp, finfo.extra_speech = words_to_timestamp(finfo.orig_speech)


def fmt_duration(duration):
    """Returns a string of the form XhYmZs given a duration in seconds.

    The duration s is first rounded to the nearest second.
    If any unit is 0, omit it, except if the duration is zero return 0s.
    """

    parts = []
    duration = round(duration)     # now an int

    # The unit_map dict maps unit names to their multiple of the next unit
    # The final unit's multiple must be None.
    unit_map = dict(s=60, m=60, h=None)

    # To include days:
    #unit_map = dict(s=60, m=60, h=24, d=None)

    # To include milliseconds, multiply duration by 1000 prior to rounding.
    # Its probably better to just do decimal seconds instead.
    #unit_map = dict(ms=1000, s=60, m=60, h=None)

    for unit, multiple in unit_map.items():
        if multiple is None:
            value = duration
        else:
            value = duration % multiple
            duration //= multiple  # int division
        if value or (not parts and duration == 0):
            parts.append(f"{value}{unit}")

    parts.reverse()
    return ''.join(parts)


async def process_file_speech(finfo):
    """Process the file from the given FileInfo parameter, filling in the
    fields duration_s, silences, non_silences, speech_range, orig_speech,
    parsed_timestamp, extra_speech, and suggested_filename.
    """
    finfo.duration_s = await get_file_duration(finfo.fpath)
    #print(f"Listening for timestamp info in '{f}' ({file_duration:.2f}s)")
    try:
        await process_timestamp_from_audio(finfo)

        # Format the timestamp
        if finfo.parsed_timestamp.second:
            time_fmt = Config.timestamp_fmt_with_seconds
        else:
            time_fmt = Config.timestamp_fmt_no_seconds
        tstr = finfo.parsed_timestamp.strftime(time_fmt)

        # Format the duration
        dstr = fmt_duration(finfo.duration_s)

        # Format the notes
        if finfo.extra_speech:
            notes = "-".join(finfo.extra_speech) + "."
        else:
            notes = ""

        finfo.suggested_filename = f"{finfo.instrument}.{tstr}.{notes}{dstr}.{finfo.orig_filename}"
        print(f"Speechinizer: {finfo.orig_filename!r} - {finfo.orig_speech!r} -> {finfo.suggested_filename!r}")

    except (NoSuitableAudioSpan, TimestampGrokError) as e:
        # Couldn't find any timestamp info
        finfo.suggested_filename = finfo.orig_filename


#============================================================================
# async processing of files and prompting for rename
#============================================================================

def play_media_file(finfo):
    """Use ffmpeg silencedetect to find all silent segments.

    Return a list of (start, duration) pairs."""

    start = 0
    if finfo.speech_range is not None:
        start = finfo.speech_range.start

    args = ExtCmd.play_media_file.construct_args(file=finfo.fpath,
                                                 start=start,
                                                 suggestion=finfo.suggested_filename)

    null = subprocess.DEVNULL
    res = subprocess.Popen(args, stdin=null, stdout=null, stderr=null)


@dataclass
class FileInfo:
    """Class tracking speech recognition and file renaming"""
    instrument: str

    fpath: str
    orig_filename: str
    src_path: str
    dest_path: str

    duration_s: float = None
    silences: List[TimeRange] = field(default_factory=list)
    non_silences: List[TimeRange] = field(default_factory=list)
    speech_range: TimeRange = None

    orig_speech: str = None
    parsed_timestamp: str = None
    extra_speech: str = None

    suggested_filename: str = None
    final_filename: str = None


import random # TODO remove, only for testing


async def waiter(name, f):
    delay = random.uniform(0.2, 1.0)
    print(f"{name}: '{f.orig_filename}' - {delay}s")
    await asyncio.sleep(delay)
    print(f"{name}: '{f.orig_filename}' - done")


async def speech_recognizer(files, recognizer2prompter_speech_guesses):
    for finfo in files:
        await process_file_speech(finfo)
        recognizer2prompter_speech_guesses.put_nowait(finfo)


async def filename_prompter(recognizer2prompter_speech_guesses, prompter2par_dest_names):

    def toolbar():
        return HTML(f"  <style bg='ansired'>{time.monotonic()}</style>")

    bindings = KeyBindings()
    @bindings.add('escape', 'h')
    def _(event):
        play_media_file(finfo)

    style = Style.from_dict(dict(
        prompt="#eeeeee bold",
        fname="#bb9900",
        comment="#9999ff",
        guess="#dddd11 bold",
        final="#33ff33 bold",
        ))

    session = PromptSession(key_bindings=bindings)
    while finfo := await recognizer2prompter_speech_guesses.get():
        with patch_stdout():
            finfo.final_filename = await session.prompt_async(HTML(
                    f"<prompt>* Confirm file rename for</prompt> <fname>{finfo.fpath}</fname>\n <guess>Guess</guess>: <fname>{finfo.suggested_filename}</fname> "
                    f"<comment>({len(finfo.suggested_filename)} characters)</comment>\n <final>Final&gt;</final> "),
                    style=style,
                    default=finfo.suggested_filename,
                    mouse_support=True,
                    bottom_toolbar=None, auto_suggest=AutoSuggestFromHistory())
        prompter2par_dest_names.put_nowait(finfo)
        recognizer2prompter_speech_guesses.task_done()

async def flac_encoder(files, flac2par_completions):
    for f in files:
        await waiter("FlacEncoder", f)
        flac2par_completions.put_nowait(f)

async def renamer_and_par_generator(prompter2par_dest_names,
                                    flac2par_completions,
                                    par2processor_completions):
    while destname := await prompter2par_dest_names.get():
        await waiter("Renamer", destname)
        prompter2par_dest_names.task_done()
        flac = await flac2par_completions.get()
        await waiter("ParGenerator", flac)
        par2processor_completions.put_nowait(flac)


async def process_wavs_from_usb(filepaths, dest):
    """Process wav files, encoding them to renamed files in the dest path.
    """
    # Construct the FileInfo objects
    files = [FileInfo(instrument="test",
                      fpath=f,
                      orig_filename=os.path.basename(f),
                      src_path=os.path.dirname(f),
                      dest_path=dest)
             for f in filepaths]

    # Set up asyncio queues of files between the various worker processes

    # The speech recognizer finds the first span of non-silent audio, passes
    # it through PocketSphinx, and attempts to parse a timestamp and comments
    # from the results.
    recognizer2prompter_speech_guesses = asyncio.Queue()

    # The Prompter asks the user for corrections on those guesses and passes
    # the results to the renamer/par2-generator
    prompter2par_dest_names = asyncio.Queue()

    # Meanwhile, the flac encoder copies the wav data while encoding it to the
    # destination as a temporary file
    flac2par_completions = asyncio.Queue()

    # Finally, the par processor queues the files back to the coordinator for
    # completion tracking.
    par2processor_completions = asyncio.Queue()

    recognizer_task = asyncio.create_task(speech_recognizer(
        files,
        recognizer2prompter_speech_guesses))

    prompter_task = asyncio.create_task(filename_prompter(
        recognizer2prompter_speech_guesses,
        prompter2par_dest_names))

    flac_task = asyncio.create_task(flac_encoder(
        files,
        flac2par_completions))

    rename_and_par_task = asyncio.create_task(renamer_and_par_generator(
        prompter2par_dest_names,
        flac2par_completions,
        par2processor_completions))

    # Wait for all files to be completely processed
    time_start = time.monotonic()
    completions = 0
    while completions < len(files):
        done_file = await par2processor_completions.get()
        print(f"Processor: '{done_file}' completed processing.")
        par2processor_completions.task_done()
        completions += 1
    time_end = time.monotonic()
    print(f"Processor: done, {time_end-time_start}s.")

    # Cancel tasks
    recognizer_task.cancel()
    prompter_task.cancel()
    flac_task.cancel()
    rename_and_par_task.cancel()
    print(f"Processor: canceled tasks.")

    # Await the cancelations to complete
    await asyncio.gather(
        recognizer_task,
        prompter_task,
        flac_task,
        rename_and_par_task,
        return_exceptions=False)

    print(f"Processor: gathered tasks.")


def main():
    files = sys.argv[1:]
    asyncio.run(process_wavs_from_usb(files, dest=None))

    return 0

if __name__ == "__main__":
    sys.exit(main())
