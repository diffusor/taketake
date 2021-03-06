#!/usr/bin/env python3.10

# TODO support transfering from flac files?
# TODO use template instead of string formating for config formats (Minor security issue)
# TODO use https://audiodiff.readthedocs.io/en/latest/ to diff flac vs. wav
#       PX-S3000 appends an extra 44B chunk at the end of its wavs that flac doesn't store

"""Transfer wav files into flac files.

This is meant for convenient but robust download from digitial piano USB drives.
taketake performs the following steps:

* Suggests file names based on spoken timestamp information in each wav
* Prompts the user to rename the file, allowing listening to it via Alt-h
* Encodes par2 recovery files for the destination flac
* Flushes cached data and verify the copied/encoded contents
* Automatically deletes the source wav file
* Copies the encoded flac and par2 files back onto the USB for archival
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
* cmp
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
import concurrent.futures
import time
import sys
import os
import shutil
import re
import json
import glob # TODO replace with Path.glob everywhere
import itertools
import collections
import subprocess
import datetime
import zoneinfo
import types
import ctypes
import dataclasses
from dataclasses import dataclass, field, is_dataclass
from typing import Any, List, Dict, Set, NamedTuple, Optional
from collections.abc import Callable, Generator, Coroutine, Sequence, Iterable, Hashable
from contextlib import contextmanager
from pathlib import Path

import speech_recognition
from word2number import w2n

# MyType: typing.TypeAlias=Classname (or "Classname" for forward reference)

# TODO make Config a @dataclass
class Config:
    act = True
    debug = False
    dbg_prog = "taketake"
    prog = sys.argv[0]

    num_listener_tasks = 6          # Number of concurrent speech-to-text threads
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
    par2_num_vol_files = 2
    par2_redundancy_per_vol = 4

    # Fincore check fails if
    #   fincore_bytes * fincore_rate_vs_fs > file_size
    # fincore runs at ~10ms/GB, SSDs do ~1GB/s -> 1% fincore rate.
    # So we can expect at most about 1% of a file's blocks to be read back in
    # after a flush before the fincore operation completes.
    fincore_rate_vs_fs = 100  # fincore runs at 100x filesystem read speed

    # This is the "one true format" for timestamps in filenames
    timestamp_fmt_compact = "%Y%m%d-%H%M%S%z-%a" # '20220529-160002-0700-Sun'
    timestamp_fmt_long = "%Y-%m-%d %H:%M:%S%z %a"
    timestamp_fmt_us = "%Y-%m-%d %H:%M:%S.%f%z"
    timestamp_re = re.compile(
            r'(?:^|(?<=\D))' # Ensure we don't grab the middle of a number
            r'(?P<fulltime>'
                r'(?P<timestamp>'
                 r'(?P<year>\d{4})'
                 r'(?P<month>\d{2})'
                 r'(?P<day>\d{2})[- _]'
                 r'(?P<hour>\d{2})'
                 r'(?P<minute>\d{2})'
                 r'(?P<second>\d{2})?'
                 r'(?P<timezone>[-+]\d{4})?)'
                r'(?:[- _.]?'   # timestamp-weekday_name separator
                 r'(?P<dayname>'
                     r'(?P<weekday>sun|mon|tue|wed|thu|fri|sat)'
                     r'(?P<weekdaysuffix>day|sday|nesday|rsday|urday)?'
                r'))?'
            r')'
            r'(?=$|\W|_)'
            , flags=re.IGNORECASE)

    timezone_offset_re = re.compile(r'^[-+]\d{4}$')

    # Most of these are only illegal on Windows.
    # Linux only forbids /
    # par2 can't handle * or ? (But Windows can't either)
    illegal_filechar_re = re.compile(r'[?*/\:<>|"]')
    # Normal Linux pathname limit is 255, but eCryptfs limits it further to 143
    # See ntninja's comment on https://serverfault.com/a/9548
    # We use 255, but have to subtract the max par2 size or par2 breaks
    max_filename_len = 255 - len('.vol195+195.par2')

    interfile_timestamp_delta_s = 5 # Assumed minimum time between takes

    prefix = "piano"
    instrument_fname = "instrmnt.txt"  # name for storing model name on USB src dir
    wav_extensions = "wav WAV"
    progress_dir_fmt = ".taketake.{}.tmp"
    source_wav_linkname = ".source.wav"
    audioinfo_fname = ".audioinfo.json"
    guess_fname = ".filename_guess"
    provided_fname = ".filename_provided"
    dest_fname_fmt = "{prefix}.{datestamp}{guess_tag}.{notes}{duration}.{instrument}.{orig_fname}.flac"
    flac_progress_fname = ".in_progress.flac"
    flac_interrupted_fname_fmt = ".interrupted-abandoned.{}.flac"
    flac_encoded_fname = ".encoded.flac"
    cmp_results_fname = ".cmp_results"
    transfer_log_fname = "transfer.log"
    src_flacs_dirname = "flacs"
    dest_par2_dirname = ".par2"
    done_processing_fname = ".done_processing"


# Exceptions
class TaketakeRuntimeError(RuntimeError): ...
class InvalidProgressFile(TaketakeRuntimeError): ...
class SubprocessError(TaketakeRuntimeError): ...
class InvalidMediaFile(TaketakeRuntimeError): ...
class MissingPar2File(TaketakeRuntimeError): ...
class TimestampGrokError(TaketakeRuntimeError): ...
class NoSuitableAudioSpan(TaketakeRuntimeError): ...
class CmpMismatch(TaketakeRuntimeError): ...
class FileFlushError(TaketakeRuntimeError): ...
class FileExists(TaketakeRuntimeError): ...

#============================================================================
# Dataclasses
#============================================================================

@dataclass
class TimeRange:
    start:float
    duration:float

    def __str__(self):
        end = self.start + self.duration
        r = "-".join(format_duration(t, style='colons') for t in (self.start, end))
        return f"[{r}]({format_duration(self.duration)})"


@dataclass
class AudioInfo:
    duration_s: Optional[float] = None
    speech_range: Optional[TimeRange] = None
    recognized_speech: Optional[str] = None # Was orig_speech
    parsed_timestamp: Optional[datetime.datetime] = None
    extra_speech:list[str] = field(default_factory=list)


@dataclass
class TransferInfo:
    """Contains the state of each transfer.

    A TransferInfo object is created for each file to transfer, based
    on the wav files that exist and any in-progress transfer directories
    in the destination directory.

    These objects are stored in the worklist, which each task indexes into
    based on the tokens it gets from walking its incoming Stepper queues.
    """
    token:int  # ID

    source_wav:Path
    dest_dir:Path
    wav_progress_dir:Path
    instrument:str

    # When None, the local timezone is used
    target_timezone: Optional[datetime.tzinfo] = None

    done_processing: bool = False  # TODO test this
    failures: list[Exception] = field(default_factory=list)

    # Read from files on resume
    audioinfo: Optional[AudioInfo] = None     # Config.audioinfo_fname
    #fstat: Optional[os.stat_result] = None
    fname_prompted: Optional[Path] = None     # Config.provided_fname
    timestamp: Optional[datetime.datetime] = None  # from fname_prompted

    # Used only between tasks that are skipped when done_processing
    fname_guess: Optional[str] = None
    timestamp_guess_direction: Optional[str] = None


#============================================================================
# JSON encode/decode
#============================================================================

class TaketakeJsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if is_dataclass(obj):
            d = vars(obj)
            d["__dataclass__"] = obj.__class__.__name__
            return d
        elif isinstance(obj, Path):
            return dict(__Path__=True, path=str(obj))
        elif isinstance(obj, datetime.datetime):
            d = dict(__datetime__=True, timestamp=obj.timestamp())
            if obj.tzinfo is not None:
                # utcoffset() returns non-None when tzinfo is non-None
                # FIXME this is probably broken, utcoffset needs a datetime
                d['tzoffset']=obj.utcoffset().total_seconds() # type: ignore
                d['tzname']=obj.tzname()
            return d
        elif isinstance(obj, datetime.timedelta):
            return dict(__timedelta_=True, total_seconds=obj.total_seconds())
        else:
            return super().default(obj)

def taketake_json_decode(d):
    if (classname := d.pop("__dataclass__", None)) is not None:
        cls = globals()[classname]
        if is_dataclass(cls):
            return cls(**d)
        else:
            return d
    elif "__Path__" in d:
        return Path(d["path"])
    elif "__datetime__" in d:
        if 'tzoffset' in d:
            tz = datetime.timezone(datetime.timedelta(seconds=d['tzoffset']), name=d['tzname'])
        else:
            tz = None
        # TODO also fix timestamp parsing and formatting functions
        return datetime.datetime.fromtimestamp(d["timestamp"], tz=tz)
    elif "__timedelta__" in d:
        return datetime.timedelta(seconds=d['total_seconds'])
    else:
        return d

def write_json(fpath:Path, obj):
    fpath.write_text(json.dumps(obj, cls=TaketakeJsonEncoder))

def read_json(fpath:Path):
    return json.loads(fpath.read_text(), object_hook=taketake_json_decode)

#============================================================================
# External command infrastructure
#============================================================================

async def communicate(p: asyncio.subprocess.Process, *args, **kwargs) -> subprocess.CompletedProcess:
    """Call p.communicate with the given args.

    Returns a subprocess.CompletedProcess with the results.
    """
    stdout_data, stderr_data = await p.communicate(*args, **kwargs)
    assert p.returncode is not None # communicate ensures this
    return subprocess.CompletedProcess(
            args=p.args, # type: ignore - monkeypatched asyncio.subprocess.Process
            returncode=p.returncode,
            stdout=stdout_data.decode(),
            stderr=stderr_data.decode(),
            )


class ExtCmdListMeta(type):
    """Allow access to instances of derived classes by lookup through the
    derived class's cmds dict.

        E.g.: DerivedClass.foo translates to DerivedClass.cmds["foo"]

    The derived class should inject its instances into the cmds dict itself:

        DerivedClass.cmds[name] = self
    """

    def __getattr__(cls, name):
        return cls.cmds[name]


def fmt_process(cp: subprocess.CompletedProcess) -> str:
    def mlfmt(s):
        lines = s.splitlines()
        return "\n    ".join(lines)

    return f"from {cp.args[0]}\n  cmd: '{' '.join(cp.args)}'\n" \
            f"  stdout:\n    {mlfmt(cp.stdout)}\n" \
            f"  stderr:\n    {mlfmt(cp.stderr)}"


class ExtCmd(metaclass=ExtCmdListMeta):
    """Collect external commands for simple documentation and execution.

    Example:
        proc = await ExtCmd.get_media_duration.run_fg(file=fpath)
    """
    cmds:Dict[str, "ExtCmd"] = {}

    def __init__(self, name:str, doc:str, template:str, **kwargs):
        self.name:str = name
        self.doc:str = doc
        self.template:str = template
        self.params:Dict[str,Any] = kwargs
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

    def run(self, **kwargs):
        args = self.construct_args(**kwargs)
        proc = subprocess.run(args, capture_output=True, text=True)

        if proc.returncode:
            raise SubprocessError(f"Got bad exit code {proc.returncode} {fmt_process(proc)}")
        return proc


    async def exec_async(self, _stdin=None, _stdout=None, _stderr=None, **kwargs):
        args = self.construct_args(**kwargs)

        proc = await asyncio.create_subprocess_exec(*args,
                stdin=_stdin, stdout=_stdout, stderr=_stderr)

        # Make asyncio's Process match the interface of subprocess.Popen
        proc.args = args # type: ignore - monkeypatched asyncio.subprocess.Process
        return proc


    async def run_fg(self, **kwargs):
        proc = await self.exec_async(
                _stdout=asyncio.subprocess.PIPE,
                _stderr=asyncio.subprocess.PIPE,
                **kwargs)

        cp = await communicate(proc)

        if cp.returncode:
            raise SubprocessError(f"Got bad exit code {cp.returncode} {fmt_process(cp)}")

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
    r"""mpv --osd-level=3 --osd-duration=3600000
        --osd-playing-msg=\n{file}{sp}({fsize:,}B)\n\n{sp}Suggested:{sp}{suggested}\n{sp}Final:{sp}{provided}
        --player-operation-mode=pseudo-gui --loop=inf {file} --start={start}""",

    file="The media file to play",
    suggested="The suggested filename for renaming",
    provided="The user-provided filename",
    start="Starting time in seconds (float) for the first non-silence",
    fsize="File size in bytes",
    sp="Space separator character", # Work around limitations in ExtCmd parsing
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
    "cmp_from_stdin",
    "Run cmp to compare source vs. stdin.",
    "cmp {source} -",

    source="Input source file to compare against stdin",
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

ExtCmd(
    "byte_count",
    "Determine the number of bytes in the stdin stream",
    "wc -c",
)


#============================================================================
# External command implementation
#============================================================================

async def flac_encode(wav_fpath, flac_encode_fpath):
    #flac --preserve-modtime 7d29t001.WAV -o 7d29t001.flac
    proc = await ExtCmd.flac_encode.run_fg(infile=wav_fpath, outfile=flac_encode_fpath)
    #print(f"Encoded to {flac_encode_fpath}:", proc.stderr_data.decode())


async def flac_decode(flac_encode_fpath, wav_fpath):
    proc = await ExtCmd.flac_decode.run_fg(infile=flac_encode_fpath, outfile=wav_fpath)
    #print(f"Decoded to {wav_fpath}:", proc.stderr_data.decode())


async def get_flac_wav_size(flac_fpath: Path) -> int:
    """Decode the flac file to count the number of bytes of the resulting wav.

    Does not actually write the wav to disk.
    """
    read_into_wc, write_from_flac = os.pipe()

    p_flacdec = await ExtCmd.flac_decode_stdout.exec_async(
            infile=flac_fpath,
            _stdout=write_from_flac,
            _stderr=asyncio.subprocess.DEVNULL)
    os.close(write_from_flac)  # Allow flac to get a SIGPIPE if wc exits

    p_wc = await ExtCmd.byte_count.exec_async(
            _stdin=read_into_wc,
            _stdout=asyncio.subprocess.PIPE,
            _stderr=asyncio.subprocess.PIPE)
    os.close(read_into_wc)
    cp_wc = await communicate(p_wc)

    if p_wc.returncode:
        raise SubprocessError(f"Got bad exit code {p_wc.returncode} from wc")
    if cp_wc.stderr:
        raise SubprocessError(f"Got unexpected stderr from wc: '{cp_wc.stderr}'")

    await p_flacdec.wait()
    if p_flacdec.returncode:
        raise SubprocessError(f"Got bad exit code {p_flacdec.returncode} from flac")

    return int(cp_wc.stdout.strip())


async def cmp_flac_vs_wav(
        flac_fpath: Path,
        wav_fpath: Path,
        cmp_results_fpath: Path,
) -> bool:
    """Run cmp on the decoded flac_fpath vs the given wav_fpath.

    Write the stdout to cmp_results_fpath.

    This results in an empty file if the comparison succeeds, but an
    indication of the failure if the comparison finds a difference in the
    data.

    Returns True if there is no difference, False on failure.
    """
    with open(cmp_results_fpath, "wb") as f:
        # asyncio subprocess uses StreamReader for asyncio.subprocess.PIPE,
        # so we need to create a pipe manually to link up the subprocesses.
        # See https://stackoverflow.com/a/36666420
        read_into_cmp, write_from_flac = os.pipe()

        p_flacdec = await ExtCmd.flac_decode_stdout.exec_async(
                infile=flac_fpath,
                _stdout=write_from_flac,
                _stderr=asyncio.subprocess.DEVNULL)
        os.close(write_from_flac)  # Allow flac to get a SIGPIPE if cmp exits

        p_cmp = await ExtCmd.cmp_from_stdin.exec_async(
                source=wav_fpath,
                _stdin=read_into_cmp,
                _stdout=f,
                _stderr=asyncio.subprocess.STDOUT)
        os.close(read_into_cmp)

        await p_cmp.wait()
        await p_flacdec.wait()
        return p_cmp.returncode == 0


def check_cmp_results_file(cmp_results_fpath: Path, wav_fpath: Path):
    """Raise CmpMismatch if the given cmp_results_fpath file shows a difference.

    HACK: the PX-S3000 appends an extra 44B chunk at the end of its WAVs that
    flac doesn't transport.  Consider using audiodiff or ffmpeg instead.
    In the meantime, we just ignore any reports of extra bytes.
    """
    if not cmp_results_fpath.exists():
        raise CmpMismatch(
                f"cmp check failed - {cmp_results_fpath} does not exist!")

    elif (contents := cmp_results_fpath.read_text().strip()) != "":
        # Work around the extra 44 byte chunk some pianos append to their WAVs
        #
        filesize = os.path.getsize(wav_fpath)
        if re.match(fr'cmp: EOF on - after byte {filesize - 44}, in line \d+$', contents):
            print(f"*** Warning: WAV file '{wav_fpath}' has an extra 44 bytes not"
                  f" transported by its flac; proceeding anyway since some pianos do this")
        else:
            raise CmpMismatch(
                    f"cmp check failed - Source and target files don't match: {contents}")


def get_nearest_n(x, n):
    """Round up to the nearest non-zero n"""
    rounded = -(-x // n) * n
    return rounded if rounded else n


async def par2_create(f, num_par2_files:int, percent_redundancy:float):
    """Create a par2 set with the given constraints, delete the base .par2

    Use the default Config.par2_base_blocksize unless the resulting number
    of blocks across num_par2_files at the given redundancy would exceed
    Config.par2_max_num_blocks; in that case, ramp up the block size multiple.
    """
    filesize = os.path.getsize(f)
    num_par2_bytes = filesize * num_par2_files * percent_redundancy // 100
    # Par2's blocksize is based on the total file size, not the resulting par2
    min_blocksize = filesize // Config.par2_max_num_blocks
    blocksize = get_nearest_n(min_blocksize, Config.par2_base_blocksize)
    dbg(f"  {filesize=}  {num_par2_bytes=}  {min_blocksize=}  {blocksize=}")

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
    # TODO switch to pathlib.Path
    proc = await ExtCmd.par2_verify.run_fg(file=get_related_par2file(str(f)))


async def par2_repair(f):
    proc = await ExtCmd.par2_repair.run_fg(file=get_related_par2file(f))
    #print("Repaired", proc.exmsg())


def get_file_duration(fpath):
    """Use ffprobe to determine how many seconds the file identified by fpath plays for."""
    proc = ExtCmd.get_media_duration.run(file=fpath)

    if proc.stderr:
        raise InvalidMediaFile(f"Got extra stderr {fmt_process(proc)}")

    try:
        duration = float(proc.stdout)
    except ValueError as e:
        raise InvalidMediaFile(f"Could not parse duration stdout {fmt_process(proc)}") from e

    return duration


def detect_silence(fpath):
    """Use ffmpeg silencedetect to find all silent segments.

    Return a list of TimeRange objects identifying the spans of silence."""

    proc = ExtCmd.ffmpeg_silence_detect.run(
            file=fpath,
            length=Config.file_scan_duration_s,
            threshold=Config.silence_threshold_dbfs,
            duration=Config.silence_min_duration_s)

    detected_lines = [line for line in proc.stderr.splitlines()
                        if line.startswith('[silencedetect')]

    offsets = [float(line.split()[-1]) for line in detected_lines if "silence_start" in line]
    durations = [float(line.split()[-1]) for line in detected_lines if "silence_end" in line]

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
        with open(str(f), "rb") as fd:
            fno = fd.fileno()
            offset = 0
            len = 0
            os.fsync(fno)
            ret = libc.posix_fadvise(fno, offset, len, POSIX_FADV_DONTNEED)
        if ret != 0:
            raise FileFlushError(f"fadvise failed with code {ret}"
                    f"\n  Could not flush file from cache: {f}")

        bytes, pages, fsize, fname = fincore_num_pages(f)

        if pages > 0:
            print(f"*** WARNING *** flushing {f} left {pages} pages in fs caches!""")

        if bytes * Config.fincore_rate_vs_fs > fsize:
            raise FileFlushError(f"After flush, fincore reported more cached bytes "
                    f"than could be read in the time to run fincore!"
                    f"\n  {fname=}"
                    f"\n  {pages} 4KB pages still cached"
                    f"\n  {bytes} bytes still cached"
                    f"\n  {fsize} total file size in bytes"
                    f"\n  {bytes * Config.fincore_rate_vs_fs=}"
                    f"\n  {Config.fincore_rate_vs_fs=}")

