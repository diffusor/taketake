===========================
Holistic filename prompting
===========================

Design goals:
-------------

* Provide the user with a view of all files and info involved in the transfer
* Stream in filename guesses as they arrive
* Allow committing filenames one-by-one, or all at once


View:
-----

The list of files will be 1 file per line.  As the user selects different
files, the resulting final filename is displayed in the status bar.

mockup::

 ************************************* log window *****************************************************************************************
 log line
 log line
 log line
 log line
 ************************************* log window *****************************************************************************************
 4 files, 2412.7 MiB, 2021-11-10 08:44:33 Wed
 src:  /media/diffusor/USB_piano1
 dest: /home/diffusor/piano_recordings
 2021 Nov 6 v      .       7       .        v        .       8        .        v       .       9        .       v        .       10
         01                                                                                                                2
  FXC # Original Name Size M  Runtime  File mtime/(ctime)   Guessed Timestamp          Edited Timestamp         Notes
 ------------------------------------------------------------------------------------------------------------------------------------------
 *Vx  0 audio004.wav    41.3  0:04:13  2014-01-01 00:01:23  2021-11-06 10:44:00 Sat    2021-11-06 10:44:00 Sat  Bach Minuet, 93 bpm, issues
 >f   1 audio005.wav   298.9  0:29:48  2014-01-01 00:45:42  2021-11-06 10:48:18 Sat+?  2021-11-06 11:13:00 Sat  Improv in G-, meditative
      2 audio006.wav   134.2  0:13:01  2014-01-01 00:00:54  2021-11-07 00:00:54 Sun+?  2021-11-09 20:15:00 Tue  Improv in Eb, noisy
      3 audio007.wav  1938.3  3:13:28  2014-01-01 00:13:54  *listening*                                         Practice, Bach Air, Mozart
 ------------------------------------------------------------------------------------------------------------------------------------------
 prefix: piano  (alt-p)      inst: sv2  (alt-i)
 Filename:    piano.20211106-111300-Sat.Improv-in-G-,meditative.sv2.audio005.flac [67 chars]  (flac encoding, timestamp is lower bound)

Status wordset in final Filename line:
......................................

* listen   - speech-to-text is in-progress
* flacenc  - flac is currently being encoded
* pargen   - par2 generation of flac file in-progress
* parver   - par2 verification of flac file in-progress
* xdelta   - xdelta3 comparison of flacdec vs. source in-progress
* copyback - files are being cleaned up and flac copied back
* parver2  - par2 verification of the copied-back flac is in progress
* done     - file processing complete

Status letters in each file line:
.................................

* Column 1 - edit status:

  - ( ) space  - not committed or selected
  - (.) period - edited but not committed
  - (*) star   - row filename committed, rename completed
  - (>) arrow  - row currently selected for user update

* Column 2 - flac status:

  - ( ) space  - not started
  - (f)        - Flac encode in-progress
  - (f)        - Flac encode complete, par2 creation blocked by prompt
  - (p)        - par2 creation in-progress
  - (P)        - par2 check in-progress
  - (V)        - par2 verification success
  - (E)        - par2 verification error

* Column 3 - xdelta status:

  - ( ) space  - not started
  - (x)        - xdelta3 patch creation in-progress
  - (X)        - xdelta3 patch verification in-progress
  - (V)        - xdelta3 verification success
  - (E)        - xdelta3 verification error

* Column 4 - copyback status:

  - ( ) space  - not started
  - (c)        - flac copy-back ion in-progress
  - (C)        - copy-back flac par2 verification in-progress
  - (V)        - copy-back flac par2 verification success
  - (E)        - copy-back flac par2 verification error


Keybindings:
............
alt-i      Edit instrument name, enter to commit
alt-p      Edit prefix, enter to commit
alt-h      Launch mpv.  Uses the original .wav until the flac has completed encoding.
alt-m      Display file mtime
alt-c      Display file ctime
tab        Next field.  Jumps between Final Timestamp date, time, and between commas in Notes
shift-tab  Previous field.
up arrow   Select previous file.
down arrow Select next file.
enter      Move to next file name row
ctrl-enter Commit the current file name
ctrl-alt-enter  Commit all file names
