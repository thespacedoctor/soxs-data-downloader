#!/usr/bin/env python
"""
*Download every SOXS raw frame in the ESO Science Archive that is not yet on disk.*

The script asks the ESO archive for the SOXS raw frames in the chosen categories
(all categories by default), compares them with the frames found anywhere under
the data folder, and downloads only the missing ones. A
frame counts as present if its ``.fits``, ``.fits.Z`` or ``.fits.gz`` file exists
in any subfolder. New frames go into one folder per night, named ``YYYY-MM-DD``
after the UT date 12 hours before the observation.

You can set your ESO username, the data folder and the other defaults in the
settings block at the top of this script. A command-line flag always overrides
the matching setting. The ESO password is asked for once and then kept in the
system keyring. Use ``--reenter-password`` to replace a stored password that is
wrong. ``--start-night`` and ``--end-night`` restrict the query to an inclusive
range of UT nights (the same night convention as the folder names). Needs
astroquery 0.4.12 or later.

:Author:
    David Young

:Date Created:
    2026-10-02

Usage:
    soxs-data-downloader.py [--user=<username>] [--data-dir=<path>] [--category=<cat>...] [--start-night=<YYYY-MM-DD>] [--end-night=<YYYY-MM-DD>] [--dry-run] [--reenter-password]
    soxs-data-downloader.py -h | --help

Options:
    -h, --help                    show this help message
    --user=<username>             ESO User Portal username (overrides ESO_USERNAME)
    --data-dir=<path>             root folder of the local SOXS raw frames (overrides DATA_DIR)
    --category=<cat>              frame category to download: SCIENCE, CALIB, ACQUISITION, TECHNICAL, TEST, SIMULATION or OTHER; repeat the flag for more than one; all categories by default (overrides FRAME_CATEGORIES)
    --start-night=<YYYY-MM-DD>    only consider frames from this UT night onward (overrides DEFAULT_START_NIGHT)
    --end-night=<YYYY-MM-DD>      only consider frames up to and including this UT night (overrides DEFAULT_END_NIGHT)
    --dry-run                     list the missing frames and stop
    --reenter-password            ask for the ESO password and replace the one stored in the keyring
"""

import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from astropy.table import Table
from astroquery.eso import Eso
from astroquery.exceptions import RemoteServiceError

# ---------------- USER SETTINGS: EDIT THESE ----------------
ESO_USERNAME = None          # e.g. "your_eso_username"; --user overrides
DATA_DIR = None              # e.g. "/data/soxs/raw"; --data-dir overrides
FRAME_CATEGORIES = None     # None = all categories, or a list e.g. ["SCIENCE", "CALIB"]; --category overrides
DEFAULT_START_NIGHT = None   # "YYYY-MM-DD" or None; --start-night overrides
DEFAULT_END_NIGHT = None     # "YYYY-MM-DD" or None; --end-night overrides
STORE_PASSWORD = True        # keep ESO password in the system keyring
MAX_DOWNLOAD_ATTEMPTS = 5
RETRY_DELAY_STEP_SECONDS = 30
# -----------------------------------------------------------

SCRIPT_NAME = "soxs-data-downloader.py"
MIN_ASTROQUERY_VERSION = "0.4.12.dev0"
INSTRUMENT = "SOXS"
ARCHIVE_COLUMNS = ["dp_id", "dp_cat", "date_obs"]
FRAME_CATEGORY_CHOICES = ("SCIENCE", "CALIB", "ACQUISITION", "TECHNICAL", "TEST", "SIMULATION", "OTHER")
FRAME_SUFFIXES = (".fits.Z", ".fits.gz", ".fits")
NIGHT_ROLLOVER = timedelta(hours=12)
ARCHIVE_FILTER_MARGIN = timedelta(hours=1)
NIGHT_FORMAT = "%Y-%m-%d"
NO_ROW_LIMIT = -1
DOWNLOAD_ERRORS = (OSError, requests.RequestException, RemoteServiceError)
# A LOST OR STALLED CONNECTION. astroquery LETS THESE OUT OF retrieve_data, ENDING THE WHOLE NIGHT
RETRYABLE_ERRORS = (requests.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.Timeout, ConnectionError)


