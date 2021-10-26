==========================
USB drive transfer process
==========================

Design goals:
-------------
* Don't modify the USB contents until data has been copied off and verified
* Do post-copy verification after flushing filesystem caches

Flow:
-----

1. *[global]* Determine wavs to process => 2, 4

   a. Verify unit tests pass

   b. Check for progress directory and resume

   c. Otherwise, create src wav progress directories::

       mkdir .taketake.20211025-1802-Mon
       echo srcdir > .taketake.20211025-1802-Mon/.src
       mkdir .taketake.20211025-1802-Mon/audio001.wav ...

   d. Fill input queues 1->2 and 1->4 with src wav progress dirs

*Perform the following steps for each wav, assuming each non-src filename is
relative to the wav's* ``.taketake.$datestamp/$wavfilename`` *progress directory*

2. Speech to text <= 1 => 3

   a. Skip if ``.filename_guess`` exists, pushing its
      contents into the outbound queue to step 3

   b. Run speech to text, parse timestamp, construct filename guess

   c. Create filename guess progress file::

       echo $filename_guess > .filename_guess

   d. Push progress dir and filename_guess into queue 2->3

3. Prompt for name <= 2 => 5

   a. Suggest contents of ``.filename_provided`` if it exists,
      otherwise use the given filename_guess

   b. Check the resulting timestamp:

      * Parse out the timestamp from the ``$filename_provided``
      * Verify that the weekday matches that from the timestamp
      * Verify the timestamp is within a reasonable delta from the speech-recognized time
      * Verify the timestamp isn't in the future
      * If the verification fails, prompt the user to confirm or redo the
        filename

   b. Create provided filename file::

       echo $filename_provided > .filename_provided

   c. Push progress dir and filename_provided into queue 3->5

4. Flac encode <= 1 => 5, 6

   a. If ``.in_progress.flac`` exists, remove it

   b. If ``.encoded.flac`` exists, skip steps c and d

   c. Flac encode src wav into dest flac::

       encode src/audio001.wav => .in_progress.flac

   d. Rename encoded flac::

       rename .in_progress.flac -> .encoded.flac

   e. Decache the src wav, even if the flac already exists::

       fadvise DONTNEED src/audio001.wav

   f. Push progress dir into queue 4->5

5. Rename and par2 dest flac file <= 3, 4 => 7

   a. If ``$filename_provided.flac`` exists, skip step b

   b. Symlink from the final filename to the ``.encoded.flac``::

       symlink $filename_provided.flac -> .encoded.flac

   c. If ``$filename_provided.flac.vol*.par2`` exists:

       * if any of their sizes are 0, delete them::

           delete $filename_provided.flac.*par2

       * otherwise, skip step d

   d. Create dest flac pars **(if interrupted, 0-sized files will be left)**::

       par2 create $filename_provided.flac

   e. Decache the dest flac and par2s::

       fadvise DONTNEED .encoded.flac *.par2

   f. Verify ``fincore .encoded.flac`` is 0

   g. Verify dest flac par2s::

       par2 verify $filename_provided.flac

   h. Push progress dir into queue 5->7

6. Xdelta wavs <= All(4) => 7

   a. If src wav no longer exists or if ``.xdelta`` exists, skip step b

   b. Verify ``fincore src/.wav`` is 0 and diff the src and decoded wav files::

       flac -c -d .encoded.flac | xdelta3 -s src/.wav > .xdelta

   c. Check ``.xdelta`` for actual diffs

   d. Push progress dir into queue 6->7

7. Delete src wav and copy back flac <= 5, All(6) => 8

   **Status of ``.taketake.$datestamp/$wavfilename``**::

        .filename_guess
        .filename_provided
        .encoded.flac [was .in_progress.flac]
        $filename_provided.flac -> .encoded.flac
        $filename_provided.flac.vol0000+500.par2
        $filename_provided.flac.vol0500+499.par2
        .xdelta

   **Skip to step g if src modification is disabled**

   a. Remove the source wav file::

       delete src/audio001.wav

   b. Copy flac file and par2s back to src if they each don't already exist
      (use .in_progress copies)::

       mkdir src/flacs
       copy .encoded.flac src/flacs/$filename_provided.flac
       update_mtime src/flacs/$filename_provided.flac
       copy
           $filename_provided.flac.vol0000+500.par2
           $filename_provided.flac.vol0500+499.par2
        -> src/flacs

   c. Decache the copied dest files

   d. par2 verified the copied dest files

   e. Move the final flac and par2 files into the dest directory::

       move .encoded.flac dest/$filename_provided.flac
       update_mtime src/flacs/$filename_provided.flac
       move $filename_provided.flac.*par2 dest/

   f. Remove the temporary dest directory::

       rm -r .taketake.$datestamp/$wavfilename

   g. Push progress dir into queue 7->8

8. *[global]* Finish <= All(8)

    a. Remove top-level progress dir ``.taketake.$datestamp``


Xdelta3 usage
-------------

Running xdelta with the stdout from flac decode
:::::::::::::::::::::::::::::::::::::::::::::::

From
https://docs.python.org/3.10/library/subprocess.html#replacing-shell-pipeline ::

    p1 = Popen(["dmesg"], stdout=PIPE)
    p2 = Popen(["grep", "hda"], stdin=p1.stdout, stdout=PIPE)
    p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
    output = p2.communicate()[0]

Verifying two files are identical
:::::::::::::::::::::::::::::::::

When the files are identical, the VCDIFF data section length is 0,
and the only instruction is a copy of the entire file::

    $ xdelta3 printdelta robust_file_copy.rst.xdelta2    
    VCDIFF version:               0
    VCDIFF header size:           50
    VCDIFF header indicator:      VCD_APPHEADER 
    VCDIFF secondary compressor:  none
    VCDIFF application header:    robust_file_copy.rst//robust_file_copy.rst~/
    XDELTA filename (output):     robust_file_copy.rst
    XDELTA filename (source):     robust_file_copy.rst~
    VCDIFF window number:         0
    VCDIFF window indicator:      VCD_SOURCE VCD_ADLER32 
    VCDIFF adler32 checksum:      7BE74121
    VCDIFF copy window length:    22670
    VCDIFF copy window offset:    0
    VCDIFF delta encoding length: 16
    VCDIFF target window length:  22670
    VCDIFF data section length:   0
    VCDIFF inst section length:   4
    VCDIFF addr section length:   1
      Offset Code Type1 Size1 @Addr1 + Type2 Size2 @Addr2
      000000 019  CPY_0 22670 @0     

**Note** - The relevant lengths and copy sizes match the filesize.  All the
following properties should be verified:

* ``VCDIFF data section length:   0``
* ``VCDIFF copy window offset:    0``
* ``VCDIFF copy window length:    22670``
* ``VCDIFF target window length:  22670``
* ``000000 019  CPY_0 22670 @0``
