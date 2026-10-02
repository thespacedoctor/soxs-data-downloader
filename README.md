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

Alternatively, just download the script and install it however you want!

## Configure

Open `soxs-data-downloader.py`. The user settings block is at the top of the file.

```python
ESO_USERNAME = None          # for example "your_eso_username"
DATA_DIR = None              # for example "/data/soxs/raw"
FRAME_CATEGORIES = None
DEFAULT_START_NIGHT = None
DEFAULT_END_NIGHT = None
STORE_PASSWORD = True
MAX_DOWNLOAD_ATTEMPTS = 5
RETRY_DELAY_STEP_SECONDS = 30
```

A command-line flag always wins over the matching setting. The script checks every value in the same way, whether it comes from a flag or from the settings block.

| Setting | Command-line flag | Meaning |
| --- | --- | --- |
| `ESO_USERNAME` | `--user` | Your ESO User Portal username. No default. |
| `DATA_DIR` | `--data-dir` | The root folder of your local SOXS raw frames. The script expands `~` in the path. No default. |
| `FRAME_CATEGORIES` | `--category` | A list of frame categories, for example `["SCIENCE", "CALIB"]`. `None` means all categories. |
| `DEFAULT_START_NIGHT` | `--start-night` | First UT night to consider, as `"YYYY-MM-DD"`. `None` leaves the start open. |
| `DEFAULT_END_NIGHT` | `--end-night` | Last UT night to consider (inclusive), as `"YYYY-MM-DD"`. `None` leaves the end open. |
| `STORE_PASSWORD` | None | `True` keeps your ESO password in the system keyring. |
| `MAX_DOWNLOAD_ATTEMPTS` | None | The number of tries for each night before the script gives up on that night. Must be a whole number of 1 or more. |
| `RETRY_DELAY_STEP_SECONDS` | None | The wait, in seconds, after the first failed try. The wait grows by this amount after each further failed try. Must be a finite number of 0 or more. |

Notes:

- You must give a username and a data folder, either as a flag or as a setting. If you give neither, the script exits and tells you which flag or setting to use.
- The script has no built-in data folder. If you used the old `download_soxs_archive.py` script, note that it used a fixed default path. This script does not. Set `DATA_DIR` or pass `--data-dir`.
- A flag that you give with an empty value, for example `--user=`, is an error. The script does not use the setting in its place. Leave the flag out to use the setting.
- The valid categories are `SCIENCE`, `CALIB`, `ACQUISITION`, `TECHNICAL`, `TEST`, `SIMULATION`, and `OTHER`. The script accepts lower-case names and converts them to upper case.
- The start night must not be after the end night.

### Password

The script never stores your password in the script file. The first time you run it, astroquery asks you for the password. The system keyring keeps it, and later runs use the stored password. Set `STORE_PASSWORD = False` to turn this off.

**Caution:** On a headless Linux machine or a shared machine, the keyring backend can be insecure or missing. On such a machine, set `STORE_PASSWORD = False`. The script then asks for your password on each run.

## Usage

Activate the environment first. The script is executable, so you can start it with `./soxs-data-downloader.py` or with `python soxs-data-downloader.py`. The examples below use `python`.

Run a dry run first. It lists the missing frames and downloads nothing.

```bash
python soxs-data-downloader.py --user=your_eso_username --data-dir=/data/soxs/raw --dry-run
```

Each line of the list has the frame ID, the category, and the night folder, separated by tabs: `dp_id<TAB>dp_cat<TAB>night`. The script writes only these lines to stdout. It writes log lines to stderr. You can therefore save the list to a file.

```bash
python soxs-data-downloader.py --dry-run > missing.tsv
```

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

You can also start the script directly.

```bash
./soxs-data-downloader.py --category CALIB
```

These examples assume you set `ESO_USERNAME` and `DATA_DIR` in the settings block. Run `python soxs-data-downloader.py --help` to see all options.

## How frames are organized

- **Night folders.** The script puts each new frame in a folder named `YYYY-MM-DD` inside the data folder. The name is the UT date 12 hours before the observation. For example, a frame observed at `2026-01-27T11:59:59.999` UT goes in `2026-01-26`.
- **Present frames.** A frame counts as present if a `.fits`, `.fits.Z`, or `.fits.gz` file with its name exists anywhere under the data folder, in any subfolder. The script does not download it again.
- **Retries.** If the connection drops during a night, the script waits and tries again with only the frames that are not on disk. It makes up to `MAX_DOWNLOAD_ATTEMPTS` tries for each night. If a night still fails, the script logs the error and goes on to the next night.
- **Unzip.** The script asks astroquery to unzip the files it downloads.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The run finished. All missing frames arrived, no frames were missing, or you used `--dry-run`. |
| `1` | A frame did not arrive, the script could not read the local data folder, or the script stopped with an error message. Error messages include an invalid option or setting, an astroquery version that is too old, and a failed login. |

A second identical run that starts while the first is still running also exits with code `0`. See [Troubleshooting](#troubleshooting).

## Troubleshooting

### Login fails

The script exits with `ESO login failed`. Run it again with `--reenter-password`, and type your password again.

astroquery saves a password in the keyring before it checks the password. A wrong password therefore stays in the keyring and is used on every later run until you replace it. The script never deletes the stored password by itself. astroquery reports an ESO server error in the same way as a wrong password, so the script cannot tell the two apart.

### astroquery is too old

The script exits with `astroquery ... is too old; 0.4.12 or later is needed`. Install the pre-release.

```bash
pip install --pre -U "astroquery>=0.4.12.dev0"
```

### Data folder not found

The script exits with `data folder not found (is the volume mounted?)`. Check the path you gave in `--data-dir` or `DATA_DIR`. If the folder is on an external or network volume, mount the volume and run the script again.

### The script exits straight away

The script uses the `fundamentals` package. If you start a command while an identical command is already running, the new command prints `This command is already running (see PID ...)` and exits with code `0`. Wait for the first run to finish, or stop it.

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ --cov=. --cov-config=.coveragerc
```

## Continuous integration

The GitHub Actions workflow `.github/workflows/tests.yml` runs the tests on Python 3.12. It runs on each push to `main` and `develop`, and on each pull request. The run fails if test coverage is below 80 percent.

## Licence

This project is licensed under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file.