def fincore_num_pages(fpath: Path) -> tuple[int, int, int, str]:
    """On an Intel i7-4770, this takes about 6ms per gigabyte.

    At 1GB/s SSD read rate, 6MB could be read in that time.
    That's 1500 4KB pages.
    """
    p = subprocess.run(("fincore", "-nb", str(fpath)),
            capture_output=True, text=True, check=True)
    bytes, pages, fsize, fname = p.stdout.split()
    return int(bytes), int(pages), int(fsize), fname

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
    return list(sorted(other_wavs))

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
    seconds: int = dt.timestamp()
    os.utime(f, (seconds, seconds))

def get_fallback_timestamp(
        fpath:Path,
        fallback_timestamp_mode:str,
        fallback_timestamp_dt:datetime.datetime,
        ) -> datetime.datetime:
    """Returns the mtime, ctime, or atime of the given file in fpath
    if the given fallback_timestamp is one of those words.
    If fallback_timestamp is now, it returns the current time.

    Otherwise, it simply returns the given fallback_timestamp string.

    The return timestamp format is intended to match the
    Config.timestamp_fmt_compact.
    """
    if fallback_timestamp_mode == "now":
        dt = datetime.datetime.now()

    elif fallback_timestamp_mode == "prior":
        # TODO LiveTrak L-12 project support requires using the next parent
        transfer_log_fpath = fpath.parent / Config.transfer_log_fname
        dt = datetime.datetime.fromtimestamp(
                getattr(transfer_log_fpath.stat(), f"st_mtime"))

    elif fallback_timestamp_mode in "atime ctime mtime".split():
        dt = datetime.datetime.fromtimestamp(
                getattr(fpath.stat(), f"st_{fallback_timestamp_mode}"))

    elif fallback_timestamp_mode in "timestamp- timestamp+".split():
        return fallback_timestamp_dt

    else:
        assert False, f"Invalid timestamp mode {fallback_timestamp_mode} for {fpath}"

    return dt