@dataclass(frozen=True)
class Options:
    """*the validated run options, built from the command line and the settings block*

    **Key Arguments:**

    - ``user`` -- ESO User Portal username
    - ``dataDir`` -- root folder of the local SOXS raw frames, with ``~`` expanded
    - ``categories`` -- the upper-case frame categories to download, or ``None`` for all of them
    - ``startNight`` -- first UT night to consider, or ``None``
    - ``endNight`` -- last UT night to consider, or ``None``
    - ``dryRun`` -- list the missing frames and stop
    - ``reenterPassword`` -- ask for a new ESO password and store it
    - ``storePassword`` -- keep the ESO password in the system keyring
    - ``maxAttempts`` -- tries per night before giving up on a lost connection
    - ``retryDelayStep`` -- seconds added to the wait after each failed try

    **Usage:**

        options = resolve_options(arguments)
    """

    user: str
    dataDir: Path
    categories: tuple[str, ...] | None
    startNight: date | None
    endNight: date | None
    dryRun: bool
    reenterPassword: bool
    storePassword: bool
    maxAttempts: int
    retryDelayStep: float


def frame_id_from_filename(filename: str) -> str | None:
    """*return the ESO ``dp_id`` for a frame file name*

    **Key Arguments:**

    - ``filename`` -- the file name, without any folder

    **Return:**

    - ``frameId`` -- the ``dp_id``, or ``None`` if the file is not a frame

    **Usage:**

        frameId = frame_id_from_filename("SOXS.2026-01-11T09:56:25.031.fits.Z")
    """
    for suffix in FRAME_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return None


def _raise_walk_error(error: OSError) -> None:
    """*raise the error that ``os.walk`` hands over, so an unreadable folder is not skipped*

    **Key Arguments:**

    - ``error`` -- the ``OSError`` raised while reading a folder

    **Return:**

    - none; the error is always raised
    """
    raise error


def index_local_frames(dataDir: str | Path) -> set[str]:
    """*return the ``dp_id`` of every frame file under a folder*

    A subfolder that cannot be read raises ``OSError``, because a skipped folder
    would make its frames look missing and cause them to be downloaded again.

    **Key Arguments:**

    - ``dataDir`` -- the root folder to search, including all subfolders

    **Return:**

    - ``frameIds`` -- the set of ``dp_id`` values found on disk

    **Usage:**

        localIds = index_local_frames("/data/soxs/raw")
    """
    frameIds = set()
    for _, _, filenames in os.walk(dataDir, onerror=_raise_walk_error):
        for filename in filenames:
            frameId = frame_id_from_filename(filename)
            if frameId:
                frameIds.add(frameId)
    return frameIds


def find_missing(archiveTable: Table, localIds: set[str]) -> Table:
    """*return the archive rows that are not on disk*

    **Key Arguments:**

    - ``archiveTable`` -- the archive rows, with a ``dp_id`` column
    - ``localIds`` -- the ``dp_id`` values found on disk

    **Return:**

    - ``missingTable`` -- the rows of ``archiveTable`` whose ``dp_id`` is not in ``localIds``

    **Usage:**

        missingTable = find_missing(archiveTable, localIds)
    """
    isMissing = [str(dpId) not in localIds for dpId in archiveTable["dp_id"]]
    return archiveTable[isMissing]


