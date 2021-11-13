#!/usr/bin/env bash

# Runs conditional (branch) coverage, generates an html report, and opens the
# report in a web browser

if which coverage > /dev/null; then

    echo "NOTE: coverage doesn't work on some NFS mounts - it hangs due to lack of lock support!"
    echo
    coverage erase

    # strace reports:
    # fcntl(3, F_SETLK, {l_type=F_RDLCK, l_whence=SEEK_SET, l_start=1073741824, l_len=1}) = -1 ENOLCK (No locks available)
    set -e -x
    time TEST_TAKETAKE_DONTSKIP=1 coverage run --branch -m unittest discover
    coverage html
    xdg-open htmlcov/index.html

else
    echo "No coverage tool found.  Install Python coverage with:"
    echo "   python3 -m pip install --user coverage"
    exit 2
fi
