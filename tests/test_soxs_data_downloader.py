"""Unit tests for soxs-data-downloader.py. No network access is needed."""

import importlib.util
import logging
import re
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from astropy.table import Table
from docopt import docopt

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "soxs-data-downloader.py"
SPEC = importlib.util.spec_from_file_location("soxs_data_downloader", SCRIPT_PATH)
dsd = importlib.util.module_from_spec(SPEC)
sys.modules["soxs_data_downloader"] = dsd
SPEC.loader.exec_module(dsd)

BLANK_SETTINGS = {
    "ESO_USERNAME": None,
    "DATA_DIR": None,
    "FRAME_CATEGORIES": None,
    "DEFAULT_START_NIGHT": None,
    "DEFAULT_END_NIGHT": None,
    "STORE_PASSWORD": True,
    "UNZIP_FRAMES": False,
    "MAX_DOWNLOAD_ATTEMPTS": 5,
    "RETRY_DELAY_STEP_SECONDS": 30,
    "LOG_LEVEL": "WARNING",
}
ATTEMPTS = 5
DELAY_STEP = 30


@pytest.fixture(autouse=True)
def restore_astroquery_log():
    # main() CHANGES ASTROQUERY'S PROCESS-WIDE LOGGER
    astroqueryLog = dsd.astroqueryLog
    saved = (astroqueryLog.level, astroqueryLog.disabled, astroqueryLog.propagate, list(astroqueryLog.handlers))
    yield
    astroqueryLog.setLevel(saved[0])
    astroqueryLog.disabled = saved[1]
    astroqueryLog.propagate = saved[2]
    astroqueryLog.handlers[:] = saved[3]


@pytest.fixture(autouse=True)
def blank_settings(monkeypatch):
    """Reset the settings block so a user's edits never change a test."""
    for name, value in BLANK_SETTINGS.items():
        monkeypatch.setattr(dsd, name, value)


@pytest.fixture
def log():
    return mock.Mock()


def make_archive_table(rows):
    return Table(rows=rows, names=("dp_id", "dp_cat", "date_obs"))


def make_eso(table=None, authenticated=True):
    eso = mock.Mock()
    eso.authenticated.return_value = authenticated
    eso.query_main.return_value = table
    return eso


def cli(*flags):
    """Return the docopt dict for the given command-line flags."""
    return docopt(dsd.__doc__, argv=list(flags))


def run_main(eso, tmp_path, *flags):
    arguments = cli("--data-dir", str(tmp_path), "--user", "dave", *flags)
    with mock.patch.object(dsd, "Eso", return_value=eso):
        return dsd.main(arguments)


def exit_message(*flags):
    with pytest.raises(SystemExit) as excinfo:
        dsd.resolve_options(cli(*flags))
    return str(excinfo.value.code)


def test_frame_id_is_read_from_uncompressed_and_compressed_names():
    # ARRANGE
    names = [
        "SOXS.2026-01-11T09:56:25.031.fits",
        "SOXS.2026-01-11T09:56:25.031.fits.Z",
        "SOXS.2026-01-11T09:56:25.031.fits.gz",
    ]

    # ACT
    ids = [dsd.frame_id_from_filename(n) for n in names]

    # ASSERT
    assert ids == ["SOXS.2026-01-11T09:56:25.031"] * 3


@pytest.mark.parametrize("name", [".DS_Store", "notes.txt", "SOXS.2026-01-11T09:56:25.031.fits.part"])
def test_frame_id_is_none_for_files_that_are_not_frames(name):
    assert dsd.frame_id_from_filename(name) is None


def test_local_index_searches_every_subfolder(tmp_path):
    # ARRANGE
    (tmp_path / "2026-01-10").mkdir()
    (tmp_path / "2026-01-11" / "deep").mkdir(parents=True)
    (tmp_path / "2026-01-10" / "SOXS.2026-01-11T01:00:00.000.fits").touch()
    (tmp_path / "2026-01-11" / "deep" / "SOXS.2026-01-11T20:00:00.000.fits.Z").touch()
    (tmp_path / "2026-01-11" / "deep" / "SOXS.2026-01-11T21:00:00.000.fits.gz").touch()
    (tmp_path / "2026-01-11" / "SOXS.2026-01-11T22:00:00.000.fits.part").touch()
    (tmp_path / ".DS_Store").touch()

    # ACT
    ids = dsd.index_local_frames(tmp_path)

    # ASSERT
    assert ids == {
        "SOXS.2026-01-11T01:00:00.000",
        "SOXS.2026-01-11T20:00:00.000",
        "SOXS.2026-01-11T21:00:00.000",
    }


def test_local_index_raises_when_a_subfolder_cannot_be_read(tmp_path):
    # ARRANGE
    def failing_walk(top, onerror=None):
        onerror(PermissionError(13, "Permission denied", str(tmp_path / "2026-01-11")))
        return iter([])

    # ACT / ASSERT
    with mock.patch.object(dsd.os, "walk", side_effect=failing_walk), pytest.raises(OSError):
        dsd.index_local_frames(tmp_path)


def test_find_missing_keeps_only_archive_rows_not_on_disk():
    # ARRANGE
    table = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-11T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-11T10:00:00"),
    ])

    # ACT
    missing = dsd.find_missing(table, {"SOXS.A"})

    # ASSERT
    assert list(missing["dp_id"]) == ["SOXS.B"]


@pytest.mark.parametrize(
    ("dateObs", "expected"),
    [
        ("2026-01-27T11:59:59.999", "2026-01-26"),
        ("2026-01-27T12:00:00.000", "2026-01-27"),
        ("2026-01-27T23:30:00.0315", "2026-01-27"),
        ("2026-01-01T03:00:00", "2025-12-31"),
        ("2026-01-27T12:30:00Z", "2026-01-27"),
        ("2026-01-27T12:30:00+01:00", "2026-01-26"),
    ],
)
def test_night_folder_rolls_back_before_noon_utc(dateObs, expected):
    assert dsd.night_folder(dateObs) == expected


def test_summarise_counts_frames_per_category():
    # ARRANGE
    table = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-11T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-11T10:00:00"),
        ("SOXS.C", "CALIB", "2026-01-11T11:00:00"),
    ])

    # ACT / ASSERT
    assert dsd.summarise(table) == "CALIB=2, SCIENCE=1"


def test_summarise_says_none_for_an_empty_table():
    assert dsd.summarise(make_archive_table([])) == "none"


ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text):
    return ANSI_PATTERN.sub("", text)


def make_status_rows():
    return [
        dsd.StatusRow("ACQUISITION", 2029, 2029, 0),
        dsd.StatusRow("CALIB", 58514, 58085, 429),
        dsd.StatusRow("SCIENCE", 2792, 2792, 0),
        dsd.StatusRow("Total", 63335, 62906, 429),
    ]


def test_category_counts_gives_sorted_category_rows_then_a_total_row():
    # ARRANGE
    archiveTable = make_archive_table([
        ("SOXS.A", "SCIENCE", "2026-01-11T09:00:00"),
        ("SOXS.B", "CALIB", "2026-01-11T10:00:00"),
        ("SOXS.C", "CALIB", "2026-01-11T11:00:00"),
        ("SOXS.D", "CALIB", "2026-01-11T12:00:00"),
    ])
    presentTable = dsd.find_present(archiveTable, {"SOXS.A", "SOXS.B"})

    # ACT
    rows = dsd.category_counts(archiveTable, presentTable)

    # ASSERT
    assert rows == [
        dsd.StatusRow("CALIB", 3, 1, 2),
        dsd.StatusRow("SCIENCE", 1, 1, 0),
        dsd.StatusRow("Total", 4, 2, 2),
    ]


def test_category_counts_shows_zero_present_for_a_category_with_nothing_on_disk():
    # ARRANGE
    archiveTable = make_archive_table([("SOXS.A", "CALIB", "2026-01-11T09:00:00")])
    presentTable = dsd.find_present(archiveTable, set())

    # ACT
    rows = dsd.category_counts(archiveTable, presentTable)

    # ASSERT
    assert rows[0] == dsd.StatusRow("CALIB", 1, 0, 1)


def test_status_row_is_frozen():
    # ARRANGE
    row = dsd.StatusRow("CALIB", 3, 1, 2)

    # ACT / ASSERT
    with pytest.raises(FrozenInstanceError):
        row.archive = 4


