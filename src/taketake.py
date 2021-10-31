#!/usr/bin/env python3

# TODO support transfering from flac files?

"""Transfer wav files into flac files.

This is meant for convenient but robust download from digitial piano USB drives.

* Suggest file names based on spoken timestamp information in each wav
* Prompt the user to rename the file, allowing listening to it via Alt-h
* Encode par2 recovery files for the destination flac
* Flush cached data and verify the copied/encoded contents
* Automatically delete the source wav file
* Copy the encoded flac and par2 files back onto the USB for archival
* If the process hits an error or is interrupted, it can be resumed

When using TalkyTime to timestamp a recording, this eases management of the
recorded files.

During Rename, press alt-h to hear the file via the configured media player.

Setup:

 $ python3 -m pip install --user SpeechRecognition PocketSphinx word2number prompt_toolkit

External tools required:
* flac
* par2
* mpv (for auditioning files to check speech recognition)
* ffmpeg
* xdelta3
"""


# Silence detection example:
# $ ffmpeg -i in.flac -af silencedetect=noise=-50dB:d=1 -f null -
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

import argparse
import asyncio
import time
import sys
import os
import glob
import itertools
import collections
import subprocess
import datetime
import types
import ctypes
from dataclasses import dataclass, field
from typing import List
from pathlib import Path

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
    act = True
    debug = False
    dbg_prog = "taketake"
    prog = sys.argv[0]

    silence_threshold_dbfs = -55    # Audio above this threshold is not considered silence
    silence_min_duration_s = 0.5    # Silence shorter than this is not detected
    file_scan_duration_s = 90       # -t (time duration).  Note -ss is startseconds
    min_talk_duration_s = 2.5       # Only consider non-silence intervals longer than this for timestamps
    max_talk_duration_s = 15        # Do recognition on up to this many seconds of audio at most
    talk_attack_s = 0.2             # Added to the start offset to avoid clipping the start of talking
    talk_release_s = 0.2            # Added to the duration to avoid clipping the end of talking
    epsilon_s = 0.01                # When comparing times, consider +/- epsilon_s the same
    par2_base_blocksize = 4096      # A multiple of this is used to avoid the 32K limit
    par2_max_num_blocks = 10000     # par2 doesn't support more than 32K num blocks, but gets unweildy with a lot of blocks anyway, so limit things a bit

    timestamp_fmt_no_seconds   = "%Y%m%d-%H%M-%a"
    timestamp_fmt_with_seconds = "%Y%m%d-%H%M%S-%a"

    wav_extensions = "wav WAV"
    progress_dir_fmt = ".taketake.{}.tmp"
    source_wav_linkname = ".source.wav"
    interrupted_flac_fmt = ".interrupted-abandoned.{}.flac"
    guess_fname = ".filename_guess"
    provided_fname = ".filename_provided"
    dest_fname_fmt = "{prefix}.{datestamp}.{notes}{duration}.{orig_fname}"
    xdelta_fname = ".xdelta"

@dataclass
class TimeRange:
    start: float
    duration: float


# Exceptions
class TaketakeRuntimeError(RuntimeError):
    pass

class SubprocessError(TaketakeRuntimeError):
    pass

class InvalidMediaFile(TaketakeRuntimeError):
    pass

class MissingPar2File(TaketakeRuntimeError):
    pass

class TimestampGrokError(TaketakeRuntimeError):
    pass

class NoSuitableAudioSpan(TaketakeRuntimeError):
    pass

class XdeltaMismatch(TaketakeRuntimeError):
    pass

#============================================================================
# External command infrastructure
#============================================================================

# Python 3.9.7 has an issue where asyncio.subprocess internally loses the
# deprecated loop keyword in some async calls.  This squelches the warning.
# DeprecationWarning: The loop argument is deprecated since Python 3.8, and scheduled for removal in Python 3.10.
# https://bugs.python.org/issue45097
#
# These warnings show up when running unittest because it explicitly enables
# warnings by prepending a new filter when running the tests.
#
# One can ignore all warnings, but that will cover future issues:
#       unittest.main(warnings='ignore')
#
# Note we wan't use this around unittest.main() because the TestRunner
# prepends non-ignore filters:
#
#        with warnings.catch_warnings():
#            warnings.filterwarnings("ignore",
#                    message="The loop argument is deprecated since Python 3.8",
#                    category=DeprecationWarning)
async def communicate(p, *args, **kwargs):
    """Call p.communicate with the given args.

    Stuff the resulting stdout and stderr bytes objects into new
    stdout_data and stderr_data attributes of the given p object.
    """
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore",
                message="The loop argument is deprecated since Python 3.8",
                category=DeprecationWarning)
        p.stdout_data, p.stderr_data = await p.communicate(*args, **kwargs)


class ExtCmdListMeta(type):
    """Allow access to instances of derived classes by lookup through the
    derived class's cmds dict.

        E.g.: DerivedClass.foo translates to DerivedClass.cmds["foo"]

    The derived class should inject its instances into the cmds dict itself:

        DerivedClass.cmds[name] = self
    """

    def __getattr__(cls, name):
        return cls.cmds[name]


class ExtCmd(metaclass=ExtCmdListMeta):
    """Collect external commands for simple documentation and execution.

    Example:
        proc = await ExtCmd.get_media_duration.run_fg(file=fpath)
    """
    cmds = {}

    def __init__(self, name, doc, template, **kwargs):
        self.name = name
        self.doc = doc
        self.template = template
        self.params = kwargs
        ExtCmd.cmds[name] = self

    def construct_args(self, **kwargs):
        """Returns a list of parameters constructed from the kwargs injected into the command template."""
        kwarg_set = set(kwargs.keys())
        cmd_arg_set = set(self.params.keys())
        if kwarg_set != cmd_arg_set:
            raise RuntimeError(f"Got invalid parameters to {self.name}"
                    f"\n  Given: {kwargs}"
                    f"\n  Expected: {self.params.keys()}")
        return [arg.format(**kwargs) for arg in self.template.split()]

    async def exec_async(self, _stdin=None, _stdout=None, _stderr=None, **kwargs):
        args = self.construct_args(**kwargs)

        proc = await asyncio.create_subprocess_exec(*args,
                stdin=_stdin, stdout=_stdout, stderr=_stderr)

        def mlfmt(b):
            lines = b.decode().splitlines()
            return "\n    ".join(lines)

        def exmsg():
            return f"from {args[0]}\n  cmd: '{' '.join(args)}'\n" \
                    f"  stdout:\n    {mlfmt(proc.stdout_data)}\n" \
                    f"  stderr:\n    {mlfmt(proc.stderr_data)}"

        proc.args = args
        proc.exmsg = exmsg
        proc.stdout_data = repr(proc.stdout)
        proc.stderr_data = repr(proc.stderr)

        return proc

    async def run_fg(self, **kwargs):
        proc = await self.exec_async(
                _stdout=asyncio.subprocess.PIPE,
                _stderr=asyncio.subprocess.PIPE,
                **kwargs)

        await communicate(proc)

        if proc.returncode:
            raise SubprocessError(f"Got bad exit code {proc.returncode} {proc.exmsg()}")

        return proc


