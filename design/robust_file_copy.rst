==========================
USB drive transfer process
==========================

Design goals:
-------------
* Don't modify the USB contents until data has been copied off and verified
* Do post-copy verification after flushing filesystem caches

Steps:
------

Step A - encode flacs, generate pars
::::::::::::::::::::::::::::::::::::

A. For all wav files to copy:
   1. make symlink from dest dir to wav file on USB drive
      (the symlink's presence indicates processing is in progress and final
      verif is still needed)::

        link audio001.wav -> src/audio001.wav

   2. extract timestamp and duration from wav file (speech_to_text, words_to_timestamp)
   3. determine dest_filename (including instrument, timestamp, notes,
      duration, and original filename w/o .wav)

      -> query user to fix up each file name as they are generated

   While waiting for filename confirmation, for each wav:

   4. convert to flac as .orig_filename.wav.flac.in_progress (via wav symlink)::

        encode audio001.wav -> .audio001.wav.flac.in_progress

   5. rename to .orig_filename.wav.flac.done::

        rename .audio001.wav.flac.in_progress -> .audio001.wav.flac.done

   6. generate 2% par2 file of wav file next to symlink in dest dir::

        par2 create audio001.wav
         -> audio001.wav.par2
         -> audio001.wav.vol000+64.par2
        rm audio001.wav.par2

   As each wav file is produced from the above:

   7. make symlink from orig_filename.wav.flac to the final destination flac name
      (this will start as broken; its presence means the flac file hasn't been
      verified yet)::

        link audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac

   8. Also symlink from final dest name .flac.wav -> orig_filename.wav::

        link inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav

   9. rename .orig_filename.wav.flac.done to dest_filename.flac
      (this also indicates the copy is complete)::

        rename .audio001.wav.flac.done -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac

   10. touch file mtime (set last modified time to timestamp)
   11. generate 2x 2% par2 files of flac file::

        par2 create inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
         -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.par2
         -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
         -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
        rm inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.par2


Step B - flush filesystem, sync
:::::::::::::::::::::::::::::::

B. Once: flush FS caches


Step C - Verify par files
:::::::::::::::::::::::::

C. For all copied wav files:
   1. Verify flac file against both of its par2 files::

        par2 verify inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
        par2 verify inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2

   2. Verify wav par2 against wav symlink (testing copy on USB drive)::

        par2 verify audio001.wav.vol000+64.par2

   3. Rename wav symlink to wav.orig::

        rename audio001.wav -> audio001.wav.orig
        (symlink is now: audio001.wav.orig -> src/audio001.wav)

   4. unpack flac based on .flac.wav symlink::

        flac decode inst.20210101-1234-Mon.1h2s.Twitch.audio001.wav -> audio001.wav

   5. verify unpacked flac wav vs wav.par2::

        par2 verify audio001.wav.vol000+64.par2

   6. retarget .flac.wav to point to .wav.orig symlink, instead of just .wav::

        link -f inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav.orig

   7. add (broken) symlink to USB .flac copy::

        link inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac

   8. remove temporary .wav file (that was decoded from the .flac file for verification)::

        rm audio001.wav


Step D - Clean src, copy flac back to USB
:::::::::::::::::::::::::::::::::::::::::

D. For all copied wav files:
   1. Remove src wav, wav.par2, and symlinks wav.orig, wav.flac, and .flac.wav::

        rm src/audio001.wav
        rm audio001.wav.vol000+64.par2
        rm audio001.wav.orig (symlink to src/audio001.wav)
        rm audio001.wav.flac
        rm inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav (symlink to audio001.wav.orig)

   2. Copy flac and its par2 files to the USB drive (in a subdir)::

        mkdir src/flacs
        copy
            inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
            inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
            inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
         -> src/flacs


Step E - flush filesystem, sync
:::::::::::::::::::::::::::::::

E. Once: flush FS caches


Step F - Verify USB copy of FLAC files, clean up
::::::::::::::::::::::::::::::::::::::::::::::::