@pytest.mark.parametrize(
    "present, total, expected",
    [
        (10, 10, "█" * 20),
        (0, 10, " " * 20),
        (1, 2, "█" * 10 + " " * 10),
        (6, 1000, " " * 20),
        (7, 1000, "▏" + " " * 19),
        (50, 1000, "█" + " " * 19),
        (63, 1000, "█▎" + " " * 18),
    ],
)
def test_progress_bar_fills_eighth_cells_and_floors(present, total, expected):
    assert dsd.progress_bar(present, total) == expected


def test_progress_bar_stays_one_eighth_short_of_full_when_a_frame_is_missing():
    # ACT
    bar = dsd.progress_bar(99999, 100000)

    # ASSERT
    assert bar == "█" * 19 + "▉"


def test_progress_bar_honours_a_custom_width():
    assert dsd.progress_bar(1, 2, width=4) == "██  "


def test_progress_bar_rejects_an_empty_total():
    with pytest.raises(ValueError, match="empty"):
        dsd.progress_bar(0, 0)


@pytest.mark.parametrize(
    "present, total, expected",
    [
        (1000, 1000, dsd.ANSI_GREEN),
        (999, 1000, dsd.ANSI_YELLOW),
        (900, 1000, dsd.ANSI_YELLOW),
        (899, 1000, dsd.ANSI_RED),
        (0, 1000, dsd.ANSI_RED),
    ],
)
def test_completion_colour_is_green_when_complete_yellow_when_near_and_red_below(present, total, expected):
    assert dsd.completion_colour(present, total) == expected


def test_completion_colour_rejects_an_empty_total():
    with pytest.raises(ValueError, match="empty"):
        dsd.completion_colour(0, 0)


def test_use_colour_is_true_for_a_terminal_without_no_color(monkeypatch):
    # ARRANGE
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = mock.Mock()
    stream.isatty.return_value = True

    # ACT / ASSERT
    assert dsd.use_colour(stream) is True


def test_use_colour_is_false_when_the_stream_is_not_a_terminal(monkeypatch):
    # ARRANGE
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = mock.Mock()
    stream.isatty.return_value = False

    # ACT / ASSERT
    assert dsd.use_colour(stream) is False


def test_use_colour_is_false_when_no_color_is_set(monkeypatch):
    # ARRANGE
    monkeypatch.setenv("NO_COLOR", "1")
    stream = mock.Mock()
    stream.isatty.return_value = True

    # ACT / ASSERT
    assert dsd.use_colour(stream) is False


def test_use_colour_ignores_an_empty_no_color(monkeypatch):
    # ARRANGE
    monkeypatch.setenv("NO_COLOR", "")
    stream = mock.Mock()
    stream.isatty.return_value = True

    # ACT / ASSERT
    assert dsd.use_colour(stream) is True


def test_use_colour_is_false_when_there_is_no_stream(monkeypatch):
    # ARRANGE
    monkeypatch.delenv("NO_COLOR", raising=False)

    # ACT / ASSERT
    assert dsd.use_colour(None) is False


def test_use_colour_is_false_when_the_stream_has_no_isatty(monkeypatch):
    # ARRANGE
    monkeypatch.delenv("NO_COLOR", raising=False)

    # ACT / ASSERT
    assert dsd.use_colour(object()) is False


def test_format_status_table_plain_text_aligns_every_column():
    # ARRANGE
    rows = make_status_rows()

    # ACT
    lines = dsd.format_status_table(rows, colour=False)

    # ASSERT
    assert lines == [
        "Category     Archive   Share  On disk  Missing  Downloaded  Progress" + " " * 12,
        "ACQUISITION    2,029    3.2%    2,029        0      100.0%  " + "█" * 20,
        "CALIB         58,514   92.4%   58,085      429       99.2%  " + "█" * 19 + "▊",
        "SCIENCE        2,792    4.4%    2,792        0      100.0%  " + "█" * 20,
        "Total         63,335  100.0%   62,906      429       99.3%  " + "█" * 19 + "▊",
    ]


def test_format_status_table_keeps_the_full_bar_width_for_a_row_with_nothing_on_disk():
    # ARRANGE
    rows = [dsd.StatusRow("CALIB", 5, 0, 5), dsd.StatusRow("Total", 5, 0, 5)]

    # ACT
    lines = dsd.format_status_table(rows, colour=False)

    # ASSERT
    assert lines[1].endswith("  0.0%  " + " " * 20)
    assert lines[2].endswith("  0.0%  " + " " * 20)


def test_category_counts_gives_no_rows_for_an_empty_archive():
    # ARRANGE
    emptyTable = make_archive_table([])

    # ACT
    rows = dsd.category_counts(emptyTable, dsd.find_present(emptyTable, set()))

    # ASSERT
    assert rows == []


def test_format_status_table_gives_no_lines_for_an_empty_archive():
    # ARRANGE
    emptyTable = make_archive_table([])
    rows = dsd.category_counts(emptyTable, dsd.find_present(emptyTable, set()))

    # ACT / ASSERT
    assert dsd.format_status_table(rows, colour=False) == []
    assert dsd.format_status_table(rows, colour=True) == []


def test_format_status_table_plain_text_has_no_escape_codes():
    # ACT
    text = "\n".join(dsd.format_status_table(make_status_rows(), colour=False))

    # ASSERT
    assert "\x1b" not in text


def test_format_status_table_colour_keeps_the_same_visible_text():
    # ARRANGE
    rows = make_status_rows()

    # ACT
    plain = dsd.format_status_table(rows, colour=False)
    coloured = dsd.format_status_table(rows, colour=True)

    # ASSERT
    assert [strip_ansi(line) for line in coloured] == plain


def test_format_status_table_colour_makes_header_and_total_bold():
    # ACT
    lines = dsd.format_status_table(make_status_rows(), colour=True)

    # ASSERT
    assert dsd.ANSI_BOLD in lines[0]
    assert dsd.ANSI_BOLD in lines[-1]
    assert dsd.ANSI_BOLD not in lines[1]


def test_format_status_table_colour_marks_missing_red_and_zero_missing_dim():
    # ACT
    lines = dsd.format_status_table(make_status_rows(), colour=True)

    # ASSERT
    assert f"    {dsd.ANSI_RED}429{dsd.ANSI_RESET}" in lines[2]
    assert f"      {dsd.ANSI_DIM}0{dsd.ANSI_RESET}" in lines[1]


def test_format_status_table_colour_grades_downloaded_percent_and_bar_by_completion():
    # ARRANGE
    rows = [
        dsd.StatusRow("A", 10, 10, 0),
        dsd.StatusRow("B", 100, 95, 5),
        dsd.StatusRow("C", 100, 50, 50),
        dsd.StatusRow("Total", 210, 155, 55),
    ]

    # ACT
    lines = dsd.format_status_table(rows, colour=True)

    # ASSERT
    assert f"    {dsd.ANSI_GREEN}100.0%{dsd.ANSI_RESET}" in lines[1]
    assert f"{dsd.ANSI_GREEN}{'█' * 20}{dsd.ANSI_RESET}" in lines[1]
    assert f"     {dsd.ANSI_YELLOW}95.0%{dsd.ANSI_RESET}" in lines[2]
    assert f"     {dsd.ANSI_RED}50.0%{dsd.ANSI_RESET}" in lines[3]
    assert f"{dsd.ANSI_RED}{'█' * 10}{dsd.ANSI_RESET}" in lines[3]


def test_find_present_keeps_only_archive_rows_on_disk():
    # ARRANGE
    archiveTable = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-11T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-11T10:00:00"),
    ])

    # ACT
    present = dsd.find_present(archiveTable, {"SOXS.B", "SOXS.Z"})

    # ASSERT
    assert list(present["dp_id"]) == ["SOXS.B"]


@pytest.mark.parametrize(
    ("presentCount", "archiveCount", "expected"),
    [
        (947, 1000, "94.7%"),
        (9999, 10000, "99.9%"),
        (0, 5, "0.0%"),
        (5, 5, "100.0%"),
        (1, 3, "33.3%"),
    ],
)
def test_percent_downloaded_rounds_down_to_one_decimal(presentCount, archiveCount, expected):
    assert dsd.percent_downloaded(presentCount, archiveCount) == expected


def test_percent_downloaded_rejects_an_empty_archive():
    with pytest.raises(ValueError, match="empty"):
        dsd.percent_downloaded(0, 0)


def test_report_writes_to_stderr_only(capsys):
    # ACT
    dsd.report("Archive holds 3 frames")

    # ASSERT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Archive holds 3 frames\n"