def inject_timestamp(template: str, when: datetime.datetime=None) -> str:
    """Format the time into the given template string.

    template must contain a single {} which indicates where the timestamp goes.
    Config.timestamp_fmt_compact is used to format the time.
    If when is None, the current time is encoded.
    Otherwise, when should be an object for which strftime is defined.
    """
    if when is None:
        when = datetime.datetime.now()
    return template.format(when.strftime(Config.timestamp_fmt_compact))

class ParsedTimestamp(NamedTuple):
    matchobj: re.Match # note: matchobj.start('timestamp') or .end('weekday')
    timestamp: datetime.datetime
    weekday_correct: Optional[bool]

def extract_timestamp_from_str(s:str) -> Optional[ParsedTimestamp]:
    """Finds and parses the timestamp from the given string.

    Looks for timestamps of the form YYYYmmdd-HHMM(SS).

    Returns the ParsedTimestamp result, or None if the parse failed.
    """
    if m := Config.timestamp_re.search(s):
        timedict = {k: int(v) for k, v in m.groupdict().items()
                if v and (k in "year month day hour minute second".split())}

        if m['timezone']:
            # Use the parsed timezone
            timedict['tzinfo'] = datetime.datetime.strptime(m['timezone'], "%z").tzinfo
        else:
            # Determine the current timezone
            timedict['tzinfo'] = datetime.datetime.now().astimezone().tzinfo

        dt = datetime.datetime(**timedict)

        expect_weekday = dt.strftime("%a")
        if m['weekday'] is not None:
            weekday_correct = m['weekday'].lower() == expect_weekday.lower()
        else:
            weekday_correct = None

        return ParsedTimestamp(
                matchobj=m,
                timestamp=dt,
                weekday_correct=weekday_correct)

    return None

def parse_timestamp(s:str) -> Optional[datetime.datetime]:
    """Uses extract_timestamp_from_str to interpret s as a timestamp.

    Returns the datetime.datetime result if parsing succeeds, or None if
    parsing fails or if there are any leading or trailing characters,
    or if the weekday mismatches.
    """
    tsinfo = extract_timestamp_from_str(s)
    if tsinfo and tsinfo.matchobj.start() == 0 \
              and tsinfo.matchobj.end() == len(s) \
              and (not tsinfo.matchobj['weekday'] or tsinfo.weekday_correct):
        return tsinfo.timestamp
    else:
        return None


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


def find_likely_audio_span(fpath: Path, scan_to_s: float) -> TimeRange:
    """Searches for regions of silence in fpath.
    Scans only the first scan_to_s seconds.

    Returns a TimeRange representing the likely timestamp readout.

    This TimeRange is the first non-silent span of audio that is considered
    long enough, expanded a bit to cover any attack or decay in the speech.

    Raises NoSuitableAudioSpan if no likely candidate was found.
    """

    silences = detect_silence(fpath)
    non_silences = invert_silences(silences, scan_to_s)

    for r in non_silences:
        duration = r.duration
        if duration >= Config.min_talk_duration_s:
            # Expand the window a bit to allow for attack and decay below the
            # silence threshold.
            r.start = max(0, r.start - Config.talk_attack_s)
            duration += Config.talk_attack_s + Config.talk_release_s
            duration = min(duration, Config.max_talk_duration_s)
            return TimeRange(r.start, duration)

    raise NoSuitableAudioSpan(f"Could not find any span of audio greater than "
                              f"{Config.min_talk_duration_s}s in file '{fpath}'")


#============================================================================
# Speech recognition and parsing
#============================================================================

def process_speech(fpath: Path, speech_range: TimeRange) -> Optional[str]:
    """Uses the PocketSphinx speech recognizer to decode the spoken timestamp
    and any notes.

    Returns the resulting text, or None if no transcription could be determined.

    This is called in a separate thread so as to not block the asyncio loop.
    """
    recognizer = speech_recognition.Recognizer()

    with speech_recognition.AudioFile(str(fpath)) as audio_file:
        speech_recording = recognizer.record(audio_file,
                                             offset=speech_range.start,
                                             duration=speech_range.duration)
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore",
                    message="the imp module is deprecated",
                    #module="ad_pulse",
                    category=DeprecationWarning)
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
    corrections = {"why": "one", "oh": "zero", "to": "two"}
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


def grok_time_words(word_list: list[str]) -> tuple[int, int, int, str, list[str]]:
    """Returns (hour, minutes, seconds, timezone, extra) from the word_list

    If no timezone is present, "" is returned.  Timezone words supported:

     * zulu - UTC (universal coordinated time)
     * local - local timezone

    The final list "extra" contains any unparsed words.
    """
    done = False
    second = None

    # Parse hour
    hour = grok_digit_pair(word_list)
    if pop_optional_words(word_list, "second seconds"):
        # ... but that was actually seconds
        second = hour
        hour = 0
        done = True

    if not done and pop_optional_words(word_list, "minute minutes"):
        # ... but that was actually minutes
        minute = hour
        hour = 0
        pop_optional_words(word_list, "and")

    else:
        pop_optional_words(word_list, "hundred hour hours oh clock oclock o'clock and")

        # Parse minute
        minute = grok_digit_pair(word_list)
        if pop_optional_words(word_list, "second seconds"):
            # ... but that was actually seconds
            second = minute
            minute = 0
            done = True
        else:
            pop_optional_words(word_list, "oh clock oclock o'clock minute minutes and")

    if not done:
        # Parse seconds
        second = grok_digit_pair(word_list)
        pop_optional_words(word_list, "second seconds")

    timezone = pop_optional_words(word_list, "zulu local")

    assert second is not None
    return hour, minute, second, timezone, list(word_list)


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


def words_to_timestamp(text: str) -> tuple[datetime.datetime, list[str]]:
    """Converts the given text to a feasible timestamp, followed by any
    remaining comments or notes encoded in the time string.

    Returns a pair of (datetime, str) containing the timestamp and any comments.
    """
    # Sample recognized text for this TalkyTime setup:
    #   format:  ${hour}:${minute} timezone. ${weekday}. ${month} ${day}, ${year}
    #   example: 19:38 zulu. Wednesday. May 19, 2021

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
    hour, minute, second, timezone, extra = grok_time_words(time_words)
    #print(f"-> {hour:02d}:{minute:02d}:{second:02d} (extra: {extra})")

    if extra:
        raise TimestampGrokError(f"Invalid extra words (timezone?) "
                f"'{' '.join(extra)}' in '{text}'")

    #print(f"  Date: {date_words}")
    year, month, day, day_of_week, extra = grok_date_words(date_words)
    #print(f"-> {year}-{month}-{day} {day_of_week} (extra: {extra})")
    #print(f"-> {year:04d}-{month:02d}-{day:02d} {day_of_week} (extra: {extra})")

    match timezone:
        case 'zulu':
            tz=datetime.timezone.utc
        case 'local':
            tz=None
        case '':
            tz=None
        case _:
            raise TimestampGrokError(f"Invalid timezone {timezone} in '{text}'")

    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=tz), extra


#============================================================================
# File Audio processing
#============================================================================

def extract_timestamp_from_audio(fpath:Path, audioinfo:AudioInfo) -> None:
    """Runs speech-to-text on the given audio file fpath.

    audioinfo must have the duration_s field already filled in
    duration_s specifies the runtime of the audio file in float seconds
    """

    # Only scan the first bit of the file to avoid transfering a lot of data.
    # This means we can prompt the user for any corrections sooner.
    assert audioinfo.duration_s is not None
    scan_duration = min(audioinfo.duration_s, Config.file_scan_duration_s)

    audioinfo.speech_range = find_likely_audio_span(fpath, scan_duration)
    print(f"Speechinizer: {fpath.name} - processing audio at {audioinfo.speech_range}")
    audioinfo.recognized_speech = process_speech(fpath, audioinfo.speech_range)
    audioinfo.parsed_timestamp, audioinfo.extra_speech \
            = words_to_timestamp(audioinfo.recognized_speech)
    dbg(f"Speechinizer: {fpath.name} Done - {audioinfo}")


def format_duration(duration: float | datetime.timedelta, style:str='letters') -> str:
    """Returns a string of the form XhYmZs given a duration in seconds.

    style is one of:
        letters: 5h2s
        colons: 5:00:02, 0:00:03

    The duration s is first rounded to the nearest second.
    If any unit is 0, omit it, except if the duration is zero return 0s.
    """

    if isinstance(duration, datetime.timedelta):
        duration = duration.total_seconds()

    parts: list[str] = []
    match style:
        case 'letters':
            intdur = round(duration)     # now an int
        case 'colons':
            intdur = int(duration)
        case _:
            assert False, f"Invalid style '{style}', should be 'letters' or 'colons'"

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
            value = intdur
        else:
            value = intdur % multiple
            intdur //= multiple  # int division
        if style == 'letters':
            if value or (not parts and intdur == 0):
                parts.append(f"{value}{unit}")
        elif style == 'colons': # pragma: no branch
            if unit in "sm":
                parts.append(f"{value:02}")
            else:
                parts.append(f"{value}")

    parts.reverse()
    match style:
        case 'letters':
            return ''.join(parts)
        case 'colons':
            s = ':'.join(parts)
            frac = round(duration - int(duration), 2)
            if frac:
                s += str(frac)[1:]
            return s
        case _:
            assert False


def format_dest_filename(xinfo:TransferInfo) -> str:
    """Returns an extensionless pathless filename string."""

    assert xinfo.timestamp is not None
    assert xinfo.audioinfo is not None
    assert xinfo.audioinfo.duration_s is not None
    assert xinfo.audioinfo.extra_speech is not None

    # TODO - consider adding a command line argument to specify the target timezone
    dt = xinfo.timestamp.astimezone(xinfo.target_timezone)
    timestamp_str = dt.strftime(Config.timestamp_fmt_compact)
    duration_str = format_duration(xinfo.audioinfo.duration_s)
    if xinfo.audioinfo.extra_speech:
        notes = "-".join(xinfo.audioinfo.extra_speech) + "."
    else:
        notes = ""

    return Config.dest_fname_fmt.format(
            prefix=Config.prefix,
            datestamp=timestamp_str,
            guess_tag=xinfo.timestamp_guess_direction,
            notes=notes,
            duration=duration_str,
            instrument=xinfo.instrument,
            orig_fname=xinfo.source_wav.stem.lower())


#============================================================================
# Timezone utilities
#============================================================================

def parse_timezone(tzstr:str) -> Optional[datetime.tzinfo]:
    """Returns the timezone as specified by the --target-timezone argument.

    None is returned if the local timezone is requested.
    """

    if not tzstr or tzstr.lower() in "local list".split():
        return None
    elif Config.timezone_offset_re.match(tzstr):
        return datetime.datetime.strptime(tzstr, "%z").tzinfo
    elif tzstr.lower() in "z zulu".split():
        return datetime.timezone.utc
    else:
        return zoneinfo.ZoneInfo(tzstr)

def list_timezones():
    now = datetime.datetime.now()
    zones = {z: now.astimezone(zoneinfo.ZoneInfo(z)).strftime("%z")
            for z in zoneinfo.available_timezones()}
    sorted_zones = {z: offset for z, offset in
            sorted(zones.items(), key=lambda pair: f"{pair[1]}.{pair[0]}")}
    for z, offset in sorted_zones.items():
        print(f"{offset}: {z}")

#============================================================================
# Signaling interface
#============================================================================

