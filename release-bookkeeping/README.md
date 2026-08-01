# Release bookkeeping

This directory contains canonical post-publication artifact digest records.
Use `scripts/release_bookkeeping.py` to create a record and its commit subject.
The generated subject always contains `[skip release]`, and the auto-release
workflow ignores this directory as a second, path-level loop prevention guard.