def night_folder(dateObs: str) -> str:
    """*return the night folder name for an ISO ``date_obs`` value*

    ESO gives ``date_obs`` in UT with no offset. A value that does carry an offset
    is converted to UT first.

    **Key Arguments:**

    - ``dateObs`` -- the ISO ``date_obs`` value

    **Return:**

    - ``night`` -- the night as ``YYYY-MM-DD``, the UT date 12 hours before the observation

    **Usage:**

        night = night_folder("2026-01-27T11:59:59.999")  # "2026-01-26"
    """
    observed = datetime.fromisoformat(str(dateObs))
    if observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc)
    return (observed - NIGHT_ROLLOVER).date().isoformat()


def night_bounds(startNight: date | None, endNight: date | None) -> tuple[datetime | None, datetime | None]:
    """*return the UT window covering every night from ``startNight`` to ``endNight``*

    A night runs from noon UT to the following noon UT, matching ``night_folder``.
    The window is ``[start, end)``.

    **Key Arguments:**

    - ``startNight`` -- first night, or ``None`` to leave the start open
    - ``endNight`` -- last night (inclusive), or ``None`` to leave the end open

    **Return:**

    - ``bounds`` -- the ``(startDt, endDt)`` pair, with ``None`` for an open side

    **Usage:**

        startDt, endDt = night_bounds(date(2026, 1, 26), date(2026, 1, 27))
    """
    startDt = datetime.combine(startNight, datetime.min.time()) + NIGHT_ROLLOVER if startNight else None
    endDt = datetime.combine(endNight, datetime.min.time()) + NIGHT_ROLLOVER + timedelta(days=1) if endNight else None
    return startDt, endDt


def tap_date_filter(bounds: tuple[datetime | None, datetime | None]) -> dict[str, str]:
    """*return the ``query_main`` ``column_filters`` on ``exp_start`` for a ``night_bounds`` window*

    The window is widened by ``ARCHIVE_FILTER_MARGIN`` on each side, because nights are
    defined by ``date_obs`` and ``exp_start`` can fall on the other side of a noon
    boundary. The archive then returns a superset, and ``filter_to_nights`` trims it.

    **Key Arguments:**

    - ``bounds`` -- the ``(startDt, endDt)`` pair from ``night_bounds``

    **Return:**

    - ``columnFilters`` -- the filter dictionary, empty if both sides are open

    **Usage:**

        columnFilters = tap_date_filter(night_bounds(startNight, endNight))
    """
    startDt, endDt = bounds
    start = (startDt - ARCHIVE_FILTER_MARGIN).isoformat(sep=" ") if startDt is not None else None
    end = (endDt + ARCHIVE_FILTER_MARGIN).isoformat(sep=" ") if endDt is not None else None
    if start and end:
        return {"exp_start": f"between '{start}' and '{end}'"}
    if start:
        return {"exp_start": f">= '{start}'"}
    if end:
        return {"exp_start": f"< '{end}'"}
    return {}


def filter_to_nights(table: Table, startNight: date | None, endNight: date | None) -> Table:
    """*return the rows whose night falls in ``[startNight, endNight]`` inclusive*

    This is the source of truth for the night range. The wider ADQL filter from
    ``tap_date_filter`` only reduces how many rows the archive returns.

    **Key Arguments:**

    - ``table`` -- the archive rows, with a ``date_obs`` column
    - ``startNight`` -- first night, or ``None`` to leave the start open
    - ``endNight`` -- last night, or ``None`` to leave the end open

    **Return:**

    - ``filteredTable`` -- the rows inside the range

    **Usage:**

        filteredTable = filter_to_nights(archiveTable, startNight, endNight)
    """
    if startNight is None and endNight is None:
        return table
    isInRange = []
    for dateObs in table["date_obs"]:
        night = date.fromisoformat(night_folder(dateObs))
        isInRange.append((startNight is None or night >= startNight) and (endNight is None or night <= endNight))
    return table[isInRange]