class StepperQueue(asyncio.Queue):
    def __init__(self, name, qtype):
        self.name = name
        self.qtype = qtype
        self.src = False
        self.dest = False
        self.pending: set = set() # Set of tokens pending for synchronization
        self.getter: Optional[asyncio.Task] = None # Queue.get() task from the last get()
        self.done = False  # Set True when the end token is seen
        super().__init__()

    def info(self):
        return f"{self.name}[{self.qtype}]"

    def __str__(self):
        return f"StepperQueue({self.info()}, src={self.src}, dest={self.dest})"

class LinkQDict(collections.UserDict):
    """named dictionary mapping coro->coro links to queues"""
    def __init__(self, name):
        self.name = name
        super().__init__()

    def fmtdata(self):
        return "\n      ".join(f"{k}: {q}" for k, q in self.data.items())

    def __str__(self):
        return f"LinkQDict({self.name}):\n      {self.fmtdata()}"

def make_queues(s:str) -> LinkQDict:
    """Make a StepperQueue for each word in s.

    Returns a dict keyed off of the given names, but also with
    attributes set based on those names so they are dot accessible.
    """
    qdict = LinkQDict("noname")
    for qname in s.split():
        qdict[qname] = StepperQueue(qname, "any")
        setattr(qdict, qname, qdict[qname])
    return qdict

def listify(arg) -> Sequence[Any]:
    if arg is None:
        return []
    elif isinstance(arg, str) or isinstance(arg, bytes):
        return [arg]
    elif isinstance(arg, Sequence):
        return arg
    elif isinstance(arg, Iterable):
        return list(arg)
    else:
        return [arg]

def format_steps(steps, sep=", "):
    steplist = listify(steps)
    return sep.join(step.__name__ for step in steplist)

class Stepper:
    """Manage a set of StepperQueue instances for coordinating stepping a sequence.

    The end of the sequence should be indicated be the given end token.
    Each sync_ and send_ parameter should be None, a StepperQueue with a
    .name attr as returned by make_queues, or a list of such.

    The step() and walk() methods provide high-level interfaces for easily
    building a network of tasks with the same set of data flowing through
    each.
    """

    def __init__(self, name=None, end='END',
            sync_from=None, pull_from=None,
            send_to=None, sync_to=None,
            cancellation_exception_type: None | type[RuntimeError] | tuple[type[RuntimeError], ...]=None,
            cancel_check_fn: None | Callable=None,
            cancel_token_fn: None | Callable=None,
            squash_canceled_tokens: bool=False,
            ):

        self.name: str = name
        self.sync_from: Sequence[StepperQueue] = listify(sync_from)
        self.pull_from: Sequence[StepperQueue] = listify(pull_from)
        self.send_to: Sequence[StepperQueue] = listify(send_to)
        self.sync_to: Sequence[StepperQueue] = listify(sync_to)
        self.end: Hashable = end

        self.cancellation_exception_type = cancellation_exception_type
        self.cancel_check_fn = cancel_check_fn
        self.cancel_token_fn = cancel_token_fn
        self.squash_canceled_tokens = squash_canceled_tokens

        self.value: Hashable = None # last gotten value
        self.pre_sync_met: bool = False

        # TODO - these get patched in from the StepNetwork, but maybe should
        # be explicitly passed in through the constructor.
        self.args: Optional[Sequence[Any]] = None
        self.kwargs: Optional[dict] = None

    def __str__(self):
        q_info = []
        for qname in 'sync_from pull_from send_to sync_to'.split():
            q_info.append(f"{qname}=[{self.fmtqueues(getattr(self, qname))}]")
        return f"Stepper({self.name}:{self.value}, " \
                f"{', '.join(q_info)})"

    def log(self, *args, **kwargs):
        dbg(f"Stepper<{self.name}:{self.value}> :", *args, depth=1, **kwargs)

    def fmtqueues(self, queues):
        return ", ".join(q.name for q in queues)

    class QueueError(RuntimeError): ...
    class GetError(QueueError): ...
    class PreSyncTokenError(QueueError): ...
    class DesynchronizationError(QueueError): ...
    class DuplicateTokenError(QueueError): ...

    async def _get_across(self, q_list: Sequence[StepperQueue], qtype:str) -> Any:
        """Gets the next token that matches across all the queues in the q_list"""
        if self.value == self.end:
            raise self.GetError(f"Step task '{self.name}' called get(), but it "
                    f"already saw the end token {self.end}"
                    f"\n    in _get_across({qtype}) in {self}")

        if not q_list:
            return None
        # Gather up all the queues' get() tasks into a dict mapping the task
        # back to the queue.  Also create any new get() tasks as needed.
        tasks = {}
        for q in q_list:
            if not q.done:
                if q.getter is None:
                    q.getter = asyncio.create_task(q.get())
                tasks[q.getter] = q

        # Loop until we get a token that's been emitted by all queues
        while True:
            # First check if any tokens have been seen by all.
            # We must do this before waiting for any more tokens because a
            # prior call to _get_across may have gotten different
            # tokens from multiple queues from the same wait call.
            tokens_done = set.intersection(*[q.pending for q in q_list])

            if self.end in tokens_done:
                # We do this check every time after an end token is received
                # across all queues, but that's probably fine perf-wise.
                # It's not an expected case.
                for q in q_list:
                    if tokens_done != q.pending:
                        # Check failed - gather mismatch info across all queues
                        queue_tokens = []
                        all_extras = set()
                        for check_q in q_list:
                            extra_tokens = check_q.pending - tokens_done
                            queue_tokens.append(
                                    f"\n      {check_q.info()}: {sorted(extra_tokens)}")
                            all_extras |= extra_tokens
                        queue_report_str = "".join(queue_tokens)

                        raise Stepper.DesynchronizationError(
                                f"Mismatching tokens between {qtype}-queues detected in "
                                f"Stepper({self.name})"
                                f"\n  Got the end token {self.end} from all input queues."
                                f"\n  Un-emitted tokens matching across all input queues: "
                                f"{sorted(tokens_done - {self.end})}"
                                f"\n  Extra tokens only in some queues: {sorted(all_extras)}"
                                f"\n  Extra tokens in each input queue:"
                                f"{queue_report_str}")

            # If there is a token ready across all queues, emit it
            if tokens_done:
                self.log(f"[[[ready tokens: {tokens_done}]]]")
                token = tokens_done.pop()
                if self.squash_canceled_tokens:
                    assert not self.is_canceled(token), f"{token=}, {self}"

                if token == self.end:
                    if tokens_done:
                        # Don't emit the end-token before emiting all other tokens
                        token = tokens_done.pop()

                # Clean out the token we're emiting across all input queues' pending lists
                for q in q_list:
                    q.pending.remove(token)

                # TODO - we need to emit tokens in the order they are received,
                # Though this is probably already assured assuming all input
                # queues operate in the same order, which is good enough.
                return token

            # Wait the get() tasks queued up on all non-finished input queues,
            # pushing any tokens retrieved into the pending token lists for its input queue
            self.log(f"[[[waiting for {len(tasks)} tasks]]]")
            if tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    token = task.result()
                    q = tasks.pop(task)
                    q.task_done()

                    if token == self.end:
                        q.getter = None
                        q.done = True
                    else:
                        # Set up non-finished queues for subsequent getting
                        # the next time around.
                        q.getter = asyncio.create_task(q.get())
                        tasks[q.getter] = q

                    if token in q.pending:
                        raise Stepper.DuplicateTokenError(
                                f"Duplicate token {token} from {q.info()} "
                                f"queue detected in Stepper({self.name})"
                                f"\n  tokens still pending in {q.info()}: "
                                f"{sorted(q.pending - {self.end})}")

                    q.pending.add(token)
                    self.log(f"[[[got {token} <= {q}]]]")

                if self.squash_canceled_tokens and len(done) > 0:
                    self._expunge_canceled_tokens(q_list)

    def _expunge_canceled_tokens(self, q_list):
        # Expunge canceled tokens.  Not super efficient, this...  TODO
        pending = set().union(*(q.pending for q in q_list))
        # TODO build a global persistent set of canceled tokens instead?
        canceled = set(token for token in pending if self.is_canceled(token))
        if len(canceled) > 0:
            for q in q_list:
                q_canceled = canceled & q.pending
                if len(q_canceled) > 0:
                    q.pending -= q_canceled
                    self.log(f"[[[EXPUNGING canceled tokens {sorted(q_canceled)} "
                             f"from {q.info()}, leaving "
                             f"{sorted(q.pending - {self.end})}]]]")

    def is_canceled(self, token: Hashable) -> bool:
        assert token is not None, f"{self}"
        return isinstance(self.cancel_check_fn, Callable) \
            and token != self.end \
            and self.cancel_check_fn(token)

    async def sync_end(self):
        """Wait for end on all sync_from Queues.

        raises Stepper.DesynchronizationError if the sync_from queues emit
        mismatching tokens.

        raises Stepper.PreSyncTokenError if the sync_from queues emit a non-end
        token.
        """
        if not self.pre_sync_met:
            if len(self.sync_from) > 0:
                while (token := await self._get_across(self.sync_from, 'sync')) != self.end:
                    raise Stepper.PreSyncTokenError(
                            f"Got non-end token {token!r} from sync_from queues"
                            f" [{self.fmtqueues(self.sync_from)}]\n    {self}")
            self.log(f"[<= sync_from({self.fmtqueues(self.sync_from)})]")
            self.pre_sync_met = True


    async def get(self):
        """Pre-sync, then wait for all pull_from queues to emit their next token.

        Raises Stepper.DesynchronizationError if the pull_from queues don't
        all report matching tokens.
        """
        await self.sync_end()
        self.value = await self._get_across(self.pull_from, 'token')
        self.log(f"[got {self.value} <= pull_from({self.fmtqueues(self.pull_from)})]")
        return self.value


    async def put(self, token: Hashable):
        """Put token into each send_to queue.

        If the token is the end token, put it in the sync_to queue.
        """
        if self.squash_canceled_tokens and self.is_canceled(token):
            self.log(f"[put {token} => SQUASHED (token has been canceled)]")
            return

        for q in self.send_to:
            await q.put(token)
        if self.send_to:
            self.log(f"[put {token} => send_to({self.fmtqueues(self.send_to)})]")

        if token == self.end:
            for q in self.sync_to:
                await q.put(token)
            if self.sync_to:
                self.log(f"[=> sync_to({self.fmtqueues(self.sync_to)})]")


    @contextmanager
    def cancellable(self, token: Hashable) -> Generator:
        """Usage:

            with stepper.cancellable(token) as s:
                s.skip_if_canceled()
                # Do things that could raise the cancellation_exception_type
        """

        class SkipExecution(BaseException): ...
        class NullException(BaseException): ...

        class Skipper:
            @staticmethod
            def skip_if_canceled():
                if self.is_canceled(token):
                    raise SkipExecution

        if self.cancellation_exception_type:
            exceptions_to_catch = self.cancellation_exception_type
        else:
            exceptions_to_catch = NullException

        try:
            yield Skipper

        except SkipExecution:
            self.log(f"[SKIPPED canceled token {token}]")

        except exceptions_to_catch as e:
            # pyright thinks RuntimeError needs to be iterable here, but only
            # when passed through a variable?
            if self.cancel_token_fn:
                self.cancel_token_fn(token, e, self)


    async def step(self):
        """Simplify the standard while loop idiom.

        Equivalent to:
            while (i := await stepper.get()) is not None:
                # Do task work
                await stepper.put(i)
            await stepper.put(stepper.end)
        """
        if self.value is not None and self.value != self.end:
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
            with self.cancellable(token=self.value) as s:
                s.skip_if_canceled()
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


