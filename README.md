# taketake
## Manage takes of musical performances

Taketake coordinates between to points in the lifetime of a recording:
* Creation: [talkytime](https://diffusor.github.io/taketake/talkytime.html) facilitates audio tagging of your take
* Download: [taketake.py](src/taketake.py) handles taking your take from your USB drive

Many digital instruments don't maintain wall-clock (real-world) time.  On
these instruments, every take you record results in a file on your USB drive
that is timestamped with some meaningless date and time.

For example, some Casio digital pianos set the creation and last modified
times of all `.wav` files they create to the same exact point in time.  Roland
pianos seem to reset their clocks on power-on to a baked-in date and time, and
then run their clock like normal from there.  Relative times between takes in
the same session are correct, but there is no correlation across power cycles.

# [talkytime](https://diffusor.github.io/taketake/talkytime.html)

Talkytime provides a text-to-speech timestamp in a webpage.  When you press the
"Speak" button, the web browser speaks the time in a format that
[taketake.py](src/taketake.py) understands.

## Usage

1. Insert a USB drive into your digital instrument
2. Connect the audio output of your web-browsing device to your instrument
   (either via cable, or, for non-iPhone/iPad, via Bluetooth)
3. Configure your instrument to record to "audio"/"WAV" format
4. Initiate recording
5. Click the "Speak" button on [talkytime.html](https://diffusor.github.io/taketake/talkytime.html)
6. Wait a second or two after the speech completes
7. Start playing and record your take
8. When you want to use your takes, run [`taketake.py`](src/taketake.py) to
   download them as losslessly-compressed [FLAC](https://xiph.org/flac/) files

Note that iPad/iPhone won't send speech synthesis to digital pianos over
Bluetooth, even if you configure the Bluetooth setting as headphones instead of
speakers.  You'll have to wire it up physically instead over an
USB-C-to-analog-audio or Lightning adapter instead.  (Maybe this is because
Apple considers speech synthesis an accessibility feature that you'd never want
played through an instruments speakers?)

# [taketake.py](src/taketake.py)

Use `taketake.py` to conveniently but robustly transfer your recordings from your
USB drive to your computer.  For each take, `taketake.py` does the following:

1. Performs speech-to-text recognition on the first audible recording in your
   audio file, parsing out the timestamp from talkytime
2. Prompts you for corrections and extra notes you want in the file name
3. Constructs the final file name
4. [FLAC](https://xiph.org/flac/)-encodes the source `.wav` file
5. Flushes the wav data from your filesystem cache and then verifies a second
   read of the data results in the same bytes
6. Generates a pair of [`.par2`](https://en.wikipedia.org/wiki/PAR2) recovery volumes
7. Deletes the source `.wav` file from your USB drive and replaces it with
   the compressed `.flac` file and its accompanying `.par2` files