def filter_categories(table: Table, categories: tuple[str, ...] | None) -> Table:
    """*return the rows whose ``dp_cat`` is one of the wanted frame categories*

    **Key Arguments:**

    - ``table`` -- the archive rows, with a ``dp_cat`` column
    - ``categories`` -- the upper-case categories to keep, or ``None`` to keep every row

    **Return:**

    - ``filteredTable`` -- the rows in those categories

    **Usage:**

        filteredTable = filter_categories(archiveTable, ("SCIENCE", "CALIB"))
    """
    if categories is None:
        return table
    isWanted = [str(category) in categories for category in table["dp_cat"]]
    return table[isWanted]


def download_night(
    eso: Eso,
    night: str,
    ids: list[str],
    dataDir: str | Path,
    maxAttempts: int,
    retryDelayStep: float,
    log: logging.Logger,
) -> None:
    """*download the frames of one night into ``dataDir/night``*

    If the connection drops, wait and try again with only the frames that are not
    on disk yet, up to ``maxAttempts`` tries in total. The last error, or any error
    that is not in ``RETRYABLE_ERRORS``, is raised.

    **Key Arguments:**

    - ``eso`` -- the logged-in ``Eso`` client
    - ``night`` -- the night folder name, ``YYYY-MM-DD``
    - ``ids`` -- the ``dp_id`` values to download
    - ``dataDir`` -- the root data folder
    - ``maxAttempts`` -- tries in total before the error is raised
    - ``retryDelayStep`` -- seconds to wait after the first failure; the wait grows by this much after each further one
    - ``log`` -- the logger

    **Usage:**

        download_night(eso, "2026-01-26", ["SOXS.A"], "/data/soxs/raw", 5, 30, log)
    """
    nightDir = Path(dataDir) / night
    remaining = ids
    for attempt in range(1, maxAttempts + 1):
        try:
            eso.retrieve_data(remaining, destination=str(nightDir), unzip=True)
            return
        except RETRYABLE_ERRORS as error:
            if attempt == maxAttempts:
                raise
            onDisk = index_local_frames(nightDir) if nightDir.is_dir() else set()
            remaining = [frameId for frameId in ids if frameId not in onDisk]
            if not remaining:
                return
            delay = retryDelayStep * attempt
            log.warning(
                "Night %s: connection lost (%s). Retrying %d frames in %s s (attempt %d/%d).",
                night, error, len(remaining), delay, attempt + 1, maxAttempts,
            )
            time.sleep(delay)


def download_missing(
    eso: Eso,
    missingTable: Table,
    dataDir: str | Path,
    maxAttempts: int,
    retryDelayStep: float,
    log: logging.Logger,
) -> None:
    """*download the missing frames into their night folders*

    A night that still fails after ``download_night`` has retried it is logged and
    the next night is tried. The caller finds the frames that did not arrive by
    indexing the local tree again.

    **Key Arguments:**

    - ``eso`` -- the logged-in ``Eso`` client
    - ``missingTable`` -- the rows to download, with ``dp_id`` and ``date_obs`` columns
    - ``dataDir`` -- the root data folder
    - ``maxAttempts`` -- tries per night before giving up on it
    - ``retryDelayStep`` -- seconds added to the wait after each failed try
    - ``log`` -- the logger

    **Usage:**

        download_missing(eso, missingTable, "/data/soxs/raw", 5, 30, log)
    """
    from collections import defaultdict

    idsByNight = defaultdict(list)
    for row in missingTable:
        idsByNight[night_folder(row["date_obs"])].append(str(row["dp_id"]))

    for index, night in enumerate(sorted(idsByNight), 1):
        ids = idsByNight[night]
        log.info("Night %s (%d/%d): downloading %d frames", night, index, len(idsByNight), len(ids))
        try:
            download_night(eso, night, ids, dataDir, maxAttempts, retryDelayStep, log)
        except DOWNLOAD_ERRORS as error:
            log.error("Night %s: download stopped (%s). Continuing with the next night.", night, error)


