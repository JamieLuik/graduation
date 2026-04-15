from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path


FIRST_PUCH_INDEX = 2
POUCH_MARKER = "=====NEXT POUCH"
STEP_PATTERN = re.compile(r"^Step\s+(\d+):\s+(.+?)\s*$")
LOG_DATETIME_PATTERN = re.compile(r"improved_bartest_log_(\d{8}_\d{6})", re.IGNORECASE)
VALID_BARCODE_PATTERN = re.compile(r"^\d{11}$")
SHUTDOWN_BARCODE_PATTERN = re.compile(r"^9\d{11}$")
LEADING_DIGITS_PATTERN = re.compile(r"^(\d+)")


def list_log_files(base_dir: Path) -> list[Path]:
    return sorted(
        [p for p in base_dir.glob("*.txt") if "log" in p.name.lower()]
    )


def parse_pouch(lines: list[str]) -> tuple[str, int, bool]:
    barcodes: list[str] = []
    try_count = 1
    previous_step: int | None = None

    for line in lines:
        match = STEP_PATTERN.match(line.strip())
        if match:
            step_number = int(match.group(1))
            value = match.group(2).strip()

            if previous_step is not None and step_number < previous_step:
                try_count += 1
            previous_step = step_number

            if value == "----":
                continue

            digits_match = LEADING_DIGITS_PATTERN.match(value)
            digits = digits_match.group(1) if digits_match else ""

            if SHUTDOWN_BARCODE_PATTERN.fullmatch(digits):
                return "", try_count, True
            if VALID_BARCODE_PATTERN.fullmatch(digits):
                barcodes.append(digits)
                continue
            barcodes.append(value)

    if not barcodes:
        return "", 3, False

    counts = Counter(barcodes)
    most_common_value, _ = counts.most_common(1)[0]
    return most_common_value, try_count, False


def analyze_log_file(path: Path) -> list[tuple[int, int, str]]:
    pouch_index = FIRST_PUCH_INDEX
    current_lines: list[str] = []
    results: list[tuple[int, int, str]] = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if POUCH_MARKER in line:
                barcode, try_count, should_stop = parse_pouch(current_lines)
                if should_stop:
                    return results
                results.append((pouch_index, try_count, barcode))
                pouch_index += 1
                current_lines = []
            else:
                current_lines.append(line)

    if current_lines or not results:
        barcode, try_count, should_stop = parse_pouch(current_lines)
        if not should_stop:
            results.append((pouch_index, try_count, barcode))

    return results


def write_analysis_file(results: list[tuple[int, int, str]], output_path: Path) -> None:
    try_labels = {
        1: "(T 1  )",
        2: "(T  2 )",
        3: "(T   3)",
    }
    lines = []
    pouch_count = len(results)
    barcode_count = sum(1 for _, _, barcode in results if barcode)
    try_found_counts = {
        1: 0,
        2: 0,
        3: 0,
    }
    for pouch_index, try_count, barcode in results:
        pouch_label = f"{pouch_index:02d}"
        display_try = try_count if barcode else 3
        try_label = try_labels.get(display_try, f"(T {display_try:>3})")
        if barcode and display_try in try_found_counts:
            try_found_counts[display_try] += 1
        if barcode:
            lines.append(f"Pouch {pouch_label} {try_label}: {barcode}")
        else:
            lines.append(f"Pouch {pouch_label} {try_label}: ----")

    read_percent = (barcode_count / pouch_count * 100.0) if pouch_count else 0.0
    lines.append(f"\nPouch Count:   {pouch_count}")
    lines.append(f"Barcode Count: {barcode_count}")
    lines.append(f"  Try 1:       {try_found_counts[1]}")
    lines.append(f"  Try 2:       {try_found_counts[2]}")
    lines.append(f"  Try 3:       {try_found_counts[3]}")
    lines.append(f"Read %:        {read_percent:.2f}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    base_dir = Path(os.getcwd())
    log_files = list_log_files(base_dir)
    if not log_files:
        raise FileNotFoundError("No .txt files containing 'log' found in the current folder.")

    written_files: list[str] = []
    for selected in log_files:
        results = analyze_log_file(selected)
        match = LOG_DATETIME_PATTERN.search(selected.name)
        if not match:
            print(f"Skipped {selected.name}: filename does not match expected datetime pattern.")
            continue

        output_path = base_dir / f"bartest_analysis_{match.group(1)}.txt"
        write_analysis_file(results, output_path)
        written_files.append(output_path.name)

    if written_files:
        print(f"Analysis complete. Wrote {len(written_files)} file(s).")
        for name in written_files:
            print(f"- {name}")
    else:
        print("No analysis files were written.")


if __name__ == "__main__":
    main()