class StepNetwork:
    """Auto-wired DAG of Steppers and their step coroutines"""
    side_q_names: dict[tuple[str, str], str] = {
            ("dest", "sync"): "sync_to",
            ("dest", "token"): "send_to",
            ("src", "token"): "pull_from",
            ("src", "sync"): "sync_from",
        }

    def __init__(self, name: str, end: Hashable='END',
            cancellation_exception_type: None | type[RuntimeError] | tuple[type[RuntimeError], ...]=None,
            cancel_check_fn: None | Callable=None,
            cancel_token_fn: None | Callable=None,
            squash_canceled_tokens: bool=False,
            ):

        self.name = name
        self.end = end
        self.common_kwargs: dict[str, Any] = {}

        self.cancellation_exception_type = cancellation_exception_type
        self.cancel_check_fn = cancel_check_fn
        self.cancel_token_fn = cancel_token_fn
        self.squash_canceled_tokens = squash_canceled_tokens

        # Coroutine to Stepper instance maps
        self.tasks = {}
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
        """Pass the given keyword arguments to each task/step coroutine."""
        self.common_kwargs |= kwargs

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
        if isinstance(src, Callable):
            src.targets |= set(dest)
            for item in dest:
                link = Link(src, item)
                q = self._map_link_to_queue(link, qdict, "src")
                stepper_qlist.append(q)

        elif isinstance(dest, Callable):
            for item in src:
                link = Link(item, dest)
                q = self._map_link_to_queue(link, qdict, "dest")
                stepper_qlist.append(q)

        else:
            assert False, f"Niether {src} nor {dest} are functions!" \
                    f"\n  {qdict}"

    def add(self, coro, *args,
            sync_from=None, pull_from=None,
            send_to=None, sync_to=None, **kwargs):
        """Add a task coroutine to the StepNetwork.

        If the given coro is marked with the @stepped_task decorator,
        then it will be automatically stepped via Stepper.walk.

        The [sync/pull/send]_[to/_from] arguments specify a single
        task/step, or a list of them, from which the
        correspending queues will be hooked up between the steppers.

        When execute() is called, the queues will be created and hooked into
        the list of Steppers.
        """
        self.seen_coroutines.add(coro)

        stepper = Stepper(name=coro.__name__, end=self.end,
            cancellation_exception_type=self.cancellation_exception_type,
            cancel_check_fn=self.cancel_check_fn,
            cancel_token_fn=self.cancel_token_fn,
            squash_canceled_tokens=self.squash_canceled_tokens,
            )
        stepper.args = args
        stepper.kwargs = kwargs
        coro._stepper = stepper # for testing

        if hasattr(coro, 'is_stepped') and getattr(coro, 'is_stepped'):
            assert pull_from is not None, \
                    f"step {stepper.name} needs a pull_from source"
            self.steps[coro] = stepper
        else:
            self.tasks[coro] = stepper

        coro.targets = set() # Build out the graph for cycle detection

        self._add_link_queues(stepper.sync_from, self.sync_queues,
                src=listify(sync_from), dest=coro)

        self._add_link_queues(stepper.pull_from, self.token_queues,
                src=listify(pull_from), dest=coro)

        self._add_link_queues(stepper.send_to, self.token_queues,
                src=coro, dest=listify(send_to))

        self._add_link_queues(stepper.sync_to, self.sync_queues,
                src=coro, dest=listify(sync_to))

    def add_pipeline(self, *args,
            sync_from=None, pull_from=None,
            send_to=None, sync_to=None, **kwargs):
        """Implicitly wire up a pipeline of steps.

        The args param is a list of coroutine steps.  add() is called on each.
        The sync_from and pull_from params are applied to the first step in args.
        The send_to and sync_to params are applied to the final step in args.
        Additional keyword arguments in kwargs are passed to each step on execution.

        Each step is chained into the pipeline to its prior and successor step
        via autogenerated pull_from and send_to queues.
        """
        assert len(args) > 1
        self.add(args[0], sync_from=sync_from, pull_from=pull_from,
                send_to=args[1], **kwargs)

        for i in range(1, len(args) - 1):
            self.add(args[i], pull_from=args[i-1], send_to=args[i+1], **kwargs)

        self.add(args[-1], pull_from=args[-2],
                send_to=send_to, sync_to=sync_to, **kwargs)

    def check_queue_wiring(self, qdict):
        for link, q in qdict.items():
            for side in link._fields:
                assert getattr(q, side), \
                    f"missing {self.fmt_linkerr(link, side, qdict)}"

    class HasCycle(Exception):
        def __init__(self, msg, gray_vertex):
            super().__init__(msg)
            self.gray_vertex = gray_vertex
            self.start_found = False
            self.path = []

        def __str__(self):
            return f"{super().__str__()}:{format_steps(reversed(self.path), sep='->')}"

    def check_for_cycles(self):
        # Create a fake vertex containing all the other vertices.  This
        # simplifies cycle trace building since this way we only need the
        # exception manager in depth_first_visit - the backedge will never
        # point to the fake vertex.
        def fake(): pass
        fake.targets = [*self.tasks.keys(), *self.steps.keys()]
        for v in fake.targets:
            v.color = "white"
        self.depth_first_visit(fake)

    def depth_first_visit(self, u):
        u.color = "gray"
        dbg(f"Set color {u.__name__}({u.color}) -> [{format_steps(u.targets)}]")
        for v in u.targets:
            dbg(f"{u.__name__}({u.color}) -> {v.__name__}({v.color})")
            if v.color == "white":
                try:
                    self.depth_first_visit(v)
                except StepNetwork.HasCycle as e:
                    if not e.start_found:
                        e.path.append(v)
                    if v == e.gray_vertex:
                        e.start_found = True
                    raise
            elif v.color == "gray":
                raise StepNetwork.HasCycle(
                    f"found backedge {u.__name__}->{v.__name__}", v)
        u.color = "black"
        dbg(f"Set color {u.__name__}({u.color})")

    def check_queues(self):
        """Ensure all queues are wired up properly, assert if not."""
        # Ensure we have symmetrical from/to wirings
        self.check_queue_wiring(self.sync_queues)
        self.check_queue_wiring(self.token_queues)

        # Ensure there are no cycles using depth-first search
        self.check_for_cycles()

    async def execute(self):
        """Run asyncio.gather on the task and step coroutines.

        execute matches the send/pull/sync queues across the Steppers in
        the network, and then gathers across the added task coroutines
        and the Stepper.walk() coroutine for each step.
        """
        self.check_queues()

        tasks = []
        for coro, stepper in self.tasks.items():
            tasks.append(coro(*stepper.args, stepper=stepper,
                              **stepper.kwargs,
                              **self.common_kwargs))

        for step_coro, stepper in self.steps.items():
            tasks.append(stepper.walk(step_coro, *stepper.args,
                                      **stepper.kwargs,
                                      **self.common_kwargs))

        await asyncio.gather(*tasks)

def stepped_task(coro:Callable) -> Callable:
    """Decorator to mark coro as stepped task."""
    coro.is_stepped = True
    return coro


#============================================================================
# Filename prompting coroutines
#============================================================================

def play_media_file(xinfo:TransferInfo) -> subprocess.Popen | None:
    """Play the given file in a background process."""

    start = 0.0
    assert xinfo.audioinfo is not None
    if xinfo.audioinfo.speech_range is not None:
        start = xinfo.audioinfo.speech_range.start

    # Try the encoded flac file first to avoid unneeded USB transfer
    fpath = xinfo.wav_progress_dir / Config.flac_encoded_fname

    # Fall back to the wav file on the source drive
    if not fpath.exists():
        fpath = xinfo.source_wav

    # Fall back to any in-progress flac if one exists from a prior run
    if not fpath.exists():
        fpath = xinfo.wav_progress_dir / Config.flac_progress_fname

    if not fpath.exists():
        print(f"Could not find a file to play: {xinfo.source_wav}")
        return None

    args = ExtCmd.play_media_file.construct_args(
            file=fpath,
            start=start,
            suggested=xinfo.fname_guess,
            provided=xinfo.fname_prompted,
            fsize=os.path.getsize(fpath),
            sp="  ")

    null = subprocess.DEVNULL
    return subprocess.Popen(args, stdin=null, stdout=null, stderr=null)

# Number of seconds per named unit
short_timedelta_units = dict(
        y=3600*24*365.25,
        mo=3600*24*365.25/12,
        wk=3600*24*7,
        day=3600*24,
        hr=3600,
        min=60,
        s=1,
        ms=0.001,
        us=0.000001,
)

def short_timedelta(td: datetime.timedelta, prec:int=1) -> str:
    s = td.total_seconds()
    for unit, units_per_s in short_timedelta_units.items():
        if abs(s) >= units_per_s:
            unit_value = s/units_per_s
            return f"{s/units_per_s:.1f}{unit}"

    return f"0s"