def summarise(table: Table) -> str:
    """*return a short ``CATEGORY=count`` summary of the ``dp_cat`` column*

    **Key Arguments:**

    - ``table`` -- the archive rows, with a ``dp_cat`` column

    **Return:**

    - ``summary`` -- for example ``CALIB=2, SCIENCE=1``, or ``none`` for an empty table

    **Usage:**

        summary = summarise(archiveTable)
    """
    from collections import Counter

    counts = Counter(str(category) for category in table["dp_cat"])
    return ", ".join(f"{category}={n}" for category, n in sorted(counts.items())) or "none"


def parse_night(value: str) -> date:
    """*parse a ``YYYY-MM-DD`` night*

    **Key Arguments:**

    - ``value`` -- the night text

    **Return:**

    - ``night`` -- the date; a ``ValueError`` is raised if ``value`` is not a valid ``YYYY-MM-DD`` date

    **Usage:**

        night = parse_night("2026-01-27")
    """
    try:
        return datetime.strptime(value, NIGHT_FORMAT).date()
    except (ValueError, TypeError) as error:
        raise ValueError(f"not a valid night (expected YYYY-MM-DD): {value}") from error


def _pick(cliValue: Any, flag: str, settingValue: Any, settingName: str) -> tuple[Any, str]:
    """*choose the command-line value if one was given, otherwise the setting*

    An explicitly empty string counts as given. Only an absent flag (``None``, or
    an empty list for a repeatable flag) falls back to the setting.

    **Key Arguments:**

    - ``cliValue`` -- the docopt value for the flag
    - ``flag`` -- the flag name, used in messages
    - ``settingValue`` -- the value from the settings block
    - ``settingName`` -- the setting name, used in messages

    **Return:**

    - ``picked`` -- the chosen value and the name of the flag or setting it came from
    """
    if cliValue is not None and cliValue != []:
        return cliValue, flag
    return settingValue, settingName


def _checked(check: Callable[[Any], Any], value: Any, label: str) -> Any:
    """*return the validated value, or exit with a message that names the flag or setting at fault*

    **Key Arguments:**

    - ``check`` -- a validator that raises ``ValueError`` for a bad value
    - ``value`` -- the value to validate
    - ``label`` -- the flag or setting name the value came from

    **Return:**

    - ``checked`` -- whatever ``check`` returns
    """
    try:
        return check(value)
    except ValueError as error:
        sys.exit(f"{label}: {error}")


def _require(value: Any, label: str, flag: str, settingName: str, noun: str) -> Any:
    """*return a value that is set and not empty, or exit with a message that says where to set it*

    **Key Arguments:**

    - ``value`` -- the picked value
    - ``label`` -- the flag or setting name the value came from
    - ``flag`` -- the command-line flag that can set it
    - ``settingName`` -- the setting that can set it
    - ``noun`` -- what the value is, for the message

    **Return:**

    - ``value`` -- the same value
    """
    where = f"set {settingName} in the settings block at the top of {SCRIPT_NAME}"
    if value is None:
        sys.exit(f"No {noun} given. Pass {flag}, or {where}.")
    if str(value).strip() == "":
        sys.exit(f"{label}: the {noun} is empty. Pass {flag} with a value, or {where}.")
    return value


def _check_username(value: Any) -> str:
    """*validate an ESO username*

    **Key Arguments:**

    - ``value`` -- the username

    **Return:**

    - ``username`` -- the username with surrounding spaces removed
    """
    if not isinstance(value, str):
        raise ValueError("must be text")
    return value.strip()


def _check_data_dir(value: Any) -> Path:
    """*validate the data folder, expanding ``~``, and check that it exists*

    **Key Arguments:**

    - ``value`` -- the folder path

    **Return:**

    - ``dataDir`` -- the expanded folder path
    """
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("must be a folder path")
    dataDir = Path(os.path.expanduser(value))
    if not dataDir.is_dir():
        raise ValueError(f"data folder not found (is the volume mounted?): {dataDir}")
    return dataDir


