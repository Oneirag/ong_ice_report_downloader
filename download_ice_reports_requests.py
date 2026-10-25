"""Download configured ICE End-of-Day reports and merge their tables to CSV.

Configuration is read from config/reports_config.csv. Its market and report
columns use the labels shown on ICE's Report Center page; option_label must
exactly match the visible option label in that report's selector.

Install dependencies and the browser once with:
    python -m pip install -r requirements.txt
    python -m playwright install chromium

The script opens a persistent, visible Chromium profile at .ice-profile. Solve
each CAPTCHA that ICE presents; the script waits up to CAPTCHA_TIMEOUT_SECONDS
for the page's I Accept button to enable, then uses requests for the report API.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from pdf_to_csv import extract_pdf_table


ORIGIN = "https://www.ice.com"
REPORT_CENTER_URL = f"{ORIGIN}/report-center"
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "reports_config.csv"
CATALOG_FILE = CONFIG_DIR / "report_catalog.csv"
OPTIONS_CATALOG_FILE = CONFIG_DIR / "report_options.csv"
DOWNLOAD_DIR = PROJECT_DIR / "downloads"
PROFILE_DIR = PROJECT_DIR / ".ice-profile"
CAPTCHA_TIMEOUT_SECONDS = 5 * 60  # Increase this value to allow more time.

METADATA_COLUMNS = ["report_date", "market", "report", "option_label", "report_id"]
CONFIG_FIELDS = ("market", "report", "option_label")

REPORT_CATALOG_JS = """() => [...document.querySelectorAll('.bg .true-grid-half')]
  .flatMap(column => {
    let market = '';
    const reports = [];
    for (const child of column.children) {
      if (child.matches('h5')) {
        market = child.textContent.trim();
      } else if (child.matches('.column-list')) {
        for (const link of child.querySelectorAll('a[href^="/report/"]')) {
          const match = link.getAttribute('href').match(/\\/report\\/(\\d+)/);
          if (match) reports.push({
            market,
            report: link.textContent.trim(),
            report_id: match[1],
          });
        }
      }
    }
    return reports;
  })"""


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")


def normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def previous_business_day(today: date | None = None) -> date:
    candidate = (today or date.today()) - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def prompt_report_date() -> date:
    default = previous_business_day()
    while True:
        value = input(f"Report date (YYYY-MM-DD) [{default.isoformat()}]: ").strip()
        if not value:
            return default
        try:
            return date.fromisoformat(value)
        except ValueError:
            print("Enter a valid date in YYYY-MM-DD format.")


def read_configuration() -> list[dict[str, str]]:
    if not CONFIG_FILE.is_file():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE}")

    with CONFIG_FILE.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = set(CONFIG_FIELDS)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(
                f"{CONFIG_FILE} must have these columns: {','.join(CONFIG_FIELDS)}"
            )
        rows = [
            {key: (row.get(key) or "").strip() for key in CONFIG_FIELDS}
            for row in reader
            if any((value or "").strip() for value in row.values())
        ]

    if not rows:
        raise ValueError(f"No report selections found in {CONFIG_FILE}.")
    for line_number, row in enumerate(rows, start=2):
        if not all(row.values()):
            raise ValueError(
                f"Configuration row {line_number} needs "
                "market, report, and option_label."
            )
    return rows


def save_report_catalog(catalog: list[dict[str, str]]) -> None:
    with CATALOG_FILE.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output, fieldnames=["market", "report", "report_id"]
        )
        writer.writeheader()
        writer.writerows(catalog)


def save_option_catalog(options: list[dict[str, str]]) -> None:
    with OPTIONS_CATALOG_FILE.open("w", newline="", encoding="utf-8") as output:
        fields = ["market", "report", "report_id", "option_label", "option_value"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(options)


def resolve_configuration(
    config_rows: list[dict[str, str]], catalog: list[dict[str, str]]
) -> dict[str, list[dict[str, str]]]:
    catalog_by_names = {
        (normalized(item["market"]), normalized(item["report"])): item
        for item in catalog
    }
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    group_info: dict[str, dict[str, str]] = {}

    for row in config_rows:
        key = (normalized(row["market"]), normalized(row["report"]))
        item = catalog_by_names.get(key)
        if item is None:
            raise ValueError(
                f"Report not found on ICE Report Center: "
                f"market={row['market']!r}, report={row['report']!r}. "
                f"See {CATALOG_FILE} for current labels."
            )
        report_id = item["report_id"]
        group_info[report_id] = {"market": item["market"], "report": item["report"]}
        grouped[report_id].append(row)

    # Preserve the report-catalog order from the configuration's first use.
    return {
        report_id: [{**group_info[report_id], **selection} for selection in selections]
        for report_id, selections in grouped.items()
    }


def copy_browser_cookies(context: BrowserContext, session: requests.Session) -> None:
    for cookie in context.cookies([ORIGIN]):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie["domain"],
            path=cookie.get("path", "/"),
        )


def dismiss_cookie_dialog(page: Page) -> None:
    cookie_dialog = page.get_by_role("alertdialog", name="Cookie Consent")
    if cookie_dialog.count() and cookie_dialog.is_visible():
        cookie_dialog.get_by_role(
            "button", name="Reject All Non-Essential"
        ).click()


def accept_terms_dialog(page: Page) -> None:
    terms_dialog = page.get_by_role("dialog")
    if terms_dialog.count() and terms_dialog.is_visible():
        terms_dialog.get_by_role("button", name="I Accept").click()


def authorize_report_in_browser(
    page: Page,
    context: BrowserContext,
    session: requests.Session,
    report_id: str,
) -> None:
    """Allow the user to solve any CAPTCHA for this report, then sync cookies."""
    report_url = f"{ORIGIN}/report/{report_id}"
    page.goto(report_url, wait_until="domcontentloaded")
    page.wait_for_function(
        """() => document.querySelector('main') && (
          document.querySelector('select#exchangeCodeAndContract') ||
          [...document.querySelectorAll('main button')].some(button =>
            button.textContent.trim() === 'I Accept' && button.getClientRects().length
          )
        )""",
        timeout=60_000,
    )

    dismiss_cookie_dialog(page)
    accept_terms_dialog(page)

    selector = page.locator("select#exchangeCodeAndContract")
    if selector.count() and selector.is_visible():
        copy_browser_cookies(context, session)
        return

    accept_button = page.locator("main").get_by_role(
        "button", name="I Accept", exact=True
    )
    accept_button.wait_for(state="visible", timeout=30_000)
    print(
        f"Solve the CAPTCHA for report {report_id} in the browser; waiting up to "
        f"{CAPTCHA_TIMEOUT_SECONDS // 60} minutes..."
    )
    try:
        page.wait_for_function(
            """() => {
                const button = [...document.querySelectorAll('main button')]
                    .find(item => item.textContent.trim() === 'I Accept'
                        && item.getClientRects().length);
                return button && !button.disabled;
            }""",
            timeout=CAPTCHA_TIMEOUT_SECONDS * 1000,
        )
    except PlaywrightTimeoutError as exc:
        raise TimeoutError(
            f"CAPTCHA timeout for report {report_id}; increase "
            "CAPTCHA_TIMEOUT_SECONDS to wait longer."
        ) from exc

    criteria_prefix = f"/marketdata/api/reports/{report_id}/criteria?"
    with page.expect_response(
        lambda response: criteria_prefix in response.url and response.status == 200,
        timeout=180_000,
    ):
        accept_button.click()

    copy_browser_cookies(context, session)


def checked_response(response: requests.Response, description: str) -> None:
    if response.status_code == 409:
        raise RuntimeError(
            f"{description} returned HTTP 409. ICE may require CAPTCHA validation "
            "for this report; complete it in the opened browser and retry."
        )
    response.raise_for_status()


def get_report_options(
    session: requests.Session, report_id: str
) -> list[dict[str, str]]:
    response = session.get(
        f"{ORIGIN}/marketdata/api/reports/{report_id}/criteria",
        params={"offset": 0, "pageNumber": 1},
        timeout=60,
    )
    checked_response(response, f"Criteria request for report {report_id}")
    criteria = response.json()
    selector = next(
        (item for item in criteria if item.get("name") == "exchangeCodeAndContract"),
        None,
    )
    if selector is None:
        raise ValueError(
            f"Report {report_id} has no exchangeCodeAndContract selector; "
            "this downloader expects an End-of-Day report with a contract selector."
        )
    return selector.get("values", [])


def fetch_report_table(
    session: requests.Session,
    report_id: str,
    selection: dict[str, str],
    option: dict[str, str],
    report_day: date,
    records: list[dict[str, str]],
) -> None:
    value = option["value"]
    response = session.post(
        f"{ORIGIN}/marketdata/api/reports/{report_id}/results",
        data={"exchangeCodeAndContract": value},
        timeout=60,
    )
    checked_response(response, f"Results request for {selection['option_label']}")
    payload = response.json()
    dates = payload.get("datasets", {}).get("contractDates", {}).get("rows", [])
    report_row = next(
        (row for row in dates if row.get("reportDate") == report_day.isoformat()),
        None,
    )
    if report_row is None:
        available = [row.get("reportDate") for row in dates]
        print(
            f"  No {report_day.isoformat()} report for {selection['option_label']}; "
            f"available dates: {available}"
        )
        return

    download = report_row.get("download", {})
    form_data = {
        item["name"]: item["value"] for item in download.get("params", [])
    }
    if not form_data or not download.get("url"):
        raise ValueError(f"No download parameters were supplied for {value}.")

    pdf_response = session.post(
        urljoin(ORIGIN, download["url"]),
        data=form_data,
        stream=True,
        timeout=120,
    )
    checked_response(pdf_response, f"PDF download for {selection['option_label']}")
    if "application/pdf" not in pdf_response.headers.get("Content-Type", "").lower():
        preview = next(pdf_response.iter_lines(), b"").decode(errors="replace")
        pdf_response.close()
        raise ValueError(f"Expected PDF data for {value}; response started: {preview}")

    option_code = value.split(",")[-1]
    filename = "_".join(
        safe_filename(part)
        for part in (report_id, option_code, report_day.isoformat())
    )
    pdf_path = DOWNLOAD_DIR / f"{filename}.pdf"
    with pdf_path.open("wb") as output:
        for chunk in pdf_response.iter_content(chunk_size=64 * 1024):
            if chunk:
                output.write(chunk)
    pdf_response.close()
    print(f"  Downloaded {pdf_path}")

    headers, rows = extract_pdf_table(pdf_path)
    metadata = {
        "report_date": report_day.isoformat(),
        "market": selection["market"],
        "report": selection["report"],
        "option_label": selection["option_label"],
        "report_id": report_id,
    }
    for row in rows:
        records.append({**metadata, **dict(zip(headers, row, strict=True))})
    print(f"  Extracted {len(rows)} table rows")


def write_merged_csv(records: list[dict[str, str]], report_day: date) -> Path:
    if not records:
        raise RuntimeError("No report rows were extracted; no merged CSV was written.")

    fieldnames = list(METADATA_COLUMNS)
    seen = set(fieldnames)
    for record in records:
        for name in record:
            if name not in seen:
                fieldnames.append(name)
                seen.add(name)

    destination = DOWNLOAD_DIR / f"end_of_day_reports_{report_day.isoformat()}.csv"
    with destination.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return destination


def main() -> None:
    config_rows = read_configuration()
    report_day = prompt_report_date()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, str]] = []
    session = requests.Session()
    session.headers.update({"Referer": REPORT_CENTER_URL, "Accept": "*/*"})

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        print(f"Using persistent browser profile: {PROFILE_DIR}")
        try:
            page.goto(REPORT_CENTER_URL, wait_until="domcontentloaded")
            page.locator(".bg .true-grid-half").first.wait_for(
                state="visible", timeout=60_000
            )
            dismiss_cookie_dialog(page)
            catalog = page.evaluate(REPORT_CATALOG_JS)
            if not catalog:
                raise RuntimeError("Could not read the End of day report catalog from ICE.")
            save_report_catalog(catalog)
            print(f"Found {len(catalog)} reports; catalog saved to {CATALOG_FILE}.")

            report_groups = resolve_configuration(config_rows, catalog)
            page.goto("about:blank")
            session.headers["User-Agent"] = page.evaluate("() => navigator.userAgent")
            option_catalog: list[dict[str, str]] = []

            for report_id, selections in report_groups.items():
                first = selections[0]
                print(
                    f"\n{first['market']} / {first['report']} "
                    f"(report {report_id})"
                )
                authorize_report_in_browser(page, context, session, report_id)
                options = get_report_options(session, report_id)
                option_catalog.extend(
                    {
                        "market": first["market"],
                        "report": first["report"],
                        "report_id": report_id,
                        "option_label": option["label"],
                        "option_value": option["value"],
                    }
                    for option in options
                )
                save_option_catalog(option_catalog)
                options_by_label = {normalized(item["label"]): item for item in options}

                for selection in selections:
                    option = options_by_label.get(normalized(selection["option_label"]))
                    if option is None:
                        raise ValueError(
                            f"Option label {selection['option_label']!r} was not found "
                            f"for {selection['market']} / {selection['report']}. "
                            "Use the exact visible label from the selector."
                        )
                    print(f"  {selection['option_label']}")
                    fetch_report_table(
                        session, report_id, selection, option, report_day, records
                    )

            merged_path = write_merged_csv(records, report_day)
            print(f"\nMerged {len(records)} rows into {merged_path}")
        finally:
            session.close()
            context.close()


if __name__ == "__main__":
    main()