def test_download_calls_retrieve_data_once_per_frame_into_its_night_folder(tmp_path, log):
    # ARRANGE
    eso = mock.Mock()
    missing = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-27T09:00:00"),
        ("SOXS.B", "CALIB", "2026-01-26T22:00:00"),
        ("SOXS.C", "SCIENCE", "2026-01-27T20:00:00"),
    ])

    # ACT
    dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_args_list == [
        mock.call(["SOXS.A"], destination=str(tmp_path / "2026-01-26"), unzip=False),
        mock.call(["SOXS.B"], destination=str(tmp_path / "2026-01-26"), unzip=False),
        mock.call(["SOXS.C"], destination=str(tmp_path / "2026-01-27"), unzip=False),
    ]


def test_download_night_reports_progress_once_per_frame(tmp_path, log):
    # ARRANGE
    eso = mock.Mock()
    progress = mock.Mock()

    # ACT
    dsd.download_night(eso, "2026-01-26", ["SOXS.A", "SOXS.B", "SOXS.C"], tmp_path, ATTEMPTS, DELAY_STEP, log, progress=progress)

    # ASSERT
    assert progress.call_args_list == [mock.call(1)] * 3


def test_download_night_retry_resumes_at_the_frame_that_dropped(tmp_path, log):
    # ARRANGE
    def drop_on_second_frame(ids, destination, unzip):
        if eso.retrieve_data.call_count == 2:
            raise dsd.requests.exceptions.ConnectionError("reset")

    eso = mock.Mock()
    eso.retrieve_data.side_effect = drop_on_second_frame
    progress = mock.Mock()

    # ACT
    with mock.patch.object(dsd.time, "sleep"):
        dsd.download_night(eso, "2026-01-26", ["SOXS.A", "SOXS.B", "SOXS.C"], tmp_path, ATTEMPTS, DELAY_STEP, log, progress=progress)

    # ASSERT
    requested = [call.args[0] for call in eso.retrieve_data.call_args_list]
    assert requested == [["SOXS.A"], ["SOXS.B"], ["SOXS.B"], ["SOXS.C"]]
    assert progress.call_count == 3


def test_download_night_keeps_retrying_while_each_try_makes_progress(tmp_path, log):
    # ARRANGE
    # EVERY FRAME AFTER THE FIRST DROPS ONCE, SO A FIXED BUDGET OF 2 TRIES WOULD ABANDON THE NIGHT
    seen = set()

    def drop_each_new_frame_once(ids, destination, unzip):
        frameId = ids[0]
        if frameId != "SOXS.A" and frameId not in seen:
            seen.add(frameId)
            raise dsd.requests.exceptions.ConnectionError("reset")

    eso = mock.Mock()
    eso.retrieve_data.side_effect = drop_each_new_frame_once
    progress = mock.Mock()

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep:
        dsd.download_night(eso, "2026-01-26", ["SOXS.A", "SOXS.B", "SOXS.C", "SOXS.D"], tmp_path, 2, DELAY_STEP, log, progress=progress)

    # ASSERT
    assert progress.call_count == 4
    assert sleep.call_args_list == [mock.call(DELAY_STEP)] * 3


def test_download_night_gives_up_after_tries_in_a_row_without_progress(tmp_path, log):
    # ARRANGE
    def fetch_first_then_always_drop(ids, destination, unzip):
        if ids[0] != "SOXS.A":
            raise dsd.requests.exceptions.ConnectionError("reset")

    eso = mock.Mock()
    eso.retrieve_data.side_effect = fetch_first_then_always_drop

    # ACT
    with mock.patch.object(dsd.time, "sleep"), pytest.raises(dsd.requests.ConnectionError):
        dsd.download_night(eso, "2026-01-26", ["SOXS.A", "SOXS.B"], tmp_path, 3, DELAY_STEP, log)

    # ASSERT
    requested = [call.args[0] for call in eso.retrieve_data.call_args_list]
    assert requested == [["SOXS.A"], ["SOXS.B"], ["SOXS.B"], ["SOXS.B"]]


@pytest.fixture
def visible_bars(monkeypatch):
    """Make download_missing draw its bar into a buffer, even though pytest has no terminal, and keep each bar."""
    import io

    from tqdm import tqdm as realTqdm

    bars = []

    def make_bar(**kwargs):
        bar = realTqdm(**{**kwargs, "disable": False, "file": io.StringIO()})
        bars.append(bar)
        return bar

    monkeypatch.setattr(dsd, "tqdm", make_bar)
    return bars


def test_download_progress_bar_counts_every_missing_frame(tmp_path, log, visible_bars):
    # ARRANGE
    eso = mock.Mock()
    missing = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-26T20:00:00"),
        ("SOXS.B", "CALIB", "2026-01-26T21:00:00"),
        ("SOXS.C", "SCIENCE", "2026-01-27T20:00:00"),
    ])

    # ACT
    dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    (bar,) = visible_bars
    assert bar.total == 3
    assert bar.n == 3


def test_download_progress_bar_still_reaches_the_total_when_a_night_fails(tmp_path, log, visible_bars):
    # ARRANGE
    eso = mock.Mock()
    eso.retrieve_data.side_effect = [dsd.RemoteServiceError("server error"), None]
    missing = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-26T20:00:00"),
        ("SOXS.B", "CALIB", "2026-01-26T21:00:00"),
        ("SOXS.C", "SCIENCE", "2026-01-27T20:00:00"),
    ])

    # ACT
    dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    (bar,) = visible_bars
    assert bar.n == 3
    assert eso.retrieve_data.call_count == 2


@pytest.mark.parametrize("unzip", [True, False])
def test_download_passes_the_unzip_choice_to_retrieve_data(tmp_path, log, unzip):
    # ARRANGE
    eso = mock.Mock()
    missing = make_archive_table([("SOXS.A", "CALIB", "2026-01-26T20:00:00")])

    # ACT
    dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log, unzip=unzip)

    # ASSERT
    eso.retrieve_data.assert_called_once_with(["SOXS.A"], destination=str(tmp_path / "2026-01-26"), unzip=unzip)


def test_download_retry_skips_a_frame_that_arrived_compressed(tmp_path, log):
    # ARRANGE
    nightDir = tmp_path / "2026-01-26"

    def drop_after_first_frame(ids, destination, unzip):
        if eso.retrieve_data.call_count == 1:
            nightDir.mkdir()
            (nightDir / "SOXS.A.fits.Z").touch()
            raise dsd.requests.exceptions.ConnectionError("reset")

    eso = mock.Mock()
    eso.retrieve_data.side_effect = drop_after_first_frame

    # ACT
    with mock.patch.object(dsd.time, "sleep"):
        dsd.download_night(eso, "2026-01-26", ["SOXS.A", "SOXS.B"], tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_args_list[1] == mock.call(["SOXS.B"], destination=str(nightDir), unzip=False)


def test_download_continues_with_next_night_when_one_night_raises(tmp_path, log):
    # ARRANGE
    eso = mock.Mock()
    eso.retrieve_data.side_effect = [dsd.RemoteServiceError("server error"), None]
    missing = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-26T20:00:00"),
        ("SOXS.B", "CALIB", "2026-01-27T20:00:00"),
    ])

    # ACT
    dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_count == 2
    log.error.assert_called_once()


def test_download_retries_a_dropped_night_with_only_the_frames_not_yet_on_disk(tmp_path, log):
    # ARRANGE
    nightDir = tmp_path / "2026-01-26"

    def drop_after_first_frame(ids, destination, unzip):
        if eso.retrieve_data.call_count == 1:
            nightDir.mkdir()
            (nightDir / "SOXS.A.fits").touch()
            raise dsd.requests.exceptions.ChunkedEncodingError("IncompleteRead")

    eso = mock.Mock()
    eso.retrieve_data.side_effect = drop_after_first_frame
    missing = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-26T20:00:00"),
        ("SOXS.B", "CALIB", "2026-01-26T21:00:00"),
    ])

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep:
        dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_args_list[1] == mock.call(["SOXS.B"], destination=str(nightDir), unzip=False)
    assert eso.retrieve_data.call_count == 2
    sleep.assert_called_once()
    log.warning.assert_called_once()