def _check_categories(value: Any) -> tuple[str, ...] | None:
    """*validate the frame categories and put them in upper case*

    **Key Arguments:**

    - ``value`` -- a list or tuple of category names, or ``None`` for all categories

    **Return:**

    - ``categories`` -- the upper-case categories without repeats, or ``None``
    """
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError("must be a list of frame categories")
    if not value:
        raise ValueError("needs at least one frame category")
    categories = tuple(dict.fromkeys(str(category).strip().upper() for category in value))
    unknown = [category for category in categories if category not in FRAME_CATEGORY_CHOICES]
    if unknown:
        raise ValueError(f"unknown frame category {', '.join(unknown)}; choose from {', '.join(FRAME_CATEGORY_CHOICES)}")
    return categories


def _check_night(value: Any) -> date | None:
    """*validate a night given as ``YYYY-MM-DD``, or ``None``*

    **Key Arguments:**

    - ``value`` -- the night text, or ``None`` to leave the side open

    **Return:**

    - ``night`` -- the date, or ``None``
    """
    return None if value is None else parse_night(value)


def _check_max_attempts(value: Any) -> int:
    """*validate the number of download attempts*

    **Key Arguments:**

    - ``value`` -- a whole number of at least 1

    **Return:**

    - ``maxAttempts`` -- the same number
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("must be a whole number of at least 1")
    return value


def _check_retry_delay(value: Any) -> float:
    """*validate the retry delay step*

    **Key Arguments:**

    - ``value`` -- a finite number of seconds, 0 or more

    **Return:**

    - ``retryDelayStep`` -- the same number
    """
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("must be a finite number of at least 0")
    return value


def _check_store_password(value: Any) -> bool:
    """*validate the store-password setting*

    **Key Arguments:**

    - ``value`` -- ``True`` or ``False``

    **Return:**

    - ``storePassword`` -- the same bool
    """
    if not isinstance(value, bool):
        raise ValueError("must be True or False")
    return value


def _log_to_stderr() -> None:
    """*move the console log handlers from stdout to stderr*

    The ``--dry-run`` listing is written to stdout, so log lines must not share it.
    The handler format and level that ``fundamentals`` set up are kept.

    **Return:**

    - none; the root logger handlers are changed in place
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setStream(sys.stderr)


def resolve_options(arguments: dict[str, Any]) -> Options:
    """*merge the docopt arguments with the settings block into validated options*

    A command-line flag beats the matching setting. The username and the data
    folder have no built-in default, so the script exits if neither is given. Every
    value gets the same validation, whichever place it came from, and a failure
    names the flag or setting to fix.

    **Key Arguments:**

    - ``arguments`` -- the docopt dictionary of command-line arguments

    **Return:**

    - ``options`` -- the validated, immutable ``Options``

    **Usage:**

        options = resolve_options(docopt(__doc__))
    """
    userValue, userLabel = _pick(arguments["--user"], "--user", ESO_USERNAME, "ESO_USERNAME")
    dataDirValue, dataDirLabel = _pick(arguments["--data-dir"], "--data-dir", DATA_DIR, "DATA_DIR")
    categoriesValue, categoriesLabel = _pick(arguments["--category"], "--category", FRAME_CATEGORIES, "FRAME_CATEGORIES")
    startValue, startLabel = _pick(arguments["--start-night"], "--start-night", DEFAULT_START_NIGHT, "DEFAULT_START_NIGHT")
    endValue, endLabel = _pick(arguments["--end-night"], "--end-night", DEFAULT_END_NIGHT, "DEFAULT_END_NIGHT")

    user = _checked(_check_username, _require(userValue, userLabel, "--user", "ESO_USERNAME", "ESO username"), userLabel)
    dataDir = _checked(_check_data_dir, _require(dataDirValue, dataDirLabel, "--data-dir", "DATA_DIR", "data folder"), dataDirLabel)
    categories = _checked(_check_categories, categoriesValue, categoriesLabel)
    startNight = _checked(_check_night, startValue, startLabel)
    endNight = _checked(_check_night, endValue, endLabel)
    if startNight and endNight and startNight > endNight:
        sys.exit(f"{startLabel} ({startNight}) is after {endLabel} ({endNight})")

    return Options(
        user=user,
        dataDir=dataDir,
        categories=categories,
        startNight=startNight,
        endNight=endNight,
        dryRun=bool(arguments["--dry-run"]),
        reenterPassword=bool(arguments["--reenter-password"]),
        storePassword=_checked(_check_store_password, STORE_PASSWORD, "STORE_PASSWORD"),
        maxAttempts=_checked(_check_max_attempts, MAX_DOWNLOAD_ATTEMPTS, "MAX_DOWNLOAD_ATTEMPTS"),
        retryDelayStep=_checked(_check_retry_delay, RETRY_DELAY_STEP_SECONDS, "RETRY_DELAY_STEP_SECONDS"),
    )


