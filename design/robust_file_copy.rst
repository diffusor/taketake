==========================
USB drive transfer process
==========================

Design goals:
-------------
* Don't modify the USB contents until data has been copied off and verified
* Do post-copy verification after flushing filesystem caches

Flow:
-----
The **setup** actor processes the command line options and figures out which
wav files to process or continue processing based in what progress directories
exist.  It builds an array of FileInfo objects which will be indexed by all
actors to process the files.

The actors get and send indexes into their input and output queues.  These
index into the array of FileInfo objects to coordinate processing.

Under each step header, a => flow line describes the inputs and outputs to the
current ``[actor]``, which is surrounded by square brackets.  Before
processing an index, the actor waits for all the inbound queues to have that
index ready for getting.  After processing the index, the actor puts that
index into all its outbound queues.  A terminal token is used to indicate that
there are no more items to process.

1. **setup**: *[global]* Determine wavs to process

   ``[setup] => listen,flacenc``

   a. Verify unit tests pass

   b. Check for progress directory and resume

   c. Otherwise, create src wav progress directories::

       mkdir .taketake.20211025-1802-Mon
       echo srcdir > .taketake.20211025-1802-Mon/.src
       mkdir .taketake.20211025-1802-Mon/audio001.wav ...

*Perform the following steps for each wav, assuming each non-src filename is
relative to the wav's* ``.taketake.$datestamp/$wavfilename`` *progress directory*

2. **listen**: Speech to text

   ``setup => [listen] => prompt``

   a. Skip steps b and c if ``.filename_guess`` exists,
      filling in the guess into the TransferInfo

   b. Run speech to text, parse timestamp, construct filename guess

   c. Create filename guess progress file::

       echo $filename_guess > .filename_guess

3. **prompt**: Prompt for name

   ``listen => [prompt] => pargen``

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

4. **flacenc**: Flac encode

   ``setup => [flacenc] => pargen``

   a. If ``.in_progress.flac`` exists, remove it

   b. If ``.encoded.flac`` exists, skip steps c and d

   c. Flac encode src wav into dest flac::

       encode src/audio001.wav => .in_progress.flac

   d. Rename encoded flac::

       rename .in_progress.flac -> .encoded.flac

   e. Decache the src wav, even if the flac already exists::

       fadvise DONTNEED src/audio001.wav

5. **pargen**: Rename and par2 dest flac file

   ``prompt,flacenc => [pargen] => cleanup``

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

6. **xdelta**: Xdelta check wavs

   ``All(flacenc) => [xdelta] => cleanup``

   a. If src wav no longer exists or if ``.xdelta`` exists, skip step b

   b. Verify ``fincore src/.wav`` is 0 and diff the src and decoded wav files::

       flac -c -d .encoded.flac | xdelta3 -s src/.wav > .xdelta

   c. Check ``.xdelta`` for actual diffs

7. **cleanup**: Delete src wav and copy back flac

   ``All(xdelta),pargen => [cleanup] => finish``

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

8. **finish**: *[global]* Wait for all processing to complete

   ``All(cleanup) => [finish]``

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