async def prompt_for_filename(xinfo:TransferInfo):
    from prompt_toolkit import PromptSession, print_formatted_text
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.styles import Style
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.enums import DEFAULT_BUFFER
    from prompt_toolkit.validation import Validator, ValidationError
    import html

    def toolbar():
        text = get_app().layout.get_buffer_by_name(DEFAULT_BUFFER).text

        tsinfo = extract_timestamp_from_str(text)
        strs = [f"<comment>{len(text)}c {len(text.encode())}B</comment>"]

        if len(text.encode()) > Config.max_filename_len:
            strs.append(f"<style fg='ansired'>&gt; {Config.max_filename_len}B MAX</style>")

        if tsinfo and tsinfo.timestamp:
            strs.append(f"Parsed {tsinfo.timestamp.strftime(Config.timestamp_fmt_long)}: ")
            age = datetime.datetime.now().astimezone() - tsinfo.timestamp.astimezone()
            if age < datetime.timedelta(0):
                strs.append("<style fg='ansired'>That's "
                        f"{short_timedelta(-age)} "
                        "in the future!</style>")
            elif age.total_seconds() > short_timedelta_units['y']:
                strs.append("<style fg='ansired'>That's "
                        f"{short_timedelta(age)} "
                        "in the past!</style>")
            else:
                strs.append(f"({short_timedelta(age)} ago)")

            assert xinfo.audioinfo is not None
            ai_timestamp = xinfo.audioinfo.parsed_timestamp
            if ai_timestamp:
                delta = tsinfo.timestamp - ai_timestamp
                strs.append(f"({short_timedelta(delta)} from guess)")

        if Config.illegal_filechar_re.search(text):
            strs.append(
                f"<style fg='ansired'>ERROR:</style> illegal chars in "
                f"{html.escape(Config.illegal_filechar_re.pattern)} not allowed!")

        if not tsinfo:
            strs.append(f"<style fg='ansired'>WARNING:</style> No timestamp found!")

        elif tsinfo.timestamp is None:
            strs.append(f"<style fg='ansired'>WARNING:</style> Timestamp couldn't be parsed!")

        elif tsinfo.matchobj['weekday'] is None:
            strs.append(f"<style fg='ansired'>WARNING:</style> "
                    f"No weekday in '{tsinfo.matchobj['fulltime']}' found!")

        elif tsinfo.weekday_correct:
            strs.append(f"<style bg='ansigreen'>Weekday matches</style>")

        else:
            strs.append(f"<style fg='ansired'>WARNING:</style> "
                    f"Weekday mismatch: timestamp is on a "
                    f"<style bg='ansigreen'>{tsinfo.timestamp.strftime('%A')}</style>, "
                    f"but the text says "
                    f"<style bg='ansired'>{tsinfo.matchobj['dayname']}</style>")

        return HTML("  ".join(strs))

    class FilenameValidator(Validator):
        def validate(self, document):
            text = document.text
            if (m := Config.illegal_filechar_re.search(text)):
                raise ValidationError(
                        message=f"Filename must not contain {m.re.pattern}",
                        cursor_position=m.start())

            if text.endswith('.'):
                raise ValidationError(
                        message=f"Filename must not end with .",
                        cursor_position=len(text) - 1)

            if len(text.encode()) > Config.max_filename_len:
                raise ValidationError(
                        message=f"Filename too long - limit is {Config.max_filename_len}B",
                        cursor_position=len(text) - 1)

            if (m := re.search(r'\s', text)):
                text = re.sub(r',\s+', ',', text)
                text = re.sub(r'\s+', '-', text)
                get_app().layout.get_buffer_by_name(DEFAULT_BUFFER).text = text
                raise ValidationError(
                        message="Filename should not contain spaces " \
                            "(fixing; you must make an edit to confirm, e.g. del -)",
                        cursor_position=m.start())

    bindings = KeyBindings()
    @bindings.add('escape', 'h')
    def _(event):
        play_media_file(xinfo)

    style = Style.from_dict(dict(
        prompt="#eeeeee bold",
        fname="#bb9900",
        comment="#9999ff",
        guess="#dddd11 bold",
        input="#33ff33 bold",
        ))

    assert xinfo.fname_guess is not None
    assert xinfo.audioinfo is not None
    session: PromptSession = PromptSession(
            key_bindings=bindings,
            style=style,
            mouse_support=True,
            bottom_toolbar=toolbar,
            auto_suggest=AutoSuggestFromHistory(),
            validator=FilenameValidator(),
            validate_while_typing=False,
            )
    with patch_stdout():
        xinfo.fname_prompted = Path(await session.prompt_async(
            HTML(
                f"<prompt>* Confirm file rename for</prompt> "
                    f"<fname>{xinfo.source_wav}</fname>"
                f"\n<prompt>* Recognized speech:</prompt> "
                    f"'<comment>{xinfo.audioinfo.recognized_speech}</comment>' "
                    f"@<guess>{xinfo.audioinfo.speech_range}</guess> "
                f"\n <guess>Guess</guess>: <fname>{html.escape(xinfo.fname_guess)}</fname> "
                f"<comment>({len(xinfo.fname_guess)} chars)</comment>"
                f"\n <input>Input&gt;</input> "),
            default=xinfo.fname_guess,
            ))


#===========================================================================
# Support functions for the steps
#============================================================================

def act(msg):
    """Logs the given message according to whether it will be executed or not.

    Returns Config.act

    This should be used to protect any code that modifies the filesystem.
    """
    dbg(f"{'Running' if Config.act else 'Skip (noact)'} :", msg, depth=1)
    return Config.act


def listen_to_wav(xinfo:TransferInfo, token:int) -> AudioInfo:
    """Do speech to text on the given workunit read from the inq.

    If a .audioinfo.json progress file exists, use that instead.
    """
    idstr = f"listen_to_wav({xinfo.source_wav.name})[{token}]"
    audioinfo_fpath = xinfo.wav_progress_dir / Config.audioinfo_fname

    if audioinfo_fpath.exists():
        audioinfo = read_json(audioinfo_fpath)
        dbg(f"{idstr} - Loaded stored data {audioinfo}")
        if not isinstance(audioinfo, AudioInfo):
            raise InvalidProgressFile(f"{idstr} got unexpected data from"
                    f" {Config.audioinfo_fname}"
                    f"\n    Path: {audioinfo_fpath}"
                    f"\n    Dump: {audioinfo}"
                    f"\n    Contents: {audioinfo_fpath.read_text()}")

    else:
        fpath = xinfo.source_wav
        audioinfo = AudioInfo(duration_s=get_file_duration(fpath))
        try:
            dbg(f"{idstr} - Listening for timestamp info in '{fpath}' ({audioinfo.duration_s:.2f}s)")
            extract_timestamp_from_audio(fpath, audioinfo)
        except (NoSuitableAudioSpan, TimestampGrokError) as e:
            pass
        if act(f"{idstr} - dump audioinfo to {audioinfo_fpath}"):
            write_json(audioinfo_fpath, audioinfo)

    dbg(f"{idstr} - done: {audioinfo}")
    return audioinfo


def sec_to_td(sec: float) -> datetime.timedelta:
    return datetime.timedelta(seconds=sec)


# TODO test this
# Note par2 fails when ? or * is in the filename.
def derive_timestamp(worklist:list[TransferInfo], token:int,
        fallback_timestamp_mode:str, fallback_timestamp_dt:datetime.datetime,
        delta:datetime.timedelta) -> None:
    """Set the TransferInfo.timestamp for the worklist entry corresponding to token.
    """
    prev = worklist[token - 1] if token > 0 else None
    current = worklist[token]
    next = worklist[token + 1] if token < (len(worklist) - 1) else None

    assert current.timestamp == None
    assert current.audioinfo is not None

    if current.audioinfo.parsed_timestamp:
        current.timestamp = current.audioinfo.parsed_timestamp
        current.timestamp_guess_direction = '@'

    elif prev and prev.timestamp is not None:
        assert prev.audioinfo is not None
        assert prev.audioinfo.duration_s is not None
        current.timestamp = prev.timestamp \
                + sec_to_td(prev.audioinfo.duration_s) + delta
        current.timestamp_guess_direction = '+'

    elif next and next.timestamp is not None:
        assert next.audioinfo is not None
        assert current.audioinfo.duration_s is not None
        current.timestamp = next.timestamp \
                - sec_to_td(current.audioinfo.duration_s) - delta
        current.timestamp_guess_direction = '-'

    else:
        current.timestamp = get_fallback_timestamp(
                current.source_wav,
                fallback_timestamp_mode,
                fallback_timestamp_dt,
                )

        if next:
            assert fallback_timestamp_mode in 'prior ctime mtime atime timestamp+'.split()
            assert token == 0
            if fallback_timestamp_mode == 'prior':
                current.timestamp += delta
                current.timestamp_guess_direction = '+'
            elif fallback_timestamp_mode.endswith('time'):
                current.timestamp_guess_direction = '~'
            else:
                assert fallback_timestamp_mode == 'timestamp+'
                current.timestamp_guess_direction = ""

        else:
            assert fallback_timestamp_mode in 'now timestamp-'.split()
            assert token == len(worklist) - 1
            if fallback_timestamp_mode == 'now':
                current.timestamp -= delta
                current.timestamp_guess_direction = '-'
            else:
                assert fallback_timestamp_mode == 'timestamp-'
                current.timestamp_guess_direction = ""


def load_xinfo_timestamp_from_fname(xinfo: TransferInfo):
    # Parse the timestamp out from the filename
    tsinfo = extract_timestamp_from_str(str(xinfo.fname_prompted))
    if tsinfo and tsinfo.timestamp:
        # Override the guessed timestamp
        xinfo.timestamp = tsinfo.timestamp


#===========================================================================
# step coroutines for the StepNetwork
#============================================================================

