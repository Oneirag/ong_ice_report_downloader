"""Convert tables in ICE report PDFs to CSV files.

Run without arguments to convert every PDF in ./downloads, or pass PDF files
and/or directories containing PDFs:

    python pdf_to_csv.py
    python pdf_to_csv.py downloads/0A1_2026-10-23.pdf
    python pdf_to_csv.py downloads

CSV files are written next to their source PDFs with the same base name.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import pdfplumber


def clean_cell(value: str | None) -> str:
    """Normalize whitespace and empty PDF table cells."""
    return re.sub(r"\s+", " ", value or "").strip()


def flatten_headers(
    group_row: list[str | None], label_row: list[str | None]
) -> list[str]:
    """Combine the report's grouped header and its column-label row."""
    headers = []
    group = ""
    for group_cell, label_cell in zip(group_row, label_row, strict=True):
        if clean_cell(group_cell):
            group = clean_cell(group_cell)
        label = clean_cell(label_cell)
        if not group and not label:
            raise ValueError("A PDF table column has no header.")
        if not label:
            header = group
        elif not group or group.casefold() == label.casefold():
            header = label
        else:
            header = f"{group} {label}"
        headers.append(header)

    if len(set(headers)) != len(headers):
        raise ValueError(f"The PDF table has ambiguous column names: {headers}")
    return headers


def extract_pdf_table(
    pdf_path: str | Path,
) -> tuple[list[str], list[list[str]]]:
    """Extract the ICE report's tabular headers and contract rows from a PDF."""
    pdf_path = Path(pdf_path)
    headers: list[str] | None = None
    rows: list[list[str]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if len(table) < 3:
                    continue
                table_headers = flatten_headers(table[0], table[1])
                if headers is None:
                    headers = table_headers
                elif table_headers != headers:
                    raise ValueError(
                        f"{pdf_path} contains tables with different column layouts; "
                        "convert those tables separately."
                    )

                for row in table[2:]:
                    cleaned_row = [clean_cell(cell) for cell in row]
                    if len(cleaned_row) != len(headers):
                        raise ValueError(
                            f"{pdf_path} contains a malformed table row: {row}"
                        )
                    # ICE inserts a commodity-name band and a totals row into
                    # the table. Keep per-contract rows only in the data CSV.
                    if any(cleaned_row) and len(cleaned_row) > 1 and cleaned_row[1]:
                        rows.append(cleaned_row)

    if headers is None:
        raise ValueError(f"No extractable table found in {pdf_path}.")
    return headers, rows


def convert_pdf_to_csv(
    pdf_path: str | Path, csv_path: str | Path | None = None
) -> Path:
    """Extract a PDF's table into a standalone CSV next to the source PDF."""
    pdf_path = Path(pdf_path)
    destination = (
        Path(csv_path)
        if csv_path is not None
        else pdf_path.with_suffix(".csv")
    )
    headers, rows = extract_pdf_table(pdf_path)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path("downloads")],
        help="PDF files or directories to scan for PDFs (default: downloads)",
    )
    args = parser.parse_args()

    pdf_paths: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            pdf_paths.extend(sorted(path.glob("*.pdf")))
        elif path.is_file() and path.suffix.lower() == ".pdf":
            pdf_paths.append(path)
        else:
            parser.error(f"Not a PDF file or directory: {path}")

    if not pdf_paths:
        parser.error("No PDF files found.")

    for pdf_path in pdf_paths:
        destination = convert_pdf_to_csv(pdf_path)
        print(f"Converted {pdf_path} -> {destination}")


if __name__ == "__main__":
    main()