#============================================================================
# External command configuration
#============================================================================

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
    "flac_decode_stdout",
    "Decodes flac file to stdout for streaming into something else.",
    "flac -c -d {infile}",

    infile="The input flac file",
)

ExtCmd(
    "xdelta_encode_from_source",
    "Encode an xdelta file to stdout from the given source using the stdin as the dest.",
    "xdelta3 -s {source}",

    source="Input source file to generate the xdelta from",
)

ExtCmd(
    "xdelta_printdelta",
    "Print the delta represented by the given xdelta file",
    "xdelta3 printdelta {xdelta}",

    xdelta="xdelta file to print the delta for",
)

ExtCmd(
    "par2_create",
    "Constructs par2 volume files for the given file.",
    "par2 create -s{blocksize} -r{redundance} -n{numfiles} -u {infile}",

    infile="""The input file to generate par2 volumes for.
        Note the par2 files will be created in the same directory as the given file.""",
    blocksize="The number of bytes for each block.  Multiples of 4K is good for disks.",
    redundance="The percent of the original file size to target for each par2 file.",
    numfiles="""Number of par2 volume files to generate.
        This doesn't include the basic .par2 file, which can be deleted to
        reduce clutter since the vol*.par2 files contain the same information.
        Note we use the -u argument so each vol*.par2 file will be the same size.""",
)

ExtCmd(
    "par2_verify",
    "Verifies the file(s) covered by the given par2 file.",
    "par2 verify -q {file}",

    file="""The file to check; can be a .par2 file, a .vol*.par2 file,
        or a file for which {file}.par2 exists.""",
)

ExtCmd(
    "par2_repair",
    "Repairs the file(s) covered by the given par2 file.",
    "par2 repair -q {file}",

    file="""The file to check; can be a .par2 file, a .vol*.par2 file,
        or a file for which {file}.par2 exists.""",
)


#============================================================================
# External command implementation
#============================================================================

async def flac_encode(wav_fpath, flac_fpath):
    #flac --preserve-modtime 7d29t001.WAV -o 7d29t001.flac
    proc = await ExtCmd.flac_encode.run_fg(infile=wav_fpath, outfile=flac_fpath)
    #print(f"Encoded to {flac_fpath}:", proc.stderr_data.decode())


async def flac_decode(flac_fpath, wav_fpath):
    proc = await ExtCmd.flac_decode.run_fg(infile=flac_fpath, outfile=wav_fpath)
    #print(f"Decoded to {wav_fpath}:", proc.stderr_data.decode())