class Step:
    """Namespace class for the step tasks in the taketake StepNetwork"""

    @staticmethod
    async def setup(cmdargs, worklist, stepper):
        if cmdargs.continue_from:
            progress_dir = cmdargs.continue_from
        else:
            progress_dir = cmdargs.dest / inject_timestamp(Config.progress_dir_fmt)
            if act(f"create main progress dir {progress_dir}"):
                progress_dir.mkdir()

        for token, wav in enumerate(cmdargs.wavs):
            assert isinstance(wav, Path)
            xinfo = TransferInfo(
                    token=token,
                    source_wav=wav,
                    dest_dir=cmdargs.dest,
                    wav_progress_dir=progress_dir / wav.name,
                    instrument=cmdargs.instrument,
                    target_timezone=cmdargs.target_timezone,
                )

            # TODO - test cases where the wav dir or symlink doesn't exist
            if not xinfo.wav_progress_dir.exists():
                if act(f"create progress dir for {wav.name}"):
                    xinfo.wav_progress_dir.mkdir()

            source_link_fpath = xinfo.wav_progress_dir / Config.source_wav_linkname
            if not source_link_fpath.is_symlink():
                wav_abspath = Path(os.path.abspath(xinfo.source_wav))
                if act(f"symlink {source_link_fpath} -> {wav_abspath}"):
                    source_link_fpath.symlink_to(wav_abspath)

            # TODO - pull the .fstat.json file if it exists, otherwise build
            # it and write it if act.  Fill in xinfo.fstat.

            done_processing_fpath = xinfo.wav_progress_dir / Config.done_processing_fname
            if done_processing_fpath.exists():
                prompted_fpath = xinfo.wav_progress_dir / Config.provided_fname
                xinfo.fname_prompted = Path(prompted_fpath.read_text().strip())
                load_xinfo_timestamp_from_fname(xinfo)

                stepper.log(f"{wav} already completed processing on a prior run, canceling")
                xinfo.done_processing = True

            worklist.append(xinfo)
            await stepper.put(len(worklist) - 1)
            stepper.log(f"Set up transfer for {xinfo.source_wav}")
            await asyncio.sleep(0) # Let the work begin

        await stepper.put(stepper.end)


    @staticmethod
    async def listen(cmdargs, worklist, *, stepper):
        """The speech recognizer finds the first span of non-silent audio, passes
        it through PocketSphinx, and attempts to parse a timestamp and comments
        from the results.

        Uses several workers to process multiple files in parallel.
        """
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=Config.num_listener_tasks) as executor:
            future_to_token = {}
            # Submit the listeners to the executor
            while (token := await stepper.get()) is not stepper.end:
                stepper.log(f"****** got {token} *******")
                future_to_token[executor.submit(
                    listen_to_wav, worklist[token], token)] = token

            for future in concurrent.futures.as_completed(future_to_token):
                token = future_to_token[future]
                worklist[token].audioinfo = future.result()
                await stepper.put(token)

        await stepper.put(stepper.end)


    # TODO - canceled tokens break our reordering AND our fallback_timestamp!
    @staticmethod
    async def reorder(cmdargs, worklist, *, stepper):
        """Buffer and emit tokens in a way that autoname can handle
        """
        token_cursor = 0
        seen = set() # Set of all gotten tokens that are > token_cursor
        found_first_timestamp = False

        while (token := await stepper.get()) is not stepper.end:
            seen.add(token)
            # If we now have a contiguous span from token_cursor through
            # token, emit them once we've found one with a timestamp.
            while token_cursor in seen:
                seen.remove(token_cursor) # Keep the set small
                if found_first_timestamp:
                    await stepper.put(token_cursor)
                elif worklist[token_cursor].audioinfo.parsed_timestamp is not None:
                    found_first_timestamp = True
                    # Emit contiguous tokens in reverse order
                    for i in range(token_cursor, -1, -1):
                        await stepper.put(i)

                token_cursor += 1

        assert token_cursor == len(worklist)
        if not found_first_timestamp:
            if cmdargs.fallback_timestamp_mode in 'now timestamp-'.split():
                # Emit all takens in reverse order
                for i in range(token_cursor - 1, -1, -1):
                    await stepper.put(i)
            else:
                for i in range(token_cursor):
                    await stepper.put(i)

        await stepper.put(stepper.end)


    @staticmethod
    @stepped_task
    async def prompt(cmdargs, worklist, *, token, stepper):
        """The Prompter asks the user for corrections on the guesses from autoname.
        """
        xinfo = worklist[token]
        fpath = xinfo.source_wav
        prompted_fpath = xinfo.wav_progress_dir / Config.provided_fname
        audioinfo = xinfo.audioinfo

        if prompted_fpath.exists():
            xinfo.fname_prompted = Path(prompted_fpath.read_text().strip())

        else:
            derive_timestamp(worklist=worklist, token=token,
                    fallback_timestamp_mode=cmdargs.fallback_timestamp_mode,
                    fallback_timestamp_dt=cmdargs.fallback_timestamp_dt,
                    delta=datetime.timedelta(seconds=Config.interfile_timestamp_delta_s))

            # Generate the extensionless guessed filename
            xinfo.fname_guess = format_dest_filename(xinfo)
            guess_fpath = xinfo.wav_progress_dir / Config.guess_fname
            if act(f"create {Config.guess_fname} for {fpath.name}:  {xinfo.fname_guess}"):
                guess_fpath.write_text(str(xinfo.fname_guess))

            print(f"Speechinizer: {fpath.name} - {audioinfo.recognized_speech!r}"
                  f"-> {xinfo.fname_guess!r}")
            if cmdargs.do_prompt:
                await prompt_for_filename(worklist[token])
            else:
                xinfo.fname_prompted = Path(xinfo.fname_guess)

            if not xinfo.fname_prompted.suffix == ".flac":
                xinfo.fname_prompted = Path(str(xinfo.fname_prompted) + ".flac")

            if act(f"Write prompted filename {xinfo.fname_prompted} to {prompted_fpath}"):
                prompted_fpath.write_text(str(xinfo.fname_prompted))

        load_xinfo_timestamp_from_fname(xinfo)

        # Need to handle @ - + ~ tags?  We already named the file at this point.


    @staticmethod
    @stepped_task
    async def flacenc(cmdargs, worklist, *, token, stepper):
        """Meanwhile, the flac encoder copies the wav data while encoding it
        to the destination as a temporary file.
        """

        xinfo = worklist[token]

        # If .in_progress.flac exists, rename it
        flac_progress_fpath = xinfo.wav_progress_dir / Config.flac_progress_fname
        if flac_progress_fpath.exists():
            intr_fname = inject_timestamp(Config.flac_interrupted_fname_fmt)
            intr_fpath = xinfo.wav_progress_dir / intr_fname
            if act(f"Earlier flacenc interrupted, rename "
                   f"{flac_progress_fpath} -> {intr_fpath}"):
                flac_progress_fpath.rename(intr_fpath)

        flac_encoded_fpath = xinfo.wav_progress_dir / Config.flac_encoded_fname
        if not flac_encoded_fpath.exists():
            if act(f"Flac encode {xinfo.source_wav} -> {flac_progress_fpath}"):
                await flac_encode(xinfo.source_wav, flac_progress_fpath)
            if act(f"Rename {flac_progress_fpath} -> {flac_encoded_fpath}"):
                flac_progress_fpath.rename(flac_encoded_fpath)

        if act(f"Flushing cache of {xinfo.source_wav}"):
            flush_fs_caches(xinfo.source_wav)


    @staticmethod
    @stepped_task
    async def pargen(cmdargs, worklist, *, token, stepper):
        xinfo = worklist[token]

        final_fpath = xinfo.wav_progress_dir / xinfo.fname_prompted
        flac_encoded_fpath = xinfo.wav_progress_dir / Config.flac_encoded_fname
        if not final_fpath.is_symlink():
            if act(f"Symlink {final_fpath} -> {Config.flac_encoded_fname}"):
                final_fpath.symlink_to(Config.flac_encoded_fname)
            if act(f"Update timestamp of {flac_encoded_fpath}"):
                set_mtime(flac_encoded_fpath, xinfo.timestamp)

        remaining_pars = 0
        truncated_pars = 0
        par2_pattern = f"{xinfo.fname_prompted}.vol*.par2"
        for par2 in xinfo.wav_progress_dir.glob(par2_pattern):
            if par2.stat().st_size == 0:
                if act(f"Delete 0-sized {par2}"):
                    par2.unlink()
                    truncated_pars += 1
            else:
                remaining_pars += 1
        if truncated_pars or not remaining_pars:
            # TODO convert par2_create to use Path objects
            if act(f"par2 create {final_fpath}"):
                await par2_create(str(final_fpath),
                        Config.par2_num_vol_files,
                        Config.par2_redundancy_per_vol)

        if act(f"Flushing cache of par2 files for {xinfo.fname_prompted}"):
            flush_fs_caches(*xinfo.wav_progress_dir.glob(par2_pattern))

        try:
            await par2_verify(final_fpath)
        except MissingPar2File as e:
            if act(f"Raise MissingPar2File exception: {e}"):
                # When in no-act, we expect the par2s to be missing
                raise

    @staticmethod
    @stepped_task
    async def cmp(cmdargs, worklist, *, token, stepper):
        xinfo = worklist[token]
        cmp_results_fpath = xinfo.wav_progress_dir / Config.cmp_results_fname
        wav_fpath = xinfo.source_wav
        flac_encoded_fpath = xinfo.wav_progress_dir / Config.flac_encoded_fname

        success = None
        if not cmp_results_fpath.exists():
            if act(f"Verify {wav_fpath} decoded from {flac_encoded_fpath})"):
                success = await cmp_flac_vs_wav(
                        flac_fpath=flac_encoded_fpath,
                        wav_fpath=wav_fpath,
                        cmp_results_fpath=cmp_results_fpath)
                if not success:
                    check_cmp_results_file(cmp_results_fpath, wav_fpath)
                    #raise CmpMismatch(
                    #        f"cmp failed, but check_cmp_results_file did not detect it!"
                    #        f"\n  {cmp_results_fpath}"
                    #        f"\n  {xinfo.source_wav}")

        if cmp_results_fpath.exists():
            # When resuming, make sure .cmp_results represents a successful check
            check_cmp_results_file(cmp_results_fpath, wav_fpath)


    @staticmethod
    async def cleanup(cmdargs, worklist, *, stepper):
        await stepper.sync_end()
        stepper.log(f"Cleaning up ...............................................")

        # Check for failures, stop if any are found
        failed_xinfos = [x for x in worklist if x.failures]
        if failed_xinfos:
            print(f"Skipping cleanup due to {len(failed_xinfos)} failed transfers:")
            for xinfo in failed_xinfos:
                print(f"  * {xinfo.token}. {xinfo.source_wav}: {len(xinfo.failures)} fail")
                for f in xinfo.failures:
                    print(f"      -> {str(f).splitlines()[0]}")
            return

        if cmdargs.skip_cleanup:
            print(f"Skipping cleanup of {cmdargs.continue_from} due to --skip-cleanup")
            return

        for xinfo in worklist:
            # a. Touch .done_processing
            if not xinfo.done_processing:
                done_processing_fpath = xinfo.wav_progress_dir / Config.done_processing_fname
                if act(f"touch {done_processing_fpath}"):
                    done_processing_fpath.touch()

            flac_encoded_fpath = xinfo.wav_progress_dir / Config.flac_encoded_fname
            dest_flac_fpath = xinfo.dest_dir / xinfo.fname_prompted
            if flac_encoded_fpath.exists() and dest_flac_fpath.exists():
                raise FileExists(f"Both {flac_encoded_fpath} and {dest_flac_fpath} exist!")

            # b. delete source wav - only after checking if we can copy the flac
            stepper.log(f"{xinfo.token}")
            if xinfo.source_wav.exists():
                if act(f"Deleting {xinfo.source_wav}"):
                    xinfo.source_wav.unlink()
            else:
                stepper.log(f"Source {xinfo.source_wav} alread deleted")

            src_flacs_dirpath = xinfo.source_wav.parent / Config.src_flacs_dirname
            if act(f"mkdir {src_flacs_dirpath}"):
                src_flacs_dirpath.mkdir(exist_ok=True)

            for f in xinfo.wav_progress_dir.glob(f"{xinfo.fname_prompted}*"):
                # c. copy back
                if act(f"cp -a {f} {src_flacs_dirpath}"):
                    shutil.copy2(f, src_flacs_dirpath)

                # d. decache
                copied_fpath = src_flacs_dirpath / f.name
                if act(f"Flushing cache of {copied_fpath}"):
                    flush_fs_caches(copied_fpath)

            # e. par2 verify
            if act(f"par2 check {src_flacs_dirpath / xinfo.fname_prompted}"):
                await par2_verify(src_flacs_dirpath / xinfo.fname_prompted)

            # f. move flac and .par2s to final location
            dest_par2_dirpath = xinfo.dest_dir / Config.dest_par2_dirname
            if act(f"mkdir {dest_par2_dirpath}"):
                dest_par2_dirpath.mkdir(exist_ok=True)

            if not dest_flac_fpath.exists():
                prompted_flac_fpath = xinfo.wav_progress_dir / xinfo.fname_prompted
                if act(f"deleting soon-to-be-broken symlink {prompted_flac_fpath}"):
                    prompted_flac_fpath.unlink()

                if act(f"mv {flac_encoded_fpath} {dest_flac_fpath}"):
                    flac_encoded_fpath.rename(dest_flac_fpath)
                    # TODO assert the mtime matches the xinfo.timestamp

                # g. Log the flac to transfer.log in src and dest
                now = datetime.datetime.now()
                ts = now.strftime(Config.timestamp_fmt_long)
                wav_abspath = Path(os.path.abspath(xinfo.source_wav))
                dest_abspath = Path(os.path.abspath(dest_flac_fpath))
                msg = f"{ts} : {wav_abspath} -> {dest_abspath}\n"
                for dirpath in xinfo.source_wav.parent, xinfo.dest_dir:
                    log_fpath = dirpath / Config.transfer_log_fname
                    if act(f"append '{msg}' to {log_fpath}"):
                        with open(log_fpath, "a") as f:
                            f.write(msg)

            # Move over the par2 files
            par2_flac_fpath = dest_par2_dirpath / xinfo.fname_prompted # dest/.par2
            flac_symlink_target = Path("..") / xinfo.fname_prompted
            if par2_flac_fpath.exists():
                stepper.log(f"par2 flac symlink {par2_flac_fpath} already created")
            elif act(f"symlink par2 flac {par2_flac_fpath} -> {flac_symlink_target}"):
                par2_flac_fpath.symlink_to(flac_symlink_target)

            par2_pattern = f"{xinfo.fname_prompted}.vol*.par2"
            for par2 in xinfo.wav_progress_dir.glob(par2_pattern):
                dest_par2_fpath = dest_par2_dirpath / par2.name
                if dest_par2_fpath.exists():
                    raise FileExists(f"Both {par2} and {dest_par2_fpath} exist!")
                elif act(f"mv {par2} {dest_par2_fpath}"):
                    par2.rename(dest_par2_fpath)


        for xinfo in worklist:

            for f in (
                    Config.done_processing_fname,
                    Config.source_wav_linkname,
                    Config.audioinfo_fname,
                    Config.guess_fname,
                    Config.provided_fname,
                    Config.cmp_results_fname,
                    ):
                fpath = xinfo.wav_progress_dir / f
                if act(f"deleting {fpath}"):
                    fpath.unlink()

            intr_glob = Config.flac_interrupted_fname_fmt.format('*')
            for fpath in xinfo.wav_progress_dir.glob(intr_glob):
                if act(f"deleting {fpath}"):
                    fpath.unlink()

            if act(f"rmdir {xinfo.wav_progress_dir}"):
                xinfo.wav_progress_dir.rmdir()

            if act(f"attempting rmdir {xinfo.wav_progress_dir.parent}"):
                try:
                    xinfo.wav_progress_dir.parent.rmdir()
                except OSError:
                    pass

        instrument_fpath = worklist[0].source_wav.parent / Config.instrument_fname
        instrument = worklist[0].instrument
        if act(f"writing '{instrument}' to {instrument_fpath}"):
            instrument_fpath.write_text(instrument)