def require_astroquery() -> None:
    """*exit if the installed astroquery is too old for the ESO API used here*

    **Usage:**

        require_astroquery()
    """
    from astroquery import __version__ as astroqueryVersion
    from packaging.version import Version

    if Version(astroqueryVersion) < Version(MIN_ASTROQUERY_VERSION):
        sys.exit(
            f"astroquery {astroqueryVersion} is too old; 0.4.12 or later is needed. "
            'Run: pip install --pre -U "astroquery>=0.4.12.dev0"'
        )


def login(user: str, reenterPassword: bool, storePassword: bool) -> Eso:
    """*return an authenticated ``Eso`` client, or exit if the login fails*

    astroquery saves a typed password in the keyring before it checks it, so a
    wrong password is reused on every run until ``reenterPassword`` replaces it.
    The stored password is never deleted automatically, because astroquery reports
    an ESO server error the same way as a wrong password.

    **Key Arguments:**

    - ``user`` -- the ESO User Portal username
    - ``reenterPassword`` -- ask for a new password and store it
    - ``storePassword`` -- keep the password in the system keyring

    **Return:**

    - ``eso`` -- the logged-in ``Eso`` client

    **Usage:**

        eso = login("your_eso_username", False, True)
    """
    eso = Eso()
    eso.ROW_LIMIT = NO_ROW_LIMIT
    eso.login(username=user, store_password=storePassword, reenter_password=reenterPassword)
    if not eso.authenticated():
        sys.exit(
            f"ESO login failed for user '{user}'. If the stored password is wrong, "
            "run again with --reenter-password to type and store a new one."
        )
    return eso


def query_archive(
    eso: Eso,
    startNight: date | None,
    endNight: date | None,
    categories: tuple[str, ...] | None,
    log: logging.Logger,
) -> Table:
    """*query the ESO archive for SOXS raw frames in a night range and set of categories*

    ``query_main`` can return ``None`` when there are no matching rows; that is
    treated as an empty table rather than left to crash the caller. The night
    filter runs first, then the category filter.

    **Key Arguments:**

    - ``eso`` -- the logged-in ``Eso`` client
    - ``startNight`` -- first night, or ``None`` to leave the start open
    - ``endNight`` -- last night, or ``None`` to leave the end open
    - ``categories`` -- the upper-case frame categories to keep, or ``None`` to keep all
    - ``log`` -- the logger

    **Return:**

    - ``archiveTable`` -- the matching rows, with ``dp_id``, ``dp_cat`` and ``date_obs`` columns

    **Usage:**

        archiveTable = query_archive(eso, None, None, ("SCIENCE",), log)
    """
    if startNight or endNight:
        log.info("Querying the ESO archive for %s raw frames from night %s to %s", INSTRUMENT, startNight or "the beginning", endNight or "now")
        columnFilters = tap_date_filter(night_bounds(startNight, endNight))
        archiveTable = eso.query_main(INSTRUMENT, columns=ARCHIVE_COLUMNS, authenticated=True, column_filters=columnFilters)
    else:
        log.info("Querying the ESO archive for all %s raw frames", INSTRUMENT)
        archiveTable = eso.query_main(INSTRUMENT, columns=ARCHIVE_COLUMNS, authenticated=True)
    if archiveTable is None:
        archiveTable = Table(names=ARCHIVE_COLUMNS)
    return filter_categories(filter_to_nights(archiveTable, startNight, endNight), categories)