def test_download_gives_up_on_a_night_after_the_last_retry_and_continues(tmp_path, log):
    # ARRANGE
    maxAttempts = 3
    dropped = dsd.requests.exceptions.ConnectionError("reset")
    eso = mock.Mock()
    eso.retrieve_data.side_effect = [dropped] * maxAttempts + [None]
    missing = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-26T20:00:00"),
        ("SOXS.B", "CALIB", "2026-01-27T20:00:00"),
    ])

    # ACT
    with mock.patch.object(dsd.time, "sleep"):
        dsd.download_missing(eso, missing, tmp_path, maxAttempts, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_count == maxAttempts + 1
    assert eso.retrieve_data.call_args_list[-1] == mock.call(["SOXS.B"], destination=str(tmp_path / "2026-01-27"), unzip=False)


def test_download_waits_a_growing_multiple_of_the_delay_step_between_attempts(tmp_path, log):
    # ARRANGE
    eso = mock.Mock()
    eso.retrieve_data.side_effect = dsd.requests.exceptions.ConnectionError("reset")

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep, pytest.raises(dsd.requests.ConnectionError):
        dsd.download_night(eso, "2026-01-26", ["SOXS.A"], tmp_path, 3, 7, log)

    # ASSERT
    assert sleep.call_args_list == [mock.call(7), mock.call(14)]
    assert eso.retrieve_data.call_count == 3


def test_download_makes_a_single_attempt_when_max_attempts_is_one(tmp_path, log):
    # ARRANGE
    eso = mock.Mock()
    eso.retrieve_data.side_effect = dsd.requests.exceptions.ConnectionError("reset")

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep, pytest.raises(dsd.requests.ConnectionError):
        dsd.download_night(eso, "2026-01-26", ["SOXS.A"], tmp_path, 1, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_count == 1
    sleep.assert_not_called()


def test_download_does_not_retry_a_night_that_fails_with_a_server_error(tmp_path, log):
    # ARRANGE
    eso = mock.Mock()
    eso.retrieve_data.side_effect = dsd.RemoteServiceError("server error")
    missing = make_archive_table([("SOXS.A", "CALIB", "2026-01-26T20:00:00")])

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep:
        dsd.download_missing(eso, missing, tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_count == 1
    sleep.assert_not_called()


def test_download_stops_retrying_when_every_frame_arrived_before_the_drop(tmp_path, log):
    # ARRANGE
    nightDir = tmp_path / "2026-01-26"

    def drop_after_every_frame(ids, destination, unzip):
        nightDir.mkdir()
        (nightDir / "SOXS.A.fits").touch()
        raise dsd.requests.exceptions.ConnectionError("reset")

    eso = mock.Mock()
    eso.retrieve_data.side_effect = drop_after_every_frame

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep:
        dsd.download_night(eso, "2026-01-26", ["SOXS.A"], tmp_path, ATTEMPTS, DELAY_STEP, log)

    # ASSERT
    assert eso.retrieve_data.call_count == 1
    sleep.assert_not_called()


def test_frame_in_an_unrelated_night_folder_is_not_downloaded(tmp_path):
    # ARRANGE
    (tmp_path / "2025-05-03").mkdir()
    (tmp_path / "2025-05-03" / "SOXS.A.fits").touch()
    eso = make_eso(make_archive_table([("SOXS.A", "CALIB", "2026-01-27T09:00:00")]))

    # ACT
    status = run_main(eso, tmp_path)

    # ASSERT
    eso.retrieve_data.assert_not_called()
    assert status == 0


def test_main_stops_before_downloading_when_local_scan_fails(tmp_path):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.A", "CALIB", "2026-01-27T09:00:00")]))

    # ACT
    with mock.patch.object(dsd, "index_local_frames", side_effect=PermissionError("denied")):
        status = run_main(eso, tmp_path)

    # ASSERT
    eso.retrieve_data.assert_not_called()
    assert status == 1


def test_dry_run_queries_archive_but_downloads_nothing(tmp_path, capsys):
    # ARRANGE
    (tmp_path / "SOXS.A.fits").touch()
    eso = make_eso(make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T20:00:00"),
    ]))

    # ACT
    status = run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    eso.query_main.assert_called_once_with(
        "SOXS", columns=["dp_id", "dp_cat", "date_obs"], authenticated=True
    )
    eso.retrieve_data.assert_not_called()
    assert "SOXS.B" in capsys.readouterr().out
    assert status == 0


def test_dry_run_lists_each_missing_frame_as_id_category_night(tmp_path, capsys):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    assert "SOXS.B\tSCIENCE\t2026-01-27\n" in capsys.readouterr().out


def test_main_returns_error_when_frames_are_still_missing_after_download(tmp_path):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))
    # A MOCK THAT WRITES NOTHING STANDS IN FOR ASTROQUERY LOGGING A 401 AND RETURNING NORMALLY

    # ACT
    status = run_main(eso, tmp_path)

    # ASSERT
    eso.retrieve_data.assert_called_once()
    assert status == 1


def test_main_returns_zero_when_every_frame_arrives(tmp_path):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    def fake_retrieve(ids, destination, unzip):
        Path(destination).mkdir(parents=True, exist_ok=True)
        for dpId in ids:
            (Path(destination) / f"{dpId}.fits").touch()

    eso.retrieve_data.side_effect = fake_retrieve

    # ACT
    status = run_main(eso, tmp_path)

    # ASSERT
    assert (tmp_path / "2026-01-27" / "SOXS.B.fits").exists()
    assert status == 0


def test_main_returns_zero_without_downloading_when_nothing_is_missing(tmp_path):
    # ARRANGE
    (tmp_path / "SOXS.B.fits").touch()
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    status = run_main(eso, tmp_path)

    # ASSERT
    eso.retrieve_data.assert_not_called()
    assert status == 0


def test_main_fails_fast_when_data_dir_is_missing(tmp_path):
    # ARRANGE
    arguments = cli("--data-dir", str(tmp_path / "not-mounted"), "--user", "dave")

    # ACT / ASSERT
    with mock.patch.object(dsd, "Eso") as esoClass, pytest.raises(SystemExit, match="is the volume mounted"):
        dsd.main(arguments)
    esoClass.assert_not_called()


def test_main_fails_when_login_is_rejected(tmp_path):
    # ARRANGE
    eso = make_eso(authenticated=False)

    # ACT / ASSERT
    with pytest.raises(SystemExit, match="--reenter-password"):
        run_main(eso, tmp_path)
    eso.query_main.assert_not_called()


@pytest.mark.parametrize(("flags", "expected"), [([], False), (["--reenter-password"], True)])
def test_login_asks_for_a_new_password_only_when_requested(tmp_path, flags, expected):
    # ARRANGE
    eso = make_eso(make_archive_table([]))

    # ACT
    run_main(eso, tmp_path, *flags)

    # ASSERT
    eso.login.assert_called_once_with(username="dave", store_password=True, reenter_password=expected)


@pytest.mark.parametrize("storePassword", [True, False])
def test_login_passes_the_store_password_choice_to_eso(storePassword):
    # ARRANGE
    eso = make_eso()

    # ACT
    with mock.patch.object(dsd, "Eso", return_value=eso):
        dsd.login("dave", False, storePassword)

    # ASSERT
    eso.login.assert_called_once_with(username="dave", store_password=storePassword, reenter_password=False)


def test_login_removes_the_archive_row_limit():
    # ARRANGE
    eso = make_eso()

    # ACT
    with mock.patch.object(dsd, "Eso", return_value=eso):
        dsd.login("dave", False, True)

    # ASSERT
    assert eso.ROW_LIMIT == -1


def test_login_failure_message_suggests_reentering_the_password_and_keeps_it_stored():
    # ARRANGE
    eso = make_eso(authenticated=False)

    # ACT
    with mock.patch.object(dsd, "Eso", return_value=eso), pytest.raises(SystemExit) as excinfo:
        dsd.login("dave", False, True)

    # ASSERT
    assert "--reenter-password" in str(excinfo.value.code)
    eso.login.assert_called_once()


def test_main_uses_the_store_password_setting(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "STORE_PASSWORD", False)
    eso = make_eso(make_archive_table([]))

    # ACT
    run_main(eso, tmp_path)

    # ASSERT
    eso.login.assert_called_once_with(username="dave", store_password=False, reenter_password=False)


def test_main_exits_when_astroquery_is_too_old(tmp_path):
    # ARRANGE
    arguments = cli("--data-dir", str(tmp_path), "--user", "dave")

    # ACT / ASSERT
    with mock.patch("astroquery.__version__", "0.4.11"), \
            mock.patch.object(dsd, "Eso") as esoClass, \
            pytest.raises(SystemExit, match="0.4.12"):
        dsd.main(arguments)
    esoClass.assert_not_called()


def test_old_astroquery_message_gives_a_generic_pip_upgrade_command():
    # ARRANGE / ACT
    with mock.patch("astroquery.__version__", "0.4.11"), pytest.raises(SystemExit) as excinfo:
        dsd.require_astroquery()

    # ASSERT
    message = str(excinfo.value.code)
    assert 'pip install --pre -U "astroquery>=0.4.12.dev0"' in message
    assert "eso-download" not in message