#============================================================================
# StepNetwork construction
#============================================================================

async def run_tasks(args):
    """Connect up the various tasks with queues and run them."""
    worklist = []

    def is_canceled(token: int) -> bool:
        xinfo = worklist[token]
        return len(xinfo.failures) > 0 or xinfo.done_processing

    def cancel(token: int, e: TaketakeRuntimeError, stepper: Stepper):
        worklist[token].failures.append(e)
        stepper.log(f"Canceling item due to {e}")


    network = StepNetwork("wavflacer",
            cancellation_exception_type=TaketakeRuntimeError,
            cancel_check_fn=is_canceled,
            cancel_token_fn=cancel,
            )
    network.update_common_kwargs(cmdargs=args, worklist=worklist)

    network.add(Step.setup,
            send_to=[Step.listen, Step.flacenc])

    network.add_pipeline(

            Step.listen,
            Step.reorder,
            Step.prompt,

            pull_from=Step.setup,
            send_to=Step.pargen)

    network.add(Step.flacenc,
            pull_from=Step.setup,
            send_to=[Step.pargen, Step.cmp],
            sync_to=Step.cmp)

    network.add(Step.pargen,
            pull_from=[Step.prompt, Step.flacenc],
            sync_to=Step.cleanup)

    network.add(Step.cmp,
            sync_from=Step.flacenc,
            pull_from=Step.flacenc,
            sync_to=Step.cleanup)

    network.add(Step.cleanup,
            sync_from=[Step.cmp, Step.pargen])

    await network.execute()


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
        now = datetime.datetime.now()
        print(f"{now.strftime(Config.timestamp_fmt_us)} -",
              *args, f"({sys._getframe(1+depth).f_code.co_name})", **kwargs)


#============================================================================
# Command line argument processing
#============================================================================

def format_args(args):
    arglist=[]
    for arg, val in vars(args).items():
        if val is not False and val is not None:
            if val is True:
                arglist.append(str(arg))
            elif not isinstance(val, str) and isinstance(val, Sequence):
                arglist.append(f"{arg}=[{', '.join(str(e) for e in val)}]")
            else:
                arglist.append(f"{arg}={val}")
    return " ".join(arglist)


def validate_args(parser: argparse.ArgumentParser, args) -> list[str]:
    """Validates arguments processed by the parser and set in parser.args.

    If --target wasn't specified, removes the last item from args.sources
    and sets it as the dest arg.

    If args.continue_from isn't set, sets it to the progress dir in dest if it
    exists.

    Builds args.wavs from the remaining args.sources and the args.continue_from.

    Sets args.dest and check for consistency, including dir existance.
    Returns a list of errors encountered during parsing.
    """

    errors = []
    def err(*args):
        errors.append(" ".join(str(a) for a in args))

    args
    # debug must be set prior to the first call to dbg()
    if args.debug:
        Config.debug = True

    if args.no_act:
        Config.act = False

    if args.prefix:
        Config.prefix = args.prefix

    dbg("args pre-val: ", format_args(args))

    # Use the final positional parameter as dest, like mv does
    if args.sources and args.dest is None:
        args.dest = args.sources.pop()

    # Process the requested timezone
    try:
        args.target_timezone = parse_timezone(args.requested_timezone)
    except zoneinfo.ZoneInfoNotFoundError as e:
        err(f"Invalid --target-timezone '{args.requested_timezone}': {e}")

    # Check and fix --fallback-timestamp
    args.fallback_timestamp_mode = args.fallback_timestamp
    args.fallback_timestamp_dt = None
    if args.fallback_timestamp[-1] in "+-" \
            and (tsinfo := extract_timestamp_from_str(args.fallback_timestamp)):

        if not tsinfo.weekday_correct:
            err(f"Mismatched weekday in --fallback-timestamp: "
                f"{args.fallback_timestamp}, expected {tsinfo.timestamp.strftime('%a')}")

        else:
            args.fallback_timestamp_mode = "timestamp" + args.fallback_timestamp[-1]
            args.fallback_timestamp_dt = tsinfo.timestamp

    elif args.fallback_timestamp not in 'prior mtime ctime atime now'.split():
        err(f"Invalid --fallback-timestamp: '{args.fallback_timestamp}'"
            f"\n      Expected one of 'prior', 'mtime', 'ctime', 'atime', or 'now'"
            f"\n      or a timestamp like {inject_timestamp('{}')}"
            f"\n      with form YYYYmmdd-HHMMSS+ or - "
            f"(seconds are optional, separator can be -, _, or a space)")

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

    # Now that we have a source directory, check if transfer.log exists if the
    # user requested the 'prior' --fallback-timestamp mode.
    if args.fallback_timestamp_mode == 'prior' and args.wavs:
        transfer_log_fpath = args.wavs[0].parent / Config.transfer_log_fname
        if not transfer_log_fpath.is_file():
            err(f"--fallback_timestamp 'prior' given, "
                f"but '{transfer_log_fpath}' does not exist")

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
        tempwavdir = None
        if args.continue_from:
            tempwavdir = args.continue_from / wav.name

        if args.continue_from and tempwavdir.exists():
            assert tempwavdir is not None
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
        for wav in sorted(args.continue_from.glob("*")):
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

    # Check for instrument file in the first src directory
    if args.wavs:
        instrument_fpath = args.wavs[0].parent / Config.instrument_fname
        if instrument_fpath.exists():
            read_instrument = instrument_fpath.read_text().strip()
            dbg(f"read {read_instrument} from {instrument_fpath}")
            if args.instrument is not None and read_instrument != args.instrument:
                err(f"Specified --instrument '{args.instrument}' doesn't match "
                    f"contents of '{instrument_fpath}': '{read_instrument}'")
            else:
                args.instrument = read_instrument
        if not args.instrument:
            err(f"No '{Config.instrument_fname}' file found in SOURCE_WAV "
                f"directory '{args.wavs[0].parent}'."
                f"\n      You must specify an instrument with -i or --instrument")

    dbg("args post-val:", format_args(args))

    return errors


def process_args(argv: Optional[list[str]]=None) \
        -> tuple[argparse.ArgumentParser, argparse.Namespace, list[str]]:

    parser = argparse.ArgumentParser(
            description=__doc__,
            formatter_class=argparse.RawTextHelpFormatter) #RawDescriptionHelpFormatter)
    arg = parser.add_argument

    arg('-n', '--no-act', action='store_true',
        help="Do everything but modify filesystems or prompt the user")

    arg('-d', '--debug', action='store_true',
        help="Show debug output, including tracebacks from exceptions")

    arg('-P', '--no-prompt', action='store_false', dest='do_prompt',
        help=f"Don't prompt for filename corrections")

    arg('-p', '--prefix', action='store',
        help=f"Prefix flac files with the given string. Default: {Config.prefix}")

    arg('-i', '--instrument', action='store',
        help=f"Inject the given instrument name into the resulting filenames")

    arg('-z', '--tz', '--target-timezone', dest='requested_timezone',
        metavar='TIMEZONE', action='store',
        help="""Use the given timezone to render timestamps in generated filenames.
The timestamp extracted from each input WAV file will be shown in the given timezone.
Possible timezones are:

    * local (default) - The local timezone is used
    * [-+]NNNN - A simple offset timezone
    * Z, zulu - Synonyms for UTC
    * IANA abbreviation, like PST or GMT - these are locale specific
    * IANA full name
    * list - print a list of timezone names
    """)

    arg('-f', '--fallback-timestamp',
        metavar='TIMESTAMP_MODE',
        action='store', default="mtime",
        help=f"""If speech-to-text fails, use the indicated timestamp instead.

Valid choices are:

    "now" - use current time for the last file minus its duration, and
    back calculate the times from there based on there durations;

    {inject_timestamp("{}-")} - specify the timestamp of the last file,
    with prior files calculated based on durations;

    {inject_timestamp("{}+")} - specify the timestamp of the first file,
    with succeeding files calculated based on durations;

    "prior" - use the last mtime from src/transfer.log as timestamp of
    the first file, with succeeding files calculated based on durations;

    "mtime", "ctime", "atime" - use the modified/creation/access time of
    the first file, with succeeding files calculated based on durations.
    """)

    arg('-S', '--skip-speech-to-text', action='store_true',
        help=f"Use the given --fallback-timestamp instead.")

    #arg('-k', '--keep-wavs', action='store_true',
        #help="Don't delete processed source wav files")

    #arg('--skip-copyback', action='store_true',
        #help="Don't copy the encoded flacs back to their source wav dir")

    arg('--skip-cleanup', action='store_true',
        help="""Don't perform any cleanup actions.

 * Don't remove the original src wavfile
 * Don't copy the encoded flac or par2 files back to the src/flacs directory
 * Leave the encoded flac and par2 files in the per-wav dest temp directories
 * Don't remove the temp directories
 * Don't write src/instrmnt.txt
 * Don't update src,dest/transfer.log
    """)

    arg('--skip-tests', action='store_true',
        help="""Do not run unit tests prior to starting the transfer.

This saves a few seconds at startup, but you risk integrity
issues if some system change causes differences that wouldn't
otherwise be detectable during normal running.
    """)

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
positional arguments will be treated as SOURCE arguments.
    """)

    arg('sources', metavar='SOURCE_WAV', nargs='*', type=Path,
        help="""Transfer the specified SOURCE_WAV files.  If there is only
a single SOURCE_WAV specified and it is a directory, then
transfer all wav files found in that directory.
    """)

    # This is left empty because sources is greedy.  process_args() fills it.
    arg('_dest', metavar='DEST_PATH', nargs='?', type=Path,
        help=f"""Destination directory for encoded flac and par2 files.

This directory will also contain the timestamped
{Config.progress_dir_fmt.format('*')} directory for tracking progress.
    """)

    args = parser.parse_args(argv)
    errors = validate_args(parser, args)
    return parser, args, errors

def format_errors(errors):
    return "".join("\n  * {}".format(e) for e in errors)

def main():
    parser, args, errors = process_args()

    if args.requested_timezone == "list":
        list_timezones()
        if not errors:
            return 0
        # Otherwise, fall through to the error reporting

    # Report errors
    if errors:
        parser.error("Invalid command line options:"
                + format_errors(errors))

    args
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