def sync_archive(eso: Eso, options: Options, log: logging.Logger) -> int:
    """*download the frames that are in the archive but not on disk*

    **Key Arguments:**

    - ``eso`` -- the logged-in ``Eso`` client
    - ``options`` -- the validated run options
    - ``log`` -- the logger

    **Return:**

    - ``status`` -- the exit code: 0 on success (or a dry run), 1 if the local folder cannot be read or any frame did not arrive

    **Usage:**

        status = sync_archive(eso, options, log)
    """
    archiveTable = query_archive(eso, options.startNight, options.endNight, options.categories, log)
    log.info("Archive holds %d frames (%s)", len(archiveTable), summarise(archiveTable))

    log.info("Indexing local frames under %s", options.dataDir)
    try:
        localIds = index_local_frames(options.dataDir)
    except OSError as error:
        log.error("Cannot read the local data folder, so nothing was downloaded: %s", error)
        return 1
    missing = find_missing(archiveTable, localIds)
    log.info("%d frames are missing locally (%s)", len(missing), summarise(missing))

    if options.dryRun:
        for row in missing:
            print(f"{row['dp_id']}\t{row['dp_cat']}\t{night_folder(row['date_obs'])}")
        return 0
    if len(missing) == 0:
        return 0

    download_missing(eso, missing, options.dataDir, options.maxAttempts, options.retryDelayStep, log)

    return _verify_download(missing, options, log)


def _verify_download(missing: Table, options: Options, log: logging.Logger) -> int:
    """*check that every missing frame is now on disk and report the ones that are not*

    **Key Arguments:**

    - ``missing`` -- the rows that were to be downloaded
    - ``options`` -- the validated run options
    - ``log`` -- the logger

    **Return:**

    - ``status`` -- 0 if every frame arrived, 1 if any did not or the local folder cannot be read
    """
    try:
        onDisk = index_local_frames(options.dataDir)
    except OSError as error:
        log.error("Cannot read the local data folder to check the download: %s", error)
        return 1
    stillMissing = find_missing(missing, onDisk)
    if len(stillMissing):
        log.error("%d frames could not be downloaded (for example, a lost connection or no access to proprietary data):", len(stillMissing))
        for dpId in stillMissing["dp_id"]:
            log.error("  %s", dpId)
        return 1
    log.info("All %d missing frames downloaded", len(missing))
    return 0


def main(arguments: dict[str, Any] | None = None) -> int:
    """*the main function used when ``soxs-data-downloader.py`` is run as a single script from the command line*

    **Key Arguments:**

    - ``arguments`` -- a docopt dictionary; read from the command line when ``None``

    **Return:**

    - ``status`` -- the exit code, 0 on success

    **Usage:**

        status = main()
    """
    from fundamentals import tools

    # SETUP THE COMMAND-LINE UTIL SETTINGS
    su = tools(
        arguments=arguments,
        docString=__doc__,
        logLevel="INFO",
        options_first=False,
        projectName=False,
        defaultSettingsFile=False,
    )
    arguments, _, log, _ = su.setup()
    _log_to_stderr()

    options = resolve_options(arguments)
    require_astroquery()
    eso = login(options.user, options.reenterPassword, options.storePassword)
    return sync_archive(eso, options, log)


if __name__ == "__main__":
    sys.exit(main())