F. For all copied wav files:
   1. On USB: Verify all copied flac files against both of their par2 files::

        in src/flacs
        par2 verify inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
        par2 verify inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2

   2. delete flac.copy symlink from dest::

        rm inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy


================================
USB drive file transfer example:
================================

Start state:
------------
::

    src/
    audio001.wav
    audio002.wav

    dest/


Step A - first file:
::::::::::::::::::::

A1 - wav symlink::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav -> src/audio001.wav

A4 - copy+convert to flac::

    src/
    audio001.wav
    audio002.wav

    dest/
    .audio001.wav.flac.in_progress
    audio001.wav -> src/audio001.wav

A5 - Rename to .orig.wav.flac.done::

    src/
    audio001.wav
    audio002.wav

    dest/
    .audio001.wav.flac.done
    audio001.wav -> src/audio001.wav

A6 - generate par2 files for original .wav::

    src/
    audio001.wav
    audio002.wav

    dest/
    .audio001.wav.flac.done
    audio001.wav -> src/audio001.wav
    audio001.wav.vol000+64.par2

A7,8 - After user prompt, symlink dest_filename (both ways)::

    src/
    audio001.wav
    audio002.wav

    dest/
    .audio001.wav.flac.done
    audio001.wav -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav

A9 - rename flac to dest filename::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav

A10 - timestamp update (set mtime)

A11 - generate flac par2s::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav

Step A - All files:
:::::::::::::::::::
::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    audio002.wav -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav


Step C - first file:
::::::::::::::::::::

C1 - verify flac against both its par2s
C2 - verify orig wav vs par2
C3 - then rename wav symlink to .orig::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav.orig -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    audio002.wav -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav

C4 - unpack flac::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav  # decompressed from inst.20210101-1234-Mon.1h2s.Twitch.audio001.wav
    audio001.wav.orig -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    audio002.wav -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav

C5 - verify unpacked flac wav vs wav.par2
C6 - retarget .flac.wav to point to wav.orig symlink
C7 - add (broken) symlink to USB .flac copy::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav  # decompressed from inst.20210101-1234-Mon.1h2s.Twitch.audio001.wav
    audio001.wav.orig -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    audio002.wav -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav.orig
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav

C8 - remove verified decoded audio001.wav::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav.orig -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    audio002.wav -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav.orig
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav

Step C - all files:
:::::::::::::::::::
::

    src/
    audio001.wav
    audio002.wav

    dest/
    audio001.wav.orig -> src/audio001.wav
    audio001.wav.vol000+64.par2
    audio001.wav.flac -> inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    audio002.wav.orig -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.wav -> audio001.wav.orig
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav.orig
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.copy -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac


Step D - first file:
::::::::::::::::::::

D1 - Remove src wav, wav.par2, and symlinks wav.orig, wav.flac, and .flac.wav::

    src/
    audio002.wav

    dest/
    audio002.wav.orig -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav.orig
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.copy -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac

D2 - copy flac and par2s::

    src/
    audio002.wav

    src/flacs
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2

    dest/
    audio002.wav.orig -> src/audio002.wav
    audio002.wav.vol000+93.par2
    audio002.wav.flac -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.wav -> audio002.wav.orig
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.copy -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac

Step D - all files:
:::::::::::::::::::
::

    src/

    src/flacs
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2

    dest/
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.copy -> src/flacs/inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.copy -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac


Step F - one file:
::::::::::::::::::

F1 - verify flacs on USB
F2 - delete symlinks::

    src/

    src/flacs
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2

    dest/
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.copy -> inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac

Step F - all files:
:::::::::::::::::::
::

    src/

    src/flacs
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2

    dest/
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0000+500.par2
    inst.20210101-1234-Mon.1h2s.Twitch.audio001.flac.vol0500+499.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol000+28.par2
    inst.20210102-1234-Mon.5m8s.Jupiter-60bpm.audio002.flac.vol028+27.par2
