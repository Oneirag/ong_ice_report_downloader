# ICE End-of-Day Report Downloader

Download selected ICE End-of-Day reports, extract their PDF tables, and merge
the rows into one CSV. The downloader uses synchronous Python, a visible
Chromium browser for ICE's human CAPTCHA, and `requests` for the report API.

## Requirements

- Python 3.10 or newer
- Chromium (installed by Playwright)
- A valid ICE Report Center session and permission to access the requested data

Install the Python dependencies and browser from the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Configure reports

Edit [`config/reports_config.csv`](config/reports_config.csv). Add one row for
each selector option you want to download:

```csv
market,report,option_label
ICE Futures Europe,Futures,TFN-ICE Futures Europe Dutch TTF Natural Gas Futures
```

- `market` and `report` must match the labels shown on the ICE Report Center.
- `option_label` must match the visible label in that report's selector. Do not
  use the selector's internal code in this column.
- Add multiple rows to download multiple options and/or reports.

On each run, the script refreshes `config/report_catalog.csv` from the report
center and writes `config/report_options.csv` with the visible option labels
and their internal values for the configured report types. Use those files to
check spelling and copy exact labels into the configuration.

## Run

```bash
python download_ice_reports_requests.py
```

The script asks for a report date in `YYYY-MM-DD` format. Press Enter to use
the previous business day (Friday when the previous day is a weekend). For
each configured report, it opens the report in Chromium. If ICE presents a
CAPTCHA, solve it in the browser; the script waits up to five minutes for the
page's **I Accept** button to enable. Increase `CAPTCHA_TIMEOUT_SECONDS` in the
script to wait longer. CAPTCHA verification is not bypassed.

The Chromium profile is stored in `.ice-profile/` and reused on later runs.
ICE may still request another CAPTCHA. Keep the profile private; it contains
session cookies.

## Output

- PDFs: `downloads/`
- Merged CSV: `downloads/end_of_day_reports_<date>.csv`

The merged CSV starts with `report_date`, `market`, `report`, `option_label`,
and `report_id`, followed by the union of table columns from the requested
reports. Columns absent from a particular report are left blank. PDF table
heading bands and totals rows are omitted; contract-level rows are retained.
If the requested date is unavailable for an option, it is reported and
skipped.

To convert already-downloaded PDFs into individual CSVs instead:

```bash
python pdf_to_csv.py downloads
```

## Local files and Git

The `.gitignore` excludes virtual environments, downloads, the persistent
browser profile, generated report catalogs, and local OpenCode/Playwright MCP
files. OpenCode-related files remain on disk but are not intended to be added
to Git. The editable `config/reports_config.csv` is not ignored.