def test_astroquery_dev_build_at_the_minimum_is_accepted():
    with mock.patch("astroquery.__version__", "0.4.12.dev1"):
        dsd.require_astroquery()


def test_night_bounds_for_a_single_night():
    # ARRANGE
    night = date(2026, 1, 27)

    # ACT
    startDt, endDt = dsd.night_bounds(night, night)

    # ASSERT
    assert startDt == datetime(2026, 1, 27, 12, 0, 0)
    assert endDt == datetime(2026, 1, 28, 12, 0, 0)


def test_night_bounds_with_open_start():
    # ARRANGE / ACT
    startDt, endDt = dsd.night_bounds(None, date(2026, 1, 27))

    # ASSERT
    assert startDt is None
    assert endDt == datetime(2026, 1, 28, 12, 0, 0)


def test_night_bounds_with_open_end():
    # ARRANGE / ACT
    startDt, endDt = dsd.night_bounds(date(2026, 1, 27), None)

    # ASSERT
    assert startDt == datetime(2026, 1, 27, 12, 0, 0)
    assert endDt is None


def test_night_bounds_across_a_year_boundary():
    # ARRANGE / ACT
    startDt, endDt = dsd.night_bounds(date(2025, 12, 30), date(2026, 1, 1))

    # ASSERT
    assert startDt == datetime(2025, 12, 30, 12, 0, 0)
    assert endDt == datetime(2026, 1, 2, 12, 0, 0)


def test_tap_date_filter_for_a_range_with_both_sides():
    # ARRANGE
    bounds = (datetime(2026, 1, 26, 12, 0, 0), datetime(2026, 1, 28, 12, 0, 0))

    # ACT / ASSERT
    assert dsd.tap_date_filter(bounds) == {
        "exp_start": "between '2026-01-26 11:00:00' and '2026-01-28 13:00:00'"
    }


def test_tap_date_filter_for_open_start():
    # ARRANGE
    bounds = (None, datetime(2026, 1, 28, 12, 0, 0))

    # ACT / ASSERT
    assert dsd.tap_date_filter(bounds) == {"exp_start": "< '2026-01-28 13:00:00'"}


def test_tap_date_filter_for_open_end():
    # ARRANGE
    bounds = (datetime(2026, 1, 26, 12, 0, 0), None)

    # ACT / ASSERT
    assert dsd.tap_date_filter(bounds) == {"exp_start": ">= '2026-01-26 11:00:00'"}


def test_tap_date_filter_for_no_bounds():
    assert dsd.tap_date_filter((None, None)) == {}


def test_tap_date_filter_keeps_a_frame_whose_exp_start_is_just_before_the_night():
    # ARRANGE
    # DATE_OBS 12:00:01 IS INSIDE NIGHT 2026-01-27, BUT EXP_START 11:59:59 IS BEFORE ITS NOON
    expStart = datetime(2026, 1, 27, 11, 59, 59)
    bounds = dsd.night_bounds(date(2026, 1, 27), date(2026, 1, 27))

    # ACT
    lowerBound = dsd.tap_date_filter(bounds)["exp_start"].split("'")[1]

    # ASSERT
    assert datetime.fromisoformat(lowerBound) < expStart


@pytest.mark.parametrize("value", ["2026-13-01", "20260127", "2026-W05-2", "", None, 20260127])
def test_parse_night_raises_value_error_for_anything_but_yyyy_mm_dd(value):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        dsd.parse_night(value)


def test_parse_night_returns_the_date_for_a_valid_night():
    assert dsd.parse_night("2026-01-27") == date(2026, 1, 27)


def test_filter_to_nights_returns_table_unchanged_when_no_range_given():
    # ARRANGE
    table = make_archive_table([("SOXS.A", "CALIB", "2026-01-27T09:00:00")])

    # ACT
    filtered = dsd.filter_to_nights(table, None, None)

    # ASSERT
    assert list(filtered["dp_id"]) == ["SOXS.A"]


def test_filter_to_nights_drops_a_row_just_outside_the_range():
    # ARRANGE
    table = make_archive_table(
        [
            ("SOXS.A", "CALIB", "2026-01-26T11:59:59.999"),
            ("SOXS.B", "CALIB", "2026-01-26T12:00:00.000"),
        ]
    )

    # ACT
    filtered = dsd.filter_to_nights(table, date(2026, 1, 26), date(2026, 1, 27))

    # ASSERT
    assert list(filtered["dp_id"]) == ["SOXS.B"]


def test_filter_to_nights_drops_a_row_at_the_noon_after_the_end_night():
    # ARRANGE
    table = make_archive_table(
        [
            ("SOXS.A", "CALIB", "2026-01-28T11:59:59.999"),
            ("SOXS.B", "CALIB", "2026-01-28T12:00:00.000"),
        ]
    )

    # ACT
    filtered = dsd.filter_to_nights(table, date(2026, 1, 26), date(2026, 1, 27))

    # ASSERT
    assert list(filtered["dp_id"]) == ["SOXS.A"]


def test_main_passes_column_filters_for_a_night_range(tmp_path):
    # ARRANGE
    eso = make_eso(make_archive_table([]))

    # ACT
    run_main(eso, tmp_path, "--start-night", "2026-01-26", "--end-night", "2026-01-27")

    # ASSERT
    eso.query_main.assert_called_once_with(
        "SOXS",
        columns=["dp_id", "dp_cat", "date_obs"],
        authenticated=True,
        column_filters={"exp_start": "between '2026-01-26 11:00:00' and '2026-01-28 13:00:00'"},
    )


def test_main_handles_query_main_returning_none(tmp_path):
    # ARRANGE
    eso = make_eso(None)

    # ACT
    status = run_main(eso, tmp_path)

    # ASSERT
    eso.retrieve_data.assert_not_called()
    assert status == 0


# ---------------------------------------------------------------- FRAME CATEGORIES


def test_filter_categories_keeps_only_the_listed_categories():
    # ARRANGE
    table = make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T10:00:00"),
        ("SOXS.C", "ACQUISITION", "2026-01-27T11:00:00"),
    ])

    # ACT
    filtered = dsd.filter_categories(table, ("SCIENCE", "ACQUISITION"))

    # ASSERT
    assert list(filtered["dp_id"]) == ["SOXS.B", "SOXS.C"]


def test_filter_categories_returns_an_empty_table_when_nothing_matches():
    # ARRANGE
    table = make_archive_table([("SOXS.A", "CALIB", "2026-01-27T09:00:00")])

    # ACT
    filtered = dsd.filter_categories(table, ("SCIENCE",))

    # ASSERT
    assert len(filtered) == 0


def test_filter_categories_accepts_an_empty_table():
    assert len(dsd.filter_categories(make_archive_table([]), ("SCIENCE",))) == 0


def test_query_archive_applies_the_category_filter_after_the_night_filter(log):
    # ARRANGE
    eso = make_eso(make_archive_table([
        ("SOXS.A", "SCIENCE", "2026-01-26T20:00:00"),
        ("SOXS.B", "CALIB", "2026-01-26T21:00:00"),
        ("SOXS.C", "SCIENCE", "2026-01-28T20:00:00"),
    ]))

    # ACT
    table = dsd.query_archive(eso, date(2026, 1, 26), date(2026, 1, 26), ("SCIENCE",), log)

    # ASSERT
    assert list(table["dp_id"]) == ["SOXS.A"]


def test_dry_run_lists_only_the_requested_categories(tmp_path, capsys):
    # ARRANGE
    eso = make_eso(make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T20:00:00"),
    ]))

    # ACT
    run_main(eso, tmp_path, "--dry-run", "--category", "calib")

    # ASSERT
    out = capsys.readouterr().out
    assert "SOXS.A\tCALIB" in out
    assert "SOXS.B" not in out