async def get_flac_wav_size(flac_file):
    """Decode the flac file to count the number of bytes of the resulting wav.

    Does not actually write the wav to disk.
    """
    read_into_wc, write_from_flac = os.pipe()

    p_flacdec = await ExtCmd.flac_decode_stdout.exec_async(
            infile=flac_file,
            _stdout=write_from_flac,
            _stderr=asyncio.subprocess.DEVNULL)
    os.close(write_from_flac)  # Allow flac to get a SIGPIPE if wc exits

    p_wc = await asyncio.create_subprocess_exec("wc", "-c",
            stdin=read_into_wc,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    os.close(read_into_wc)
    await communicate(p_wc)

    if p_wc.returncode:
        raise SubprocessError(f"Got bad exit code {p_wc.returncode} from wc")
    if p_wc.stderr_data:
        raise SubprocessError(f"Got unexpected stderr from wc: '{p_wc.stderr_data.decode()}'")

    await p_flacdec.wait()
    if p_flacdec.returncode:
        raise SubprocessError(f"Got bad exit code {p_flacdec.returncode} from flac")

    return int(p_wc.stdout_data.decode().strip())


async def encode_xdelta_from_flac_to_wav(flac_file, wav_file, xdelta_file):
    """Encode an xdelta_file from the wav_file to the decoded flac_file.

    This results in an xdelta that can repair the wav decoded from the given
    flac_file to match the contents read from the wav_file.

    In the normal context of taketake copying files from a USB drive, the
    wav_file will be being read a second time from USB.  This would result in
    different contents than what was read the first time while encoding into
    the flac file if there are data issues with the USB drive.

    The xdelta_file represents the differences between the two separate read
    attempts from the USB drive.

    Runs flac -c -d flac_file | xdelta3 -s wav_file > xdelta_file

    Return (flac, xdelta) Process functions"""
    with open(xdelta_file, "wb") as f:
        # asyncio subprocess uses StreamReader for asyncio.subprocess.PIPE,
        # so we need to create a pipe manually to link up the subprocesses.
        # See https://stackoverflow.com/a/36666420
        read_into_xdelta, write_from_flac = os.pipe()

        p_flacdec = await ExtCmd.flac_decode_stdout.exec_async(
                infile=flac_file,
                _stdout=write_from_flac,
                _stderr=asyncio.subprocess.DEVNULL)
        os.close(write_from_flac)  # Allow flac to get a SIGPIPE if xdelta exits

        p_xdelta = await ExtCmd.xdelta_encode_from_source.exec_async(
                source=wav_file,
                _stdin=read_into_xdelta,
                _stdout=f,
                _stderr=asyncio.subprocess.DEVNULL)
        os.close(read_into_xdelta)

        await p_xdelta.wait()
        await p_flacdec.wait()
        return p_flacdec, p_xdelta


async def check_xdelta(xdelta_file, expected_size, target_size):
    """Raise XdeltaMismatch if the given xdelta file shows a difference.

    Warning: if the target file size is smaller than the source, then xdelta
    will simply encode a copy for the number of bytes in the target, and the
    xdelta file will appear to express a match if that size is passed in as
    the expected_size!  Thus it is imperative that both expected_size and
    target_size be passed in for checking.

    Note: this fails for matching files smaller than 18 bytes, where xdelta
    just encodes the contents of the target file directly into the xdelta
    file.

    When the source and dest files match during an xdelta encode, the
    resulting xdelta files will contain a single CPY_0 command instructing
    xdelta to reconstruct the target file by simply copying the entirety of
    the source file.

    When they do not match, additional instructions are required to patch the
    source file into the state of the target file.

    Thus, this function reads only the header and first few instructions,
    checking for the following parameters:

        VCDIFF copy window length:    22670
        VCDIFF copy window offset:    0
        VCDIFF target window length:  22670
        VCDIFF data section length:   0
          Offset Code Type1 Size1 @Addr1 + Type2 Size2 @Addr2
          000000 019  CPY_0 22670 @0     

    If these conditions aren't met, the function raises an XdeltaMismatch
    exception describing the point of discovery for the mismatch.
    """

    if expected_size != target_size:
        raise XdeltaMismatch(f"Xdelta pre-parse check failed:"
                f"\n  Source filesize {expected_size}"
                f"\n  Target filesize {target_size}")

    expected_vcdiffs = {
        "VCDIFF header indicator":      "VCD_APPHEADER",
        "VCDIFF copy window length":    str(expected_size),
        "VCDIFF copy window offset":    "0",
        "VCDIFF target window length":  str(expected_size),
        "VCDIFF data section length":   "0",
    }

    header_line = "Offset Code Type1 Size1 @Addr1 + Type2 Size2 @Addr2"
    expected_instr = f"000000 019  CPY_0 {expected_size} @0".split()

    p = await ExtCmd.xdelta_printdelta.exec_async(
            xdelta=xdelta_file,
            _stdout=asyncio.subprocess.PIPE,
            _stderr=asyncio.subprocess.PIPE)

    try:
        line_num = 0
        lines_read = []

        async def getline():
            nonlocal line_num
            line_num += 1
            line_bytes = await p.stdout.readline()
            lines_read.append(line_bytes.decode())
            return lines_read[-1].strip()

        def fail(msg):
            lines_joined = "".join(lines_read)
            raise XdeltaMismatch(
                    f"Xdelta check failed - {msg}"
                    f"\n  Cmd {p.args}"
                    f"\n  Returncode {p.returncode}"
                    f"\n  At line {line_num} in output:"
                    f"\n{lines_joined}"
                )

        def expect(line_type, expect, got):
            if got != expect:
                fail(f"Mismatched {line_type}:"
                        f"\n  Expected: '{expect}'"
                        f"\n  Got:      '{got}'")

        # Read line-by-line ensuring the file contains the expected headers
        while line := await getline():
            if ":" not in line:
                break
            key, value = line.split(":", maxsplit=1)
            value = value.strip()
            if key in expected_vcdiffs:
                if expected_vcdiffs[key] == value:
                    # Found this one, make sure we don't get a second one
                    expected_vcdiffs[key] = None
                else:
                    fail(f"key '{key}' value '{value}' != '{expected_vcdiffs[key]}'")

        # Ensure we found every expected VCDIFF line
        for key, value in expected_vcdiffs.items():
            if value is not None:
                fail(f"Couldn't find key '{key}'")

        # Still have a line out from the while loop
        expect("header", header_line, line)
        line = await getline()
        expect("instruction", expected_instr, line.split())
        line = await getline()
        expect("empty", "", line)

        if not p.stdout.at_eof():
            fail(f"Expected EOF")

        # Wait for the xdelta3 process to finish.  For a matching file
        # (the expected case), this wait will succeed immediately, since we
        # have already confirmed that the output stream is ended.
        await communicate(p)

        if p.stderr_data:
            fail(f"Got non-empty stderr:\n{''.join(p.stderr_data.decode())}")
        if p.stdout_data:
            fail(f"Got unexpected stdout after EOF detected:\n{''.join(p.stdout_data.decode())}")

        if p.returncode != 0:
            fail(f"Got unexpected {p.returncode=}")

    except XdeltaMismatch:
        raise

    except:
        fail(f"Got unexpected exception")

    finally:
        # terminate(): will sometimes emit a warning
        # 'Unknown child process pid 266074, will report returncode 255'
        # See https://bugs.python.org/issue43578
        #
        # As a workaround to minimize the number of times we see this in
        # testing, we can add a 10ms wait_for timeout call to p.wait() to
        # allow the process to complete on its own.
        #
        # However, this results in cleanup errors sometimes:
        # 'RuntimeError: Event loop is closed'
        #try:
        #    await asyncio.wait_for(p.wait(), timeout=0.01)
        #except asyncio.exceptions.TimeoutError:
        #    pass
        if p.returncode is None:
            p.terminate()
        await p.wait()

        # Note we must wait for termination - otherwise we trigger resource warnings:
        #/usr/lib/python3.9/asyncio/base_subprocess.py:125: ResourceWarning: unclosed transport <_UnixSubprocessTransport pid=118443 running stdout=<_UnixReadPipeTransport closed fd=6 closed>>
        #  _warn(f"unclosed transport {self!r}", ResourceWarning, source=self)
        #Object allocated at (most recent call last):
        #  File "/usr/lib/python3.9/asyncio/unix_events.py", lineno 197
        #    transp = _UnixSubprocessTransport(self, protocol, args, shell,
        #
        # This may be related to https://bugs.python.org/issue41320
        #Loop <_UnixSelectorEventLoop running=False closed=True debug=False> that handles pid 115696 is closed


def get_nearest_n(x, n):
    """Round up to the nearest non-zero n"""
    rounded = -(-x // n) * n
    return rounded if rounded else n


async def par2_create(f, num_par2_files, percent_redundancy):
    """Create a par2 set with the given constraints, delete the base .par2

    Use the default Config.par2_base_blocksize unless the resulting number
    of blocks across num_par2_files at the given redundancy would exceed
    Config.par2_max_num_blocks; in that case, ramp up the block size multiple.
    """
    filesize = os.path.getsize(f)
    num_par2_bytes = filesize * num_par2_files * percent_redundancy // 100
    min_blocksize = num_par2_bytes // Config.par2_max_num_blocks
    blocksize = get_nearest_n(min_blocksize, Config.par2_base_blocksize)
    #print(f"{filesize=}\n{num_par2_bytes=}\n{min_blocksize=}\n{blocksize=}")

    proc = await ExtCmd.par2_create.run_fg(infile=f, blocksize=blocksize,
            redundance=percent_redundancy, numfiles=num_par2_files)
    os.remove(f + ".par2")


def get_related_par2file(f):
    if not f.endswith(".par2"):
        par2files = glob.glob(f"{f}.*par2")
        if not par2files:
            raise MissingPar2File(f"Couldn't find par2 file for {f}\n"
                    "  Candidates:\n   " + "\n   ".join(glob.glob(f"{f}*")))
        f = par2files[0]
    return f


async def par2_verify(f):
    """Verify the given file f.

    f may be a par2 file, or a file with any associated .vol*.par2 or .par2 file
    """
    proc = await ExtCmd.par2_verify.run_fg(file=get_related_par2file(f))


async def par2_repair(f):
    proc = await ExtCmd.par2_repair.run_fg(file=get_related_par2file(f))
    #print("Repaired", proc.exmsg())


async def get_file_duration(fpath):
    """Use ffprobe to determine how many seconds the file identified by fpath plays for."""
    proc = await ExtCmd.get_media_duration.run_fg(file=fpath)

    if proc.stderr_data:
        raise InvalidMediaFile(f"Got extra stderr {proc.exmsg()}")

    try:
        duration = float(proc.stdout_data)
    except ValueError as e:
        raise InvalidMediaFile(f"Could not parse duration stdout {proc.exmsg()}") from e

    return duration


async def detect_silence(fpath):
    """Use ffmpeg silencedetect to find all silent segments.

    Return a list of TimeRange objects identifying the spans of silence."""

    proc = await ExtCmd.ffmpeg_silence_detect.run_fg(
            file=fpath,
            length=Config.file_scan_duration_s,
            threshold=Config.silence_threshold_dbfs,
            duration=Config.silence_min_duration_s)

    detected_lines = [line for line in proc.stderr_data.splitlines()
                        if line.startswith(b'[silencedetect')]

    offsets = [float(line.split()[-1]) for line in detected_lines if b"silence_start" in line]
    durations = [float(line.split()[-1]) for line in detected_lines if b"silence_end" in line]

    return list(TimeRange(start, duration) for start, duration in zip(offsets, durations))


#============================================================================
# File and OS utilities
#============================================================================

def flush_fs_caches(*files):
    """Call the sync(1) command on the filesystems containing the given files,
    then flush all filesystem caches in the virtual memory subsystem.
    """

    libc = ctypes.cdll.LoadLibrary("libc.so.6")
    libc.posix_fadvise.argtypes = (ctypes.c_int,
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int)
    POSIX_FADV_DONTNEED = 4 # (from /usr/include/linux/fadvise.h)

    for f in files:
        with open(f, "rb") as fd:
            fno = fd.fileno()
            offset = 0
            len = 0
            os.fsync(fno)
            ret = libc.posix_fadvise(fno, offset, len, POSIX_FADV_DONTNEED)
        if ret != 0:
            raise RuntimeError("fadvise failed, could not flush file from cache: " + f)

def get_wavs_in(source, other_wavs=None):
    """Search the given source pathlib.Path instance for wav files.

    Do a set-union across the found wavs and those in other_wavs.
    Uses Config.wav_extensions as the pattern.
    Return a sorted list of resulting pathlib.Path instances.
    """

    # The globs may match the same file multiple times,
    # so make sure they are unique
    if other_wavs is None:
        other_wavs = set()
    for ext in Config.wav_extensions.split():
        other_wavs |= set(source.glob(f"*.{ext}"))
    return sorted(other_wavs)

def find_duplicate_basenames(paths):
    """Return a dict mapping duplicate basenames to their full paths."""
    pathmap = collections.defaultdict(list)
    for p in paths:
        pathmap[p.name].append(p)
    # Remove any entries that only have one item in their associated list
    for name in list(pathmap):
        if len(pathmap[name]) == 1:
            del pathmap[name]
    return pathmap

def set_mtime(f, dt):
    """Update the timestamp of the given file f to the given datetime dt"""
    seconds = dt.timestamp()
    os.utime(f, (seconds,)*2)

def inject_timestamp(fmt, when=None):
    """Format the time into the given fmt string.

    fmt must contain a single {} which indicates where the timestamp goes.
    Config.timestamp_fmt_with_seconds is used to format the time.
    If when is None, the current time is encoded.
    Otherwise, when should be an object for which strftime is defined.
    """
    if when is None:
        when = datetime.datetime.now()
    return fmt.format(when.strftime(Config.timestamp_fmt_with_seconds))

#============================================================================
# Audio file processing
#============================================================================

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


def format_duration(duration):
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
        dstr = format_duration(finfo.duration_s)

        # Format the notes
        if finfo.extra_speech:
            notes = "-".join(finfo.extra_speech) + "."
        else:
            notes = ""

        finfo.suggested_filename = Config.dest_fname_fmt.format(
                prefix=finfo.instrument, datestamp=tstr,
                notes=notes, duration=dstr, orig_fname=finfo.orig_filename)
        print(f"Speechinizer: {finfo.orig_filename!r} - {finfo.orig_speech!r} -> {finfo.suggested_filename!r}")

    except (NoSuitableAudioSpan, TimestampGrokError) as e:
        # Couldn't find any timestamp info
        finfo.suggested_filename = finfo.orig_filename


#============================================================================
# Signaling interface
#============================================================================

class QDict(dict):
    """Allow additing attributes.  dict locks down its slots."""

def make_queues(s):
    """Make an asyncio.Queue with an added name attribute for each word in s.

    Returns a dict keyed off of the given names, but also with
    attributes set based on those names so they are dot accessible.
    """
    qdict = QDict()
    for qname in s.split():
        qdict[qname] = asyncio.Queue()
        qdict[qname].name = qname
        setattr(qdict, qname, qdict[qname])
    return qdict

def listify(arg):
    if arg is None:
        return []
    elif isinstance(arg, str) or isinstance(arg, bytes):
        return[arg]
    elif isinstance(arg, collections.abc.Sequence):
        return arg
    elif isinstance(arg, collections.abc.Iterable):
        return list(arg)
    else:
        return [arg]

def format_steps(steps):
    steplist = listify(steps)
    return ", ".join(step.__name__ for step in steplist)

class Stepper:
    """Manage a set of asyncio.Queues for coordinating stepping a sequence.

    The end of the sequence should be indicated be the given end token.
    Each sync_ and send_ parameter should be None, an asyncio.Queue with a
    .name attr as returned by make_queues, or a list of such.

    The step() and walk() methods provide high-level interfaces for easily
    building a network of tasks with the same set of data flowing through
    each.
    """
    def __init__(self, name=None, sync_from=None, pull_from=None,
            send_to=None, sync_to=None, end=None):

        self.name = name
        self.sync_from = listify(sync_from)
        self.pull_from = listify(pull_from)
        self.send_to = listify(send_to)
        self.sync_to = listify(sync_to)
        self.end = end

        self.value = end # last gotten value
        self.pre_sync_met = False

    def log(self, *args, **kwargs):
        dbg(f"Stepper<{self.name}> :", *args, depth=1, **kwargs)

    def fmtqueues(self, queues):
        return ", ".join(q.name for q in queues)

    class PreSyncTokenError(RuntimeError):
        pass

    class DesynchronizationError(RuntimeError):
        pass

    async def _get_across(self, q_list, qtype=""):
        last_token = None
        last_queue = None
        # sync across the next token from each stream
        for q in q_list:
            token = await q.get()
            q.task_done()
            if last_queue is not None and token != last_token:
                raise Stepper.DesynchronizationError(
                        f"Mismatching tokens between queues!"
                        f"\n  {last_token!r} <= {last_queue.name}"
                        f"\n  {token!r} <= {q.name}")
            last_token = token
            last_queue = q
        if q_list:
            if qtype == "sync_from":
                self.log(f"[<= {qtype}({self.fmtqueues(q_list)})]")
            else:
                self.log(f"[got {last_token} <= {qtype}({self.fmtqueues(q_list)})]")
        return last_token


    async def pre_sync(self):
        """Wait for end on all sync_from Queues.

        raises Stepper.DesynchronizationError if the sync_from queues emit
        mismatching tokens.

        raises Stepper.PreSyncTokenError if the sync_from queues emit a non-end
        token.
        """
        if not self.pre_sync_met:
            while (token := await self._get_across(self.sync_from,
                    qtype="sync_from")) != self.end:
                raise Stepper.PreSyncTokenError(
                        f"Got non-end token {token!r} from sync_from queues"
                        f" {self.fmtqueues(self.sync_from)}")
            self.pre_sync_met = True


    async def get(self):
        """Pre-sync, then wait for all pull_from queues to emit their next token.

        Raises Stepper.DesynchronizationError if the pull_from queues don't
        all report matching tokens.
        """
        await self.pre_sync()
        self.value = await self._get_across(self.pull_from, "pull_from")
        return self.value


    async def put(self, token):
        """Put token into each send_to queue.

        If the token is the end token, put it in the sync_to queue.
        """
        for q in self.send_to:
            await q.put(token)
        if self.send_to:
            self.log(f"[put {token} => send_to({self.fmtqueues(self.send_to)})]")

        if token == self.end:
            for q in self.sync_to:
                await q.put(token)
            if self.sync_to:
                self.log(f"[=> sync_to({self.fmtqueues(self.sync_to)})]")

    async def step(self):
        """Simplify the standard while loop idiom.

        Equivalent to:
            while (i := await stepper.get()) is not None:
                # Do task work
                await stepper.put(i)
            await stepper.put(None)
        """
        if self.value != self.end:
            # Put the value from the last iteration
            await self.put(self.value)

        # Wait for the next token before starting this iteration
        await self.get()
        done = self.value == self.end
        if done:
            self.log("[Task complete.]")
            await self.put(self.value)
        else:
            self.log(f"[Executing step {self.value!r}]")
        return not done

    async def walk(self, coro, *args, **kwargs):
        """Repeatedly await the given coro using step().

        The given args and kwargs are passed through to each invocation of
        coro, along with the following special keyword arguments:

            * token: the last recieved token from the pull_from queues
            * stepper: the stepper instance used for the walk
        """
        while await self.step():
            await coro(*args, **kwargs, token=self.value, stepper=self)

class Link(collections.namedtuple('Link', 'src dest')):
    __slots__ = ()

    def shortname(self):
        return f"{self.src.__name__}->{self.dest.__name__}"

    def __str__(self):
        return f"Link({self.shortname()})"

    def name(self, side):
        return getattr(self, side).__name__

    def other_name(self, side):
        return getattr(self, self.other(side)).__name__

    @classmethod
    def other(cls, side):
        return "src" if side == "dest" else "dest"

class LinkQDict(collections.UserDict):
    def __init__(self, name):
        self.name = name
        super().__init__()

    def fmtdata(self):
        return "\n      ".join(f"{k}: {q}" for k, q in self.data.items())

    def __str__(self):
        return f"LinkQDict({self.name}):\n      {self.fmtdata()}"

class StepperQueue(asyncio.Queue):
    def __init__(self, name, qtype):
        self.name = name
        self.qtype = qtype
        self.src = False
        self.dest = False
        super().__init__()

    def __str__(self):
        return f"StepperQueue({self.qtype}:{self.name}, " \
                f"src={self.src}, dest={self.dest})"

class StepNetwork:
    """Auto-wired DAG of Steppers and their step coroutines"""
    side_q_names = {
            ("dest", "sync"): "sync_to",
            ("dest", "token"): "send_to",
            ("src", "token"): "pull_from",
            ("src", "sync"): "sync_from",
        }

    def __init__(self, name, end=None):
        self.name = name
        self.end = end
        self.common_kwargs = {}

        # Coroutine to Stepper instance maps
        self.producers = {}
        self.steps = {}

        self.seen_coroutines = set()

        # Link(src, dest) -> StepperQueue mapping dicts
        self.sync_queues = LinkQDict("sync")
        self.token_queues = LinkQDict("token")

        # Plan:
        # * Add args, kwargs, step (coro) to Stepper
        # * Remove name from Stepper, use step.__name__
        # * Fill queue lists from StepNetwork

    def update_common_kwargs(self, **kwargs):
        """Pass the given keyword arguments to each producer/step coroutine."""
        self.common_kwargs |= kwargs

    def add_producer(self, *args, **kwargs):
        """Add a producer into the StepNetwork.

        Producer coroutines are not automatically stepped, unlike step
        coroutines.  They define the start of the workunit network pipeline,
        so they do not have inbound sync_from or pull_from queues.

        send_to is a coroutine (or list of them) to send tokens to
        sync_to is a coroutine (or list of them) to send end tokens to

        The given coro must use stepper.put() to feed its send_to and sync_to
        queues.
        """
        self.add_step(*args, is_producer=True, **kwargs)

    def fmt_linkerr(self, link: Link, side: str, qdict: LinkQDict) -> str:
        return \
            f"{link.name(side)}:{self.side_q_names[(link.other(side), qdict.name)]}=" \
            f"{link.other_name(side)} for {qdict.name}-type {link} " \
            f"in StepNetwork({self.name})"

    def _map_link_to_queue(self, link, qdict, side):
        """Creates a queue for the link and maps it in qdict if no mapping exists.

        The side parameter allows tracking of which ends of the queue have
        been wired up in the network.

        Returns the queue associated with the link.
        """
        assert link.src != link.dest, \
                f"Self-loops are disallowed: {self.fmt_linkerr(link, side, qdict)}"

        if link not in qdict:
            other_side = Link.other(side)
            other_coro = getattr(link, other_side)
            # If we already added the other side of the link to the
            # StepNetwork (according to seen_coroutines), then that other
            # coroutine should have already added the link to the qdict.
            assert other_coro not in self.seen_coroutines, \
                f"Already added {other_coro.__name__}, but it was missing " \
                f"{self.fmt_linkerr(link, other_side, qdict)}"
            qdict[link] = StepperQueue(link.shortname(), qdict.name)

        q = qdict[link]
        assert not getattr(q, side), \
            f"Already added {side} side of {link}:" \
            f"\n  {q}" \
            f"\n  in {qdict}"
        setattr(q, side, getattr(link, side).__name__)
        return q

    def _add_link_queues(self, stepper_qlist, qdict, src, dest):
        if isinstance(src, types.FunctionType):
            src.targets = dest
            for item in dest:
                link = Link(src, item)
                q = self._map_link_to_queue(link, qdict, "src")
                stepper_qlist.append(q)

        elif isinstance(dest, types.FunctionType):
            for item in src:
                link = Link(item, dest)
                q = self._map_link_to_queue(link, qdict, "dest")
                stepper_qlist.append(q)

        else:
            assert False, f"Niether {src} nor {dest} are functions!" \
                    f"\n  {qdict}"

    def add_step(self, coro, *args, is_producer=False,
                 sync_from=None, pull_from=None,
                 send_to=None, sync_to=None, **kwargs):
        """Add a step to the StepNetwork.

        The given coro will be automatically stepped via Stepper.walk.

        The [sync/pull/send]_[to/_from] arguments specify a single
        step/producer/consumer, or a list of them, from which the
        correspending queues will be hooked up between the steppers.

        When execute() is called, the queues will be created and hooked into
        the Steppers.
        """
        self.seen_coroutines.add(coro)

        stepper = Stepper(name=coro.__name__, end=self.end)
        stepper.args = args
        stepper.kwargs = kwargs

        if is_producer:
            assert sync_from is None, \
                    f"Producer {stepper.name} can't have sync_from source(s): " \
                    f"{format_steps(sync_from)}"
            assert pull_from is None, \
                    f"Producer {stepper.name} can't have pull_from source(s): " \
                    f"{format_steps(pull_from)}"
            self.producers[coro] = stepper
        else:
            assert pull_from is not None, \
                    f"Non-producer {stepper.name} needs a pull_from source"
            self.steps[coro] = stepper

        self._add_link_queues(stepper.sync_from, self.sync_queues,
                src=listify(sync_from), dest=coro)

        self._add_link_queues(stepper.pull_from, self.token_queues,
                src=listify(pull_from), dest=coro)

        self._add_link_queues(stepper.send_to, self.token_queues,
                src=coro, dest=listify(send_to))

        self._add_link_queues(stepper.sync_to, self.sync_queues,
                src=coro, dest=listify(sync_to))

    def check_queue_wiring(self, qdict):
        for link, q in qdict.items():
            for side in link._fields:
                assert getattr(q, side), \
                    f"missing {self.fmt_linkerr(link, side, qdict)}"

    # TODO - how to even get at the edges in this graph...
    def check_for_cycles(self):
        for v in itertools.chain(self.producers.keys(), self.steps.keys()):
            v.color = "white"
        for v in itertools.chain(self.producers.keys(), self.steps.keys()):
            if v.color == "white":
                self.depth_first_visit(v)

    def depth_first_visit(self, u):
        u.color = "gray"
        for v in u.targets:
            if v.color == "white":
                self.depth_first_visit(v)
            elif v.color == "gray":
                assert false, "found backedge"
        u.color = "black"

    def check_queues(self):
        """Ensure all queues are wired up properly, assert if not."""
        # Ensure we have symmetrical from/to wirings
        self.check_queue_wiring(self.sync_queues)
        self.check_queue_wiring(self.token_queues)

        # Ensure there are no cycles using depth-first search
        self.check_for_cycles()

    async def execute(self):
        """Run asyncio.gather on the producer and step coroutines.

        execute matches the send/pull/sync queues across the Steppers in
        the network, and then gathers across the added producer coroutines
        and the Stepper.walk() coroutine for each step.
        """
        self.check_queues()

        tasks = []
        for coro, stepper in self.producers.items():
            tasks.append(coro(*stepper.args, stepper=stepper,
                              **stepper.kwargs,
                              **self.common_kwargs))

        for step_coro, stepper in self.steps.items():
            tasks.append(stepper.walk(step_coro, *stepper.args,
                                      **stepper.kwargs,
                                      **self.common_kwargs))

        await asyncio.gather(*tasks)

#============================================================================
# Phase A: Encode - encode flacs, rename, generate pars
#============================================================================

def play_media_file(finfo):
    """Play the given file in a background process."""

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

    # Signal completion - no more files to process
    recognizer2prompter_speech_guesses.put_nowait(None)


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
    while (finfo := await recognizer2prompter_speech_guesses.get()) is not None:
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

    # Signal completion - no more files to process
    prompter2par_dest_names.put_nowait(None)


async def flac_encoder(files, flac2par_completions):
    for finfo in files:
        await waiter("FlacEncoder", finfo)
        flac2par_completions.put_nowait(finfo)

    # Signal completion - no more files to process
    flac2par_completions.put_nowait(None)


async def renamer_and_par_generator(prompter2par_dest_names,
                                    flac2par_completions,
                                    par2processor_completions):
    while (finfo := await prompter2par_dest_names.get()) is not None:
        await waiter("Renamer", finfo)
        prompter2par_dest_names.task_done()

        # Wait for flac encoding to complete as well
        flac = await flac2par_completions.get()
        if finfo is not flac:
            raise RuntimeError("Got mismatching files from the prompter and flac task queues!"
                    f"\n  Prompter: {finfo.orig_filename}"
                    f"\n  Encoder : {flac.orig_filename}")
        await waiter("ParGenerator", flac)
        par2processor_completions.put_nowait(flac)

    # Signal completion - no more files to process
    flac2par_completions.put_nowait(None)


async def phaseA_encode(filepaths, dest):
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

    time_start = time.monotonic()
    # Wait for all files to be completely processed
    await asyncio.gather(
        recognizer_task,
        prompter_task,
        flac_task,
        rename_and_par_task)
    time_end = time.monotonic()
    print(f"Processor: done, {time_end-time_start:.1f}s total.")


#============================================================================
# Phase B: Verify and copyback - Verify par2s, clean up, copy flacs back to src
#============================================================================

async def phaseB_verify_and_copyback(dest):
    pass


#============================================================================
# Phase C: Verify backcopy - Verify USB copy of FLAC files, clean up
#============================================================================

async def phaseC_verify_backcopy(dest):
    pass


#============================================================================
# TransferInfo and worklists
#============================================================================

class TransferInfo(types.SimpleNamespace):
    """Contains the state of each transfer.

    A TransferInfo object is created for each file to transfer, based
    on the wav files that exist and any in-progress transfer directories
    in the destination directory.

    These objects are stored in the worklist, which each task indexes into
    based on the tokens it gets from walking its incoming Stepper queues.
    """

    source_wav = None
    wav_abspath = None
    dest_dir = None
    wav_progress_dir = None
    source_link = None

    fname_guess = None
    fname_prompted = None
    mtime = None
    flac_fpath = None
    par_fpaths = None

#============================================================================
# Tasks
#============================================================================

def act(msg):
    """Logs the given message according to whether it will be executed or not.

    Returns Config.act

    This should be used to protect any code that modifies the filesystem.
    """
    dbg(f"{'Running' if Config.act else 'Skip (noact)'} :", msg, depth=1)
    return Config.act


class Step:
    async def setup(cmdargs, worklist, stepper):
        if cmdargs.continue_from:
            progress_dir = cmdargs.continue_from
        else:
            progress_dir = cmdargs.dest / inject_timestamp(Config.progress_dir_fmt)
            if act(f"create main progress dir {progress_dir}"):
                progress_dir.mkdir()

        for wav in cmdargs.wavs:
            info = TransferInfo(
                    source_wav=wav,
                    wav_abspath=os.path.abspath(wav),
                    dest_dir=cmdargs.dest,
                    wav_progress_dir=progress_dir / wav.name,
                    source_link=progress_dir / wav.name / Config.source_wav_linkname,
                )

            if act(f"create wav progress dir {wav.name} and symlink to {info.wav_abspath}"):
                info.wav_progress_dir.mkdir()
                info.source_link.symlink_to(info.wav_abspath)

            worklist.append(info)
            await stepper.put(len(worklist) - 1)
            await asyncio.sleep(0) # Let the work begin

        await stepper.put(None)


    async def listen(cmdargs, worklist, *, token, stepper):
        stepper.log(f"****** working on {token} *******")

    async def prompt(cmdargs, worklist, *, token, stepper):
        pass

    async def flacenc(cmdargs, worklist, *, token, stepper):
        pass

    async def pargen(cmdargs, worklist, *, token, stepper):
        pass

    async def xdelta(cmdargs, worklist, *, token, stepper):
        pass

    async def cleanup(cmdargs, worklist, *, token, stepper):
        pass

    async def finish(worklist):
        dbg("in finish()")

#============================================================================
# Main sequence
#============================================================================

async def run_tasks(args):
    """Connect up the various tasks with queues and run them."""
    worklist = []

    network = StepNetwork("wavflacer")
    network.update_common_kwargs(cmdargs=args, worklist=worklist)

    network.add_producer(Step.setup,
            send_to=[Step.listen, Step.flacenc])

    network.add_step(Step.listen,
            pull_from=Step.setup,
            send_to=Step.prompt)

    network.add_step(Step.prompt,
            pull_from=Step.listen,
            send_to=Step.pargen)

    network.add_step(Step.flacenc,
            pull_from=Step.setup,
            send_to=[Step.pargen, Step.xdelta],
            sync_to=Step.xdelta)

    network.add_step(Step.pargen,
            pull_from=[Step.prompt, Step.flacenc],
            send_to=Step.cleanup)

    network.add_step(Step.xdelta,
            sync_from=Step.flacenc,
            pull_from=Step.flacenc,
            sync_to=Step.cleanup)

    network.add_step(Step.cleanup,
            sync_from=Step.xdelta,
            pull_from=Step.pargen)

    await network.execute()
    await Step.finish(worklist)


def run_tests_in_subprocess():
    """Run unittests in test_taketake.py.

    Use a subprocess so the tests won't be affected by or use the current Config.
    Also buffer the stdout/stderr to keep noisy tests quiet.
    """

    file_dir = Path(__file__).resolve().parent
    test_script = str(file_dir / 'tests' / 'test_taketake.py')

    print("Ensuring taketake ecosystem integrity - running", test_script)
    p = subprocess.run([test_script, "-b"])
    if p.returncode != 0:
        print("taketake pre-testing failed!  Aborting.")
        sys.exit(1)


def dbg(*args, depth=0, **kwargs):
    if Config.debug:
        print(f"*{Config.dbg_prog}* -",
              *args, f"({sys._getframe(1+depth).f_code.co_name})", **kwargs)


def format_args(args):
    arglist=[]
    for arg, val in vars(args).items():
        if val is not False and val is not None:
            if val is True:
                arglist.append(str(arg))
            elif isinstance(val, collections.abc.Sequence):
                arglist.append(f"{arg}=[{', '.join(str(e) for e in val)}]")
            else:
                arglist.append(f"{arg}={val}")
    return " ".join(arglist)


def validate_args(parser):
    """Validates arguments processed by the parser and set in parser.args.

    If --target wasn't specified, removes the last item from args.sources
    and sets it as the dest arg.

    If args.continue_from isn't set, sets it to the progress dir in dest if it
    exists.

    Builds args.wavs from the remaining args.sources and the args.progress_dir.

    Sets parser.args.dest and check for consistency, including dir existance.
    Sets parser.errors, which is a list of errors encountered during parsing.
    """

    parser.errors = []
    def err(*args):
        parser.errors.append(" ".join(str(a) for a in args))

    args = parser.args
    # debug must be set prior to the first call to dbg()
    if args.debug:
        Config.debug = True

    if args.no_act:
        Config.act = False

    dbg("args pre-val: ", format_args(args))

    # Use the final positional parameter as dest, like mv does
    if args.sources and args.dest is None:
        args.dest = args.sources.pop()

    # Expand any sources that are directories
    args.wavs = []
    for source in args.sources:
        if source.is_dir():
            if len(args.sources) > 1:
                others = list(args.sources)
                others.remove(source)
                err("When transfering from a whole directory, "
                    "no other SOURCE_WAV parameters should be specified."
                    "\n    Found SOURCE_WAV directory:", source,
                    "\n    other SOURCE_WAVs:",
                    f"[{' '.join(str(d) for d in others)}]")

            args.wavs = get_wavs_in(source)
            break

        else:
            args.wavs.append(source)

    # Set up dest using continue_from or sources
    if args.continue_from:
        if not args.continue_from.is_dir():
            err("PROGRESS_DIR does not exist! Got: --continue", args.continue_from)
        if args.sources:
            err("--continue was specified, but so were SOURCE_WAVs:", *args.sources)
        if args.dest:
            err("--continue was specified, but so was DEST_PATH:", args.dest)

        # Override dest when continuing from a progress dir
        p = args.continue_from
        # Try to stay relative and with symlinks unresolved,
        if p.name == "." or p.name == "..":
            # But if the continue_from is relative via . or ..,
            # we have to resolve it.
            p = p.resolve()
        args.dest = p.parent

    if not args.dest:
        err("No DEST_PATH specified!")

    elif not args.dest.is_dir():
        err("Specified DEST_PATH does not exist!", args.dest)

    elif not args.continue_from:
        # Check for interrupted progress directories in dest
        progress_dirs = sorted(Path(args.dest).glob(
            Config.progress_dir_fmt.format("*")))
        if len(progress_dirs) > 1:
            sep = "\n      "
            err("Too many progress directories found in DEST_PATH:", args.dest,
                f"{sep}{sep.join(str(d) for d in progress_dirs)}"
                "\n    Use -c|--continue on a specific directory"
                " to continue the transfer represented by that directory"
                f"\n    For example:  {Config.prog} -c '{progress_dirs[0]}'")

        elif len(progress_dirs) == 1:
            args.continue_from = progress_dirs[0]

    # Check wavs exist, or are in the progress_dirs
    for wav in args.wavs:
        if args.continue_from:
            tempwavdir = args.continue_from / wav.name

        if args.continue_from and tempwavdir.exists:
            srclink = tempwavdir / Config.source_wav_linkname
            if not tempwavdir.is_dir():
                err("temp wavfile exists in progress dir",
                    "but is not a directory!", tempwavdir)
            elif not srclink.is_symlink():
                err("temp wavfile tracker is not a symlink!", srclink)
            elif wav.resolve() != srclink.resolve():
                err("wav progress symlink resolves to a different file than the "
                    "specified SOURCE_WAV file!"
                    f"\n    progress:   {srclink} -> {srclink.resolve()}"
                    f"\n    SOURCE_WAV: {wav} -> {wav.resolve()}")
        elif not wav.is_file():
            # No progress dir entry
            err("SOURCE_WAV not found:", wav)

    if not args.sources and not args.continue_from:
        err("No SOURCE_WAVs specified to transfer!")

    # Ensure args.wavs have unique basenames
    dups = find_duplicate_basenames(args.wavs)
    if dups:
        sep = "\n      "
        err("Duplicate wavfiles names specified!",
            *[f"\n      {n} -> {', '.join(str(p) for p in paths)}"
                for n, paths in dups.items()])

    # inject wavs from args.continue_from into args.wavs
    if args.continue_from:
        # map basename to fullname
        src_wavs_dict = {w.name: w for w in args.wavs}
        for wav in args.continue_from.glob("*"):
            if wav.is_dir():
                wavlink = wav / Config.source_wav_linkname
                if wav.name not in src_wavs_dict:
                    # Need the link target so we can copy-back the flac
                    # to the right place.
                    args.wavs.append(wavlink.readlink())
                # Can't check this since we use this symlink to point back to
                # the original wav dir for flac copy-back
                #if not wavlink.exists():
                #    err(f"Broken progress dir symlink:"
                #        f"\n       {wavlink}"
                #        f"\n    -> {wavlink.resolve()}")

    dbg("args post-val:", format_args(args))


def process_args(argv=None):
    parser = argparse.ArgumentParser(
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    arg = parser.add_argument

    arg('-n', '--no-act', action='store_true',
        help="Do everything but modify filesystems or prompt the user")

    arg('-d', '--debug', action='store_true',
        help="Show debug output, including tracebacks from exceptions")

    arg('-p', '--prefix', action='store',
        help="Prefix flac files with the given string")

    arg('-k', '--keep-wavs', action='store_true',
        help="Don't delete processed source wav files")

    arg('--skip-copyback', action='store_true',
        help="Don't copy the encoded flacs back to their source wav dir")

    arg('--skip-tests', action='store_true',
        help="""Do not run unit tests prior to starting the transfer.

            This saves a few seconds at startup, but you risk integrity
            issues if some system change causes differences that wouldn't
            otherwise be detectable during normal running.""")

    arg('-c', '--continue',
        metavar='PROGRESS_DIR', action='store', dest='continue_from',
        type=Path,
        help="""Continue processing an interrupted transfer.
            The PROGRESS_DIR must exist and be a child directory
            contained within the target DEST_PATH.
            When -c is used, specifying SOURCE_WAV and DEST_PATH
            is unnecessary, but if they are specified, they must
            match what is found in the given PROGRESS_DIR.
            """)

    arg('-t', '--target', '--target-directory', dest='dest', type=Path,
        metavar='DEST_PATH', action='store',
        help="""Causes the specified path to be used as the destination.
            The final positional will not be handled specially--all
            positional arguments will be treated as SOURCE arguments""")

    arg('sources', metavar='SOURCE_WAV', nargs='*', type=Path,
        help="""Transfer the specified SOURCE_WAV files.  If there is only
            a single SOURCE_WAV specified and it is a directory, then
            transfer all wav files found in that directory.""")

    # This is left empty because sources is greedy.  process_args() fills it.
    arg('_dest', metavar='DEST_PATH', nargs='?', type=Path,
        help=f"""Destination directory for encoded flac and par2 files.

            This directory will also contain the timestamped
            {Config.progress_dir_fmt.format('*')} directory for tracking progress.""")

    parser.args = parser.parse_args(argv)
    validate_args(parser)
    return parser

def format_errors(errors):
    return "".join("\n  * {}".format(e) for e in errors)

def main():
    arg_parser = process_args()

    # Report errors
    if arg_parser.errors:
        arg_parser.error("Invalid command line options:"
                + format_errors(arg_parser.errors))

    args = arg_parser.args
    if not args.skip_tests:
        run_tests_in_subprocess()

    try:
        asyncio.run(run_tasks(args))
    except TaketakeRuntimeError as e:
        print(f"Error - aborting: {e}", file=sys.stderr)
        if args.debug:
            raise
        return(1)

    return 0

if __name__ == "__main__":
    sys.exit(main())
