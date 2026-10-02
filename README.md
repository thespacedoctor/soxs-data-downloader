# soxs-data-downloader

A command-line script that downloads SOXS raw frames from the ESO Science Archive. This is for SOXS team members who have an ESO User Portal account. Files already downloaded to your machine are recognised and not downloaded again.

## Install

1. Clone the repository.

   ```bash
   git clone https://github.com/thespacedoctor/soxs-data-downloader.git
   cd soxs-data-downloader
   ```

2. Install the dependencies. Use one of these two methods.

   With conda:

   ```bash
   conda env create -f environment.yml && conda activate soxs-data-downloader
   ```

   With pip:

   ```bash
   pip install -r requirements.txt
   ```

Alternatively, just [download the script](https://github.com/thespacedoctor/soxs-data-downloader/blob/main/soxs-data-downloader.py) and install it however you want!

## Configure

Open `soxs-data-downloader.py`. The user settings block is at the top of the file.

```python
ESO_USERNAME = None          # for example "your_eso_username"
DATA_DIR = None              # for example "/data/soxs/raw"
FRAME_CATEGORIES = ["SCIENCE", "CALIB", "ACQUISITION"]
DEFAULT_START_NIGHT = None
DEFAULT_END_NIGHT = None
STORE_PASSWORD = True
UNZIP_FRAMES = False         # True = unzip downloaded frames; --unzip turns this on for one run
MAX_DOWNLOAD_ATTEMPTS = 5
RETRY_DELAY_STEP_SECONDS = 30
LOG_LEVEL = "WARNING"        # "DEBUG", "INFO", "WARNING" or "ERROR"
```

A command-line flag always wins over the matching setting. The script checks every value in the same way, whether it comes from a flag or from the settings block.

| Setting | Command-line flag | Meaning |
| --- | --- | --- |
| `ESO_USERNAME` | `--user` | Your ESO User Portal username. No default. |
| `DATA_DIR` | `--data-dir` | The root folder of your local SOXS raw frames. The script expands `~` in the path. No default. |
| `FRAME_CATEGORIES` | `--category` | A list of frame categories, for example `["SCIENCE", "CALIB"]`. The default is `["SCIENCE", "CALIB", "ACQUISITION"]`. `None` means all categories. |
| `DEFAULT_START_NIGHT` | `--start-night` | First UT night to consider, as `"YYYY-MM-DD"`. `None` leaves the start open. |
| `DEFAULT_END_NIGHT` | `--end-night` | Last UT night to consider (inclusive), as `"YYYY-MM-DD"`. `None` leaves the end open. |
| `STORE_PASSWORD` | None | `True` keeps your ESO password in the system keyring. |
| `UNZIP_FRAMES` | `--unzip` | `True` unzips the downloaded frames. `False` keeps them compressed. The default is `False`. The flag can only turn unzipping on. |
| `MAX_DOWNLOAD_ATTEMPTS` | None | The number of tries in a row that download no frame before the script gives up on that night. A try that downloads frames before the connection drops starts the count again. With `1`, the script never retries. Must be a whole number of 1 or more. |
| `RETRY_DELAY_STEP_SECONDS` | None | The wait, in seconds, after the first failed try. The wait grows by this amount after each further failed try. Must be a finite number of 0 or more. |
| `LOG_LEVEL` | None | How much the script writes to the log. Use `"DEBUG"`, `"INFO"`, `"WARNING"`, or `"ERROR"`, in any case. The default is `"WARNING"`, which hides the other log lines. The frame counts and the progress bar always show. |

Notes:

- The valid categories are `SCIENCE`, `CALIB`, `ACQUISITION`, `TECHNICAL`, `TEST`, `SIMULATION`, and `OTHER`. The script accepts lowercase names and converts them to uppercase.

### Password

The script never stores your password in the script file. The first time you run it, astroquery asks you for the password. The system keyring keeps it, and later runs use the stored password. Set `STORE_PASSWORD = False` to turn this off.

**Caution:** On a headless Linux machine or a shared machine, the keyring backend can be insecure or missing. On such a machine, set `STORE_PASSWORD = False`. The script then asks for your password on each run.

## Usage

Activate the environment first. The script is executable, so you can start it with `./soxs-data-downloader.py` or with `python soxs-data-downloader.py`. The examples below use `python`.

Download the frames for a range of nights. Both ends are inclusive.

```bash
python soxs-data-downloader.py --start-night=2026-01-26 --end-night=2026-01-31
```

Download calibration frames only.

```bash
python soxs-data-downloader.py --category CALIB
```

Download more than one category. Repeat the flag.

```bash
python soxs-data-downloader.py --category SCIENCE --category CALIB
```

Replace a stored password that is wrong.

```bash
python soxs-data-downloader.py --reenter-password
```

Unzip the downloaded frames. They stay compressed (`.fits.Z`) by default.

```bash
python soxs-data-downloader.py --unzip
```

You can also start the script directly.

```bash
./soxs-data-downloader.py --category CALIB
```

These examples assume you set `ESO_USERNAME` and `DATA_DIR` in the settings block. Run `python soxs-data-downloader.py --help` to see all options.

## What the script shows

At the start of every run, the script writes a summary to stderr. It does this at any `LOG_LEVEL`.

```text
Querying the ESO archive for all SOXS raw frames
Archive holds 63335 frames (ACQUISITION=2029, CALIB=58514, SCIENCE=2792)
Already on disk: 60000 frames (ACQUISITION=2000, CALIB=56000, SCIENCE=2000), 94.7% downloaded
3335 frames are missing locally (ACQUISITION=29, CALIB=2514, SCIENCE=792)
```

- **Archive holds.** The frames in the archive that match your night range and categories.
- **Already on disk.** The part of those frames that you already have. Frames outside your night range or categories are not counted. The percentage is rounded down, so `100.0%` shows only when no frame is missing.
- **Missing locally.** The frames the script will download.

If the archive holds no matching frames, the script stops after the first two lines.

While the script downloads, a progress bar on stderr counts the downloaded frames and shows the current night. The script fetches the frames one at a time. The bar is hidden when stderr is not a terminal, for example when you run the script from cron or redirect its output to a file. When all frames arrive, the script ends with `All N missing frames downloaded`, where `N` is the number of frames it fetched.

## How frames are organised

- **Night folders.** The script puts each new frame in a folder named `YYYY-MM-DD` inside the data folder. The name is the UT date 12 hours before the observation. For example, a frame observed at `2026-01-27T11:59:59.999` UT goes in `2026-01-26`.
- **Present frames.** A frame counts as present if a `.fits`, `.fits.Z`, or `.fits.gz` file with its name exists anywhere under the data folder, in any subfolder. The script does not re-download it.
- **Retries.** If the connection drops during a night, the script waits and tries again, starting at the frame that dropped and skipping frames that are on disk. It gives up on the night after `MAX_DOWNLOAD_ATTEMPTS` tries in a row that download no frame; a try that downloads frames starts the count again. If a night still fails, the script logs the error and goes on to the next night.
- **Compressed frames.** ESO serves the frames as `.fits.Z` files, and the script keeps them in this form by default. To unzip them, pass `--unzip` or set `UNZIP_FRAMES = True`. The flag can only turn unzipping on. Unzipping uses the `gunzip` command on your system. If `gunzip` is not available, astroquery shows a warning and leaves the files compressed. The script does not download compressed frames again, because they count as present.

## Troubleshooting

### Login fails

The script exits with `ESO login failed`. Run it again with `--reenter-password`, and type your password again.

astroquery saves a password in the keyring before checking it. A wrong password therefore stays in the keyring and is used on every later run until you replace it. The script never deletes the stored password on its own. A wrong password also triggers an ESO server error, so the script cannot tell the two apart.

## Licence

This project is licensed under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file.
