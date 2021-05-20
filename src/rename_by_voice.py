#!/usr/bin/env python3

# TODO - timestamp word parsing, renaming, file touch

"""Rename file[s] based on the spoken information at the front of the file.

When using TalkyTime to timestamp a recording, this eases management of the
recorded files.

Supports .wav and .flac.

Setup:
    $ pip3 install --user SpeechRecognition
    $ pip3 install --user PocketSphinx
    $ pip3 install --user word2number

Silence detection:
    $ ffmpeg -i in.flac -af silencedetect=noise=-50dB:d=1 -f null -
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


import sys
import subprocess
import re
import speech_recognition
import itertools
from word2number import w2n

class Config:
    silence_threshold_dbfs = -55    # Audio above this threshold is not considered silence
    silence_min_duration_s = 0.5    # Silence shorter than this is not detected
    file_scan_duration_s = 120      # -t (time duration).  Note -ss is startseconds
    min_talk_duration_s = 2.5         # Only consider non-silence intervals longer than this for timestamps

    ffmpeg_cmd = "ffmpeg"
    ffmpeg_silence_filter = "silencedetect=noise={threshold}dB:d={duration}" # precede with -af
    ffmpeg_silence_extra_args = "-f null -".split()

    duration_cmd = "ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1".split()

#silence_cmd = "ffmpeg -af silencedetect=noise=-50dB:d=1 -f null - -i".split()

def detect_silence(f, threshold_dbfs=-50):
    """Use ffmpeg silencedetect to find all silent segments.

    Return a list of (start, duration) pairs."""
    args = [Config.ffmpeg_cmd]
    args.append("-af")
    args.append(Config.ffmpeg_silence_filter.format(threshold = Config.silence_threshold_dbfs,
                                                    duration = Config.silence_min_duration_s))
    args.extend(Config.ffmpeg_silence_extra_args)
    args.append("-t")
    args.append(str(Config.file_scan_duration_s))
    args.append("-i")
    args.append(f)

    res = subprocess.run(args, capture_output=True, universal_newlines=True)
    detected_lines = [line for line in res.stderr.splitlines() if line.startswith('[silencedetect')]
    offsets = [float(line.split()[-1]) for line in detected_lines if "silence_start" in line]
    durations = [float(line.split()[-1]) for line in detected_lines if "silence_end" in line]
    return list(zip(offsets, durations))


def invert_silences(silences, file_scan_duration_s):
    """Return a list of (start, duration) pairs of spans that are not silence."""
    non_silences = []
    prev_silence_end = 0.0

    # Add on an entry starting at the file_scan_duration_s to catch any
    # non-silent end bits.
    for start, duration in itertools.chain(silences, ([file_scan_duration_s, 0.0],)):
        if start > prev_silence_end:
            non_silences.append((prev_silence_end, start - prev_silence_end))
        prev_silence_end = start + duration

    return non_silences


def find_likely_audio_span(f):
    """Searches the file f for regions of silence.

    Returns the start offset and duration in seconds of the first span of
    non-silent audio that is long enough.

    Returns None if no likely candidate was found.
    """

    silences = detect_silence(f)
    #print("Silences:")
    #for s in silences: print(f"{s[0]} - {s[0] + s[1]} ({s[1]}s duration)")

    non_silences = invert_silences(silences, Config.file_scan_duration_s)
    #print("Non silences:")
    #for s in non_silences: print(f"{s[0]} - {s[0] + s[1]} ({s[1]}s duration)")

    for start, duration in non_silences:
        if duration >= Config.min_talk_duration_s:
            return (start, duration)
    else:
        return None
    #raise RuntimeError(f"Could not find any span of audio greater than {Config.min_talk_duration_s}s in file '{f}'")


def speech_to_text(f, offset, duration):
    """Uses the PocketSphinx speech recognizer to decode the spoken timestamp
    and any notes.

    Returns the resulting text, or None if no transcription could be determined.
    """
    recognizer = speech_recognition.Recognizer()
    with speech_recognition.AudioFile(f) as audio_file:
        speech_recording = recognizer.record(audio_file, offset=offset, duration=duration)
    try:
        return recognizer.recognize_sphinx(speech_recording)
    except speech_recognition.UnknownValueError as e:
        return None


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
    corrections = {"why": "one"}
    ordinals = reverse_hashify(
        "zeroth    first    second  third      fourth     fifth     sixth     seventh     eighth     ninth "
        "tenth     eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth "
        "twentieth 21st     22nd    23rd       24th       25th      26th      27th        28th       29th "
        "thirtieth")
    ordinal_suffixes = reverse_hashify("th st nd rd")


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
    for word in opt_words.split():
        if word_list and word_list[0] == word:
            word_list.pop(0)


def grok_digit_pair(word_list):
    """Parse the given 1 or 2 digit doublet of timey numbers"""
    value = 0
    pop_optional_words(word_list, "oh")
    if word_list:
        next_num = to_num(word_list[0])
        if next_num is not None:
            value = next_num
            word_list.pop(0)
            if word_list and (value == 0 or value >= 20):
                next_num = to_num(word_list[0])
                if next_num < 10:
                    value += next_num
                    word_list.pop(0)
    #print(" * got", value)
    return value


def grok_time_words(word_list):
    """Returns a triplet of (hour, minutes, seconds) from the word_list"""

    # Parse hours
    hours = grok_digit_pair(word_list)
    pop_optional_words(word_list, "hundred hours oh clock oclock o'clock")

    # Parse minutes
    minutes = grok_digit_pair(word_list)
    pop_optional_words(word_list, "oh clock oclock o'clock minutes and")

    # Parse seconds
    seconds = grok_digit_pair(word_list)
    pop_optional_words(word_list, "seconds")

    return hours, minutes, seconds


def words_to_timestamp(text):
    """Converts the given text to a feasible timestamp, followed by any
    remaining comments or notes encoded in the time string.

    Returns a pair of (Datetime, str) containing the timestamp and any comments.
    """
    # Sample recognized text for this TalkyTime setup:
    #   format:  ${hour}:${minute}, ${weekday}. ${month} ${day}, ${year}
    #   example: 19:38, Wednesday. May 19, 2021
    #
    #"zero oh one wednesday may nineteenth twenty twenty one", "00:01"
    #"zero fifty one wednesday may nineteenth twenty twenty one", "00:51"
    #"zero hundred wednesday may nineteenth twenty twenty one", "00:00"
    #"five oh clock wednesday may nineteenth twenty twenty one", "05:00"
    #"five oclock wednesday may nineteenth twenty twenty one", "05:00"
    #"zero five wednesday may nineteenth twenty twenty one", "00:05"
    #"five oh five wednesday may nineteenth twenty twenty one", "05:05"
    #"nineteen hundred wednesday may nineteenth twenty twenty one", "19:00"
    #"nineteen hundred hours wednesday may nineteenth twenty twenty one"
    #"twenty twenty monday march eighteenth two thousand twenty why"
    #"eleven fifteen sunday march twenty first two thousand twenty one"
    #"thirteen hundred hours sunday march twenty first two thousand twenty one"
    #"twenty one forty one thursday march twenty fifth two thousand twenty one"

    words = text.split()

    time_words = []
    day_of_week = None
    month = 0
    date_words = []

    # Find the day of week name
    # This separates the timestamp from the month, day, and year
    for i, word in enumerate(words):
        if word in TimestampWords.days:
            time_words = words[:i]
            day_of_week = words[i]
            month = TimestampWords.months[words[i+1]] + 1
            date_words = words[i+2:]
            break

    print(f"  Time: {time_words}\n  Day: {day_of_week}\n  Month: {month}\n  Day,Year: {date_words}")

    hours, minutes, seconds = grok_time_words(time_words)
    print(f"-> {hours:02d}:{minutes:02d}:{seconds:02d}")


def process_file(f):
    print(f"Scanning '{f}'")
    span = find_likely_audio_span(f)

    if span is not None:
        start, duration = span
        print(f"Parsing {duration:.2f}s of audio starting at offset {start:.2f}s in '{f}'...")
        text = speech_to_text(f, start, duration)
        if text is not None:
            print(f'"{text}"')
            words_to_timestamp(text)  # TODO returns Datetime, str
        else:
            print("No speech found")
    else:
        print("No likely span of audio found")

    print()


def main():
    for f in sys.argv[1:]:
        process_file(f)

    return 0

if __name__ == "__main__":
    sys.exit(main())
