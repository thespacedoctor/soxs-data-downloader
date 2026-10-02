# Changes

## Unreleased

- **FEATURE**: Initial release: settings block, docopt command line, category filter, night range, retries on lost connections, and keyring password storage.
- **ENHANCEMENT**: Downloaded frames stay compressed by default; `--unzip` or `UNZIP_FRAMES = True` unzips them.
- **ENHANCEMENT**: Quiet output by default: new `LOG_LEVEL` setting (default `"WARNING"`), a start-of-run summary of archive, on-disk, and missing frames, and a `tqdm` progress bar; frames now download one at a time.
- **ENHANCEMENT**: `MAX_DOWNLOAD_ATTEMPTS` now counts tries in a row that download no frame, so a long night with scattered connection drops is no longer abandoned.
- **ENHANCEMENT**: The start-of-run "Already on disk" and "missing locally" lines are replaced by a per-category status table (archive, share, on disk, missing, downloaded %, progress bar), coloured green, yellow, or red by completeness when stderr is a terminal and `NO_COLOR` is not set.
- **FIXED**: astroquery warnings and errors (for example "Access denied") were hidden, and its INFO lines went to stdout; they now go to stderr at the chosen `LOG_LEVEL`.