def test_no_category_setting_keeps_every_archive_category(tmp_path, capsys):
    # ARRANGE
    eso = make_eso(make_archive_table([
        ("SOXS.A", "TECHNICAL", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T20:00:00"),
    ]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    out = capsys.readouterr().out
    assert "SOXS.A\tTECHNICAL" in out
    assert "SOXS.B\tSCIENCE" in out


def test_filter_categories_keeps_every_row_when_categories_is_none():
    # ARRANGE
    table = make_archive_table([
        ("SOXS.A", "TECHNICAL", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T10:00:00"),
    ])

    # ACT
    filtered = dsd.filter_categories(table, None)

    # ASSERT
    assert list(filtered["dp_id"]) == ["SOXS.A", "SOXS.B"]


@pytest.mark.parametrize(
    "category", ["SCIENCE", "CALIB", "ACQUISITION", "TECHNICAL", "TEST", "SIMULATION", "OTHER"]
)
def test_every_eso_raw_category_is_accepted_from_the_command_line(tmp_path, category):
    # ARRANGE
    arguments = cli("--user", "dave", "--data-dir", str(tmp_path), "--category", category.lower())

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.categories == (category,)


def test_no_category_flag_and_no_setting_gives_no_category_filter(tmp_path):
    # ACT
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path)))

    # ASSERT
    assert options.categories is None


# ---------------------------------------------------------------- RETRY POLICY


def test_main_passes_the_retry_settings_to_the_download(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "MAX_DOWNLOAD_ATTEMPTS", 2)
    monkeypatch.setattr(dsd, "RETRY_DELAY_STEP_SECONDS", 4)
    eso = make_eso(make_archive_table([("SOXS.A", "CALIB", "2026-01-26T20:00:00")]))
    eso.retrieve_data.side_effect = dsd.requests.exceptions.ConnectionError("reset")

    # ACT
    with mock.patch.object(dsd.time, "sleep") as sleep:
        status = run_main(eso, tmp_path)

    # ASSERT
    assert eso.retrieve_data.call_count == 2
    sleep.assert_called_once_with(4)
    assert status == 1


# ---------------------------------------------------------------- OPTIONS: PRECEDENCE


def test_options_are_built_from_the_command_line(tmp_path):
    # ARRANGE
    arguments = cli(
        "--user", "dave", "--data-dir", str(tmp_path), "--category", "science",
        "--start-night", "2026-01-01", "--end-night", "2026-01-31", "--dry-run", "--reenter-password", "--unzip",
    )

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options == dsd.Options(
        user="dave",
        dataDir=tmp_path,
        categories=("SCIENCE",),
        startNight=date(2026, 1, 1),
        endNight=date(2026, 1, 31),
        dryRun=True,
        reenterPassword=True,
        storePassword=True,
        unzip=True,
        maxAttempts=5,
        retryDelayStep=30,
    )


def test_options_fall_back_to_the_settings_block(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "ESO_USERNAME", "settingsUser")
    monkeypatch.setattr(dsd, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dsd, "FRAME_CATEGORIES", ["CALIB"])
    monkeypatch.setattr(dsd, "DEFAULT_START_NIGHT", "2026-02-01")
    monkeypatch.setattr(dsd, "DEFAULT_END_NIGHT", "2026-02-28")
    monkeypatch.setattr(dsd, "STORE_PASSWORD", False)
    monkeypatch.setattr(dsd, "UNZIP_FRAMES", True)
    monkeypatch.setattr(dsd, "MAX_DOWNLOAD_ATTEMPTS", 3)
    monkeypatch.setattr(dsd, "RETRY_DELAY_STEP_SECONDS", 10)

    # ACT
    options = dsd.resolve_options(cli())

    # ASSERT
    assert options == dsd.Options(
        user="settingsUser",
        dataDir=tmp_path,
        categories=("CALIB",),
        startNight=date(2026, 2, 1),
        endNight=date(2026, 2, 28),
        dryRun=False,
        reenterPassword=False,
        storePassword=False,
        unzip=True,
        maxAttempts=3,
        retryDelayStep=10,
    )


def test_command_line_flags_override_the_settings_block(tmp_path, monkeypatch):
    # ARRANGE
    settingsDir = tmp_path / "settings"
    cliDir = tmp_path / "cli"
    settingsDir.mkdir()
    cliDir.mkdir()
    monkeypatch.setattr(dsd, "ESO_USERNAME", "settingsUser")
    monkeypatch.setattr(dsd, "DATA_DIR", str(settingsDir))
    monkeypatch.setattr(dsd, "FRAME_CATEGORIES", ["CALIB"])
    monkeypatch.setattr(dsd, "DEFAULT_START_NIGHT", "2026-02-01")
    monkeypatch.setattr(dsd, "DEFAULT_END_NIGHT", "2026-02-28")

    # ACT
    options = dsd.resolve_options(cli(
        "--user", "cliUser", "--data-dir", str(cliDir), "--category", "SCIENCE",
        "--start-night", "2026-03-01", "--end-night", "2026-03-31",
    ))

    # ASSERT
    assert (options.user, options.dataDir, options.categories) == ("cliUser", cliDir, ("SCIENCE",))
    assert (options.startNight, options.endNight) == (date(2026, 3, 1), date(2026, 3, 31))


def test_repeated_and_mixed_case_categories_are_normalised_and_deduplicated(tmp_path):
    # ARRANGE
    arguments = cli(
        "--user", "dave", "--data-dir", str(tmp_path),
        "--category", "calib", "--category=CALIB", "--category", "Science",
    )

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.categories == ("CALIB", "SCIENCE")


def test_settings_categories_are_accepted_in_any_case(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "FRAME_CATEGORIES", ["science", "Calib"])

    # ACT
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path)))

    # ASSERT
    assert options.categories == ("SCIENCE", "CALIB")


def test_options_are_immutable(tmp_path):
    # ARRANGE
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path)))

    # ACT / ASSERT
    with pytest.raises(FrozenInstanceError):
        options.user = "someoneElse"


# ---------------------------------------------------------------- OPTIONS: VALIDATION


@pytest.mark.parametrize("settingValue", [None, "", "   "])
def test_missing_username_names_the_flag_and_the_setting(tmp_path, monkeypatch, settingValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "ESO_USERNAME", settingValue)

    # ACT
    message = exit_message("--data-dir", str(tmp_path))

    # ASSERT
    assert "--user" in message
    assert "ESO_USERNAME" in message


def test_username_setting_that_is_not_text_is_rejected(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "ESO_USERNAME", 12345)

    # ACT
    message = exit_message("--data-dir", str(tmp_path))

    # ASSERT
    assert "ESO_USERNAME" in message


def test_data_dir_setting_that_is_not_a_path_is_rejected(monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "DATA_DIR", 12345)

    # ACT
    message = exit_message("--user", "dave")

    # ASSERT
    assert "DATA_DIR" in message


def test_explicitly_empty_user_flag_is_an_error_not_a_fallback_to_the_setting(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "ESO_USERNAME", "settingsUser")

    # ACT
    message = exit_message("--user=", "--data-dir", str(tmp_path))

    # ASSERT
    assert "--user" in message
    assert "empty" in message


def test_explicitly_empty_data_dir_flag_is_an_error_not_a_fallback_to_the_setting(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "DATA_DIR", str(tmp_path))

    # ACT
    message = exit_message("--user", "dave", "--data-dir=")

    # ASSERT
    assert "--data-dir" in message
    assert "empty" in message


@pytest.mark.parametrize("settingValue", [None, ""])
def test_missing_data_dir_names_the_flag_and_the_setting(monkeypatch, settingValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "DATA_DIR", settingValue)

    # ACT
    message = exit_message("--user", "dave")

    # ASSERT
    assert "--data-dir" in message
    assert "DATA_DIR" in message


def test_data_dir_from_the_command_line_must_exist(tmp_path):
    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path / "gone"))

    # ASSERT
    assert "is the volume mounted?" in message
    assert "--data-dir" in message


def test_data_dir_from_the_settings_block_must_exist(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "DATA_DIR", str(tmp_path / "gone"))

    # ACT
    message = exit_message("--user", "dave")

    # ASSERT
    assert "is the volume mounted?" in message
    assert "DATA_DIR" in message


def test_tilde_in_the_data_dir_flag_is_expanded(tmp_path, monkeypatch):
    # ARRANGE
    (tmp_path / "raw").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    # ACT
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", "~/raw"))

    # ASSERT
    assert options.dataDir == tmp_path / "raw"


def test_tilde_in_the_data_dir_setting_is_expanded(tmp_path, monkeypatch):
    # ARRANGE
    (tmp_path / "raw").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(dsd, "DATA_DIR", "~/raw")

    # ACT
    options = dsd.resolve_options(cli("--user", "dave"))

    # ASSERT
    assert options.dataDir == tmp_path / "raw"


def test_data_dir_that_is_a_file_is_rejected(tmp_path):
    # ARRANGE
    aFile = tmp_path / "notes.txt"
    aFile.touch()

    # ACT / ASSERT
    assert "is the volume mounted?" in exit_message("--user", "dave", "--data-dir", str(aFile))


def test_unknown_category_from_the_command_line_is_rejected(tmp_path):
    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path), "--category", "BIAS")

    # ASSERT
    assert "BIAS" in message
    assert "--category" in message
    assert "SCIENCE" in message


@pytest.mark.parametrize("settingValue", [[], (), "SCIENCE", ["SCIENCE", "BIAS"], ["SCIENCE", None]])
def test_invalid_settings_categories_are_rejected(tmp_path, monkeypatch, settingValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "FRAME_CATEGORIES", settingValue)

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path))

    # ASSERT
    assert "FRAME_CATEGORIES" in message


def test_invalid_night_from_the_command_line_names_the_flag(tmp_path):
    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path), "--start-night", "2026-13-01")

    # ASSERT
    assert "--start-night" in message
    assert "YYYY-MM-DD" in message


@pytest.mark.parametrize("setting", ["DEFAULT_START_NIGHT", "DEFAULT_END_NIGHT"])
@pytest.mark.parametrize("badValue", ["27/01/2026", "20260127", date(2026, 1, 27)])
def test_invalid_night_in_the_settings_block_names_the_setting(tmp_path, monkeypatch, setting, badValue):
    # ARRANGE
    monkeypatch.setattr(dsd, setting, badValue)

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path))

    # ASSERT
    assert setting in message


def test_start_night_after_end_night_is_rejected(tmp_path):
    # ACT
    message = exit_message(
        "--user", "dave", "--data-dir", str(tmp_path),
        "--start-night", "2026-01-27", "--end-night", "2026-01-26",
    )

    # ASSERT
    assert "after" in message


def test_start_night_after_end_night_is_rejected_across_flag_and_settings(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "DEFAULT_END_NIGHT", "2026-01-01")

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path), "--start-night", "2026-01-27")

    # ASSERT
    assert "--start-night" in message
    assert "DEFAULT_END_NIGHT" in message


def test_equal_start_and_end_night_is_accepted(tmp_path):
    # ARRANGE
    arguments = cli(
        "--user", "dave", "--data-dir", str(tmp_path),
        "--start-night", "2026-01-27", "--end-night", "2026-01-27",
    )

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.startNight == options.endNight == date(2026, 1, 27)


@pytest.mark.parametrize("badValue", [0, -1, 2.5, "5", True, None])
def test_max_download_attempts_must_be_an_integer_of_at_least_one(tmp_path, monkeypatch, badValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "MAX_DOWNLOAD_ATTEMPTS", badValue)

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path))

    # ASSERT
    assert "MAX_DOWNLOAD_ATTEMPTS" in message


@pytest.mark.parametrize("badValue", [-1, -0.5, "30", True, None, float("inf"), float("-inf"), float("nan")])
def test_retry_delay_step_must_be_a_number_of_at_least_zero(tmp_path, monkeypatch, badValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "RETRY_DELAY_STEP_SECONDS", badValue)

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path))

    # ASSERT
    assert "RETRY_DELAY_STEP_SECONDS" in message


@pytest.mark.parametrize("goodValue", [0, 0.5, 30])
def test_retry_delay_step_accepts_zero_and_positive_numbers(tmp_path, monkeypatch, goodValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "RETRY_DELAY_STEP_SECONDS", goodValue)

    # ACT
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path)))

    # ASSERT
    assert options.retryDelayStep == goodValue


@pytest.mark.parametrize("badValue", ["yes", 1, 0, None])
def test_store_password_must_be_a_bool(tmp_path, monkeypatch, badValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "STORE_PASSWORD", badValue)

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path))

    # ASSERT
    assert "STORE_PASSWORD" in message


def test_frames_stay_compressed_by_default(tmp_path):
    # ACT
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path)))

    # ASSERT
    assert options.unzip is False


def test_unzip_flag_turns_unzipping_on(tmp_path):
    # ACT
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path), "--unzip"))

    # ASSERT
    assert options.unzip is True


@pytest.mark.parametrize("badValue", ["yes", 1, 0, None])
def test_unzip_frames_must_be_a_bool(tmp_path, monkeypatch, badValue):
    # ARRANGE
    monkeypatch.setattr(dsd, "UNZIP_FRAMES", badValue)

    # ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path))

    # ASSERT
    assert "UNZIP_FRAMES" in message


def test_main_passes_the_unzip_flag_through_to_the_download(tmp_path):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    run_main(eso, tmp_path, "--unzip")

    # ASSERT
    eso.retrieve_data.assert_called_once_with(["SOXS.B"], destination=str(tmp_path / "2026-01-27"), unzip=True)


# ---------------------------------------------------------------- LOG DESTINATION AND RESCAN


@pytest.fixture
def fresh_fundamentals_logger():
    """Forget fundamentals' logger, which it disables itself on a second setup in one process."""
    logging.Logger.manager.loggerDict.pop("fundamentals.logs", None)


def test_dry_run_stdout_holds_only_the_listing_and_log_lines_go_to_stderr(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    eso = make_eso(make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T20:00:00"),
    ]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    captured = capsys.readouterr()
    outLines = captured.out.splitlines()
    assert len(outLines) == 2
    assert all(re.fullmatch(r"SOXS\.\w+\t\w+\t\d{4}-\d{2}-\d{2}", line) for line in outLines)
    assert "Archive holds 2 frames" in captured.err


def test_sync_archive_returns_error_when_the_rescan_after_download_fails(tmp_path, log):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))
    options = dsd.resolve_options(cli("--user", "dave", "--data-dir", str(tmp_path)))

    # ACT
    with mock.patch.object(dsd, "index_local_frames", side_effect=[set(), PermissionError("denied")]):
        status = dsd.sync_archive(eso, options, log)

    # ASSERT
    eso.retrieve_data.assert_called_once()
    assert status == 1
    log.error.assert_called()


# ---------------------------------------------------------------- LAST DAYS


@pytest.fixture
def currentNight(monkeypatch):
    """Pin the current night to 2026-01-27."""
    monkeypatch.setattr(dsd, "current_night", lambda: date(2026, 1, 27))


def make_frozen_datetime(now):
    """Return a ``datetime`` subclass whose ``now`` always gives ``now`` in the requested timezone."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.replace(tzinfo=tz)

    return FrozenDatetime


def test_last_days_starts_n_minus_one_nights_before_the_current_night(tmp_path, currentNight):
    # ARRANGE
    arguments = cli("--user", "dave", "--data-dir", str(tmp_path), "--last-days", "3")

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.startNight == date(2026, 1, 25)
    assert options.endNight is None


def test_last_days_of_one_is_the_current_night_only(tmp_path, currentNight):
    # ARRANGE
    arguments = cli("--user", "dave", "--data-dir", str(tmp_path), "--last-days=1")

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.startNight == date(2026, 1, 27)
    assert options.endNight is None


def test_last_days_counts_back_across_a_year_boundary(tmp_path, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "current_night", lambda: date(2026, 1, 2))
    arguments = cli("--user", "dave", "--data-dir", str(tmp_path), "--last-days", "5")

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.startNight == date(2025, 12, 29)


@pytest.mark.parametrize("nightFlag", ["--start-night", "--end-night"])
def test_last_days_cannot_be_combined_with_a_night_flag(tmp_path, currentNight, nightFlag):
    # ARRANGE / ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path), "--last-days", "3", nightFlag, "2026-01-01")

    # ASSERT
    assert message == "--last-days cannot be combined with --start-night or --end-night"


@pytest.mark.parametrize("value", ["0", "-2", "abc", "1.5", "", "36501", "99999999999"])
def test_last_days_rejects_a_value_that_is_not_a_whole_number_in_range(tmp_path, currentNight, value):
    # ARRANGE / ACT
    message = exit_message("--user", "dave", "--data-dir", str(tmp_path), f"--last-days={value}")

    # ASSERT
    assert message.startswith("--last-days: not a whole number of days between 1 and 36500")
    assert value in message


def test_last_days_accepts_the_maximum_number_of_days(tmp_path, currentNight):
    # ARRANGE
    arguments = cli("--user", "dave", "--data-dir", str(tmp_path), "--last-days", "36500")

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert options.startNight == date(2026, 1, 27) - timedelta(days=36499)


def test_last_days_overrides_the_night_settings(tmp_path, currentNight, monkeypatch):
    # ARRANGE
    monkeypatch.setattr(dsd, "DEFAULT_START_NIGHT", "2026-02-01")
    monkeypatch.setattr(dsd, "DEFAULT_END_NIGHT", "2026-02-28")
    arguments = cli("--user", "dave", "--data-dir", str(tmp_path), "--last-days", "2")

    # ACT
    options = dsd.resolve_options(arguments)

    # ASSERT
    assert (options.startNight, options.endNight) == (date(2026, 1, 26), None)


def test_current_night_is_a_date():
    # ACT
    night = dsd.current_night()

    # ASSERT
    assert type(night) is date


@pytest.mark.parametrize(
    ("utNow", "expected"),
    [
        (datetime(2026, 1, 27, 11, 59, 59), date(2026, 1, 26)),
        (datetime(2026, 1, 27, 12, 0, 0), date(2026, 1, 27)),
    ],
)
def test_current_night_rolls_over_at_noon_ut(monkeypatch, utNow, expected):
    # ARRANGE
    monkeypatch.setattr(dsd, "datetime", make_frozen_datetime(utNow))

    # ACT
    night = dsd.current_night()

    # ASSERT
    assert night == expected


def test_main_queries_an_open_ended_window_for_last_days(tmp_path, currentNight):
    # ARRANGE
    eso = make_eso(make_archive_table([]))

    # ACT
    run_main(eso, tmp_path, "--last-days", "2")

    # ASSERT
    eso.query_main.assert_called_once_with(
        "SOXS",
        columns=["dp_id", "dp_cat", "date_obs"],
        authenticated=True,
        column_filters={"exp_start": ">= '2026-01-26 11:00:00'"},
    )


# ---------------------------------------------------------------- COMMAND LINE (DOCOPT)


def test_help_flag_prints_the_usage_text_and_exits(monkeypatch, capsys):
    # ARRANGE
    monkeypatch.setattr(sys, "argv", ["soxs-data-downloader.py", "--help"])

    # ACT
    with pytest.raises(SystemExit) as excinfo:
        dsd.main()

    # ASSERT
    assert excinfo.value.code in (0, None)
    assert "Usage:" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--bogus"], ["--start-night"], ["--user"], ["--last-days"]])
def test_unknown_or_incomplete_flags_are_rejected_by_docopt(monkeypatch, flags):
    # ARRANGE
    monkeypatch.setattr(sys, "argv", ["soxs-data-downloader.py", *flags])

    # ACT / ASSERT
    with pytest.raises(SystemExit) as excinfo:
        dsd.main()
    assert "Usage:" in str(excinfo.value.code)


def test_main_reads_the_real_command_line_when_no_arguments_are_passed(tmp_path, monkeypatch, capsys):
    # ARRANGE
    monkeypatch.setattr(
        sys, "argv",
        ["soxs-data-downloader.py", "--user", "dave", "--data-dir", str(tmp_path), "--dry-run",
         "--category", "science", "--category", "calib"],
    )
    eso = make_eso(make_archive_table([("SOXS.A", "CALIB", "2026-01-27T20:00:00")]))

    # ACT
    with mock.patch.object(dsd, "Eso", return_value=eso):
        status = dsd.main()

    # ASSERT
    assert status == 0
    assert "SOXS.A\tCALIB\t2026-01-27" in capsys.readouterr().out


def test_main_writes_no_settings_or_log_files(tmp_path, monkeypatch):
    # ARRANGE
    workDir = tmp_path / "work"
    homeDir = tmp_path / "home"
    dataDir = tmp_path / "data"
    for folder in (workDir, homeDir, dataDir):
        folder.mkdir()
    monkeypatch.chdir(workDir)
    monkeypatch.setenv("HOME", str(homeDir))
    eso = make_eso(make_archive_table([]))

    # ACT
    run_main(eso, dataDir, "--dry-run")

    # ASSERT
    assert list(workDir.iterdir()) == []
    assert list(homeDir.iterdir()) == []
    assert list(dataDir.iterdir()) == []


def test_script_run_directly_prints_the_help_text():
    # ARRANGE / ACT
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"], capture_output=True, text=True, check=False, timeout=60
    )

    # ASSERT
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--data-dir=<path>" in result.stdout


def test_main_reports_archive_disk_and_missing_counts_at_the_start(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    (tmp_path / "SOXS.A.fits").touch()
    eso = make_eso(make_archive_table([
        ("SOXS.A", "CALIB", "2026-01-27T09:00:00"),
        ("SOXS.B", "SCIENCE", "2026-01-27T20:00:00"),
    ]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    errLines = capsys.readouterr().err.splitlines()
    assert "Archive holds 2 frames (CALIB=1, SCIENCE=1)" in errLines
    statusRows = [line.split() for line in errLines if line.startswith(("Category", "CALIB", "SCIENCE", "Total"))]
    assert statusRows == [
        ["Category", "Archive", "Share", "On", "disk", "Missing", "Downloaded", "Progress"],
        ["CALIB", "1", "50.0%", "1", "0", "100.0%", "█" * 20],
        ["SCIENCE", "1", "50.0%", "0", "1", "0.0%"],
        ["Total", "2", "100.0%", "1", "1", "50.0%", "█" * 10],
    ]


def test_main_colours_the_status_table_when_colour_is_wanted(tmp_path, capsys, monkeypatch, fresh_fundamentals_logger):
    # ARRANGE
    monkeypatch.setattr(dsd, "use_colour", lambda stream: True)
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    assert dsd.ANSI_RED in capsys.readouterr().err


def test_main_writes_no_escape_codes_when_stderr_is_not_a_terminal(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    assert "\x1b" not in capsys.readouterr().err


def test_main_hides_info_log_lines_by_default(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    assert "Indexing local frames" not in capsys.readouterr().err


def test_main_shows_info_log_lines_when_log_level_is_info(tmp_path, capsys, monkeypatch, fresh_fundamentals_logger):
    # ARRANGE
    monkeypatch.setattr(dsd, "LOG_LEVEL", "info")
    eso = make_eso(make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    assert "Indexing local frames" in capsys.readouterr().err


def test_main_sets_the_astroquery_logger_to_the_chosen_level(tmp_path, fresh_fundamentals_logger):
    # ARRANGE
    dsd.astroqueryLog.setLevel("INFO")
    eso = make_eso(make_archive_table([]))

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    assert dsd.astroqueryLog.getEffectiveLevel() == logging.WARNING


@pytest.mark.parametrize("value", ["LOUD", "", 20, None])
def test_main_rejects_an_invalid_log_level_setting(tmp_path, monkeypatch, value):
    # ARRANGE
    monkeypatch.setattr(dsd, "LOG_LEVEL", value)

    # ACT / ASSERT
    with mock.patch.object(dsd, "Eso") as esoClass, pytest.raises(SystemExit, match="LOG_LEVEL"):
        dsd.main(cli("--data-dir", str(tmp_path), "--user", "dave"))
    esoClass.assert_not_called()


def test_main_reports_an_empty_archive_and_downloads_nothing(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    eso = make_eso(make_archive_table([]))

    # ACT
    status = run_main(eso, tmp_path)

    # ASSERT
    err = capsys.readouterr().err
    assert "Archive holds 0 frames" in err
    assert "Category" not in err
    eso.retrieve_data.assert_not_called()
    assert status == 0


def test_main_shows_each_astroquery_warning_once_on_stderr(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    eso = make_eso(make_archive_table([]))
    eso.query_main.side_effect = lambda *args, **kwargs: dsd.astroqueryLog.warning("Access denied to SOXS.A")

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    captured = capsys.readouterr()
    assert captured.err.count("Access denied to SOXS.A") == 1
    assert "Access denied" not in captured.out


def test_main_hides_astroquery_info_lines_by_default(tmp_path, capsys, fresh_fundamentals_logger):
    # ARRANGE
    eso = make_eso(make_archive_table([]))
    eso.query_main.side_effect = lambda *args, **kwargs: dsd.astroqueryLog.info("Downloading file 1/1")

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    captured = capsys.readouterr()
    assert "Downloading file" not in captured.err + captured.out


def test_dry_run_stdout_holds_only_the_listing_at_info_level(tmp_path, capsys, monkeypatch, fresh_fundamentals_logger):
    # ARRANGE
    monkeypatch.setattr(dsd, "LOG_LEVEL", "INFO")
    eso = make_eso()

    def log_and_return_table(*args, **kwargs):
        dsd.astroqueryLog.info("Authenticating dave on 'www.eso.org' ...")
        return make_archive_table([("SOXS.B", "SCIENCE", "2026-01-27T20:00:00")])

    eso.query_main.side_effect = log_and_return_table

    # ACT
    run_main(eso, tmp_path, "--dry-run")

    # ASSERT
    captured = capsys.readouterr()
    assert captured.out == "SOXS.B\tSCIENCE\t2026-01-27\n"
    assert "Authenticating dave" in captured.err
