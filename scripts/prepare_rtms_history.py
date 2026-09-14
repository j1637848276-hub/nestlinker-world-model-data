"""Prepare immutable yearly history-only configs; does not fetch or fabricate runs."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldmodel_data.observation import load_observation_config


def prepare(template, output_dir, start_year, end_year, last_month):
    if not 2011 <= start_year <= end_year <= date.today().year:
        raise ValueError("years must be ordered, start at 2011 or later, and not be future years")
    if not 1 <= last_month <= 12:
        raise ValueError("last_month must be between 1 and 12")
    base = json.loads(Path(template).read_text(encoding="utf-8"))
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError("output directory already exists; use a new plan directory")
    output.mkdir(parents=True)
    paths = []
    for year in range(start_year, end_year + 1):
        payload = dict(base)
        payload.update(rolling_contract_months=0, cadence_days=31,
                       low_frequency_contract_months=[],
                       fixed_contract_months=[f"{year}{month:02d}" for month in
                                              range(1, (last_month if year == end_year else 12) + 1)])
        path = output / f"rtms-history-{year}.json"
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        load_observation_config(path)
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", default="config/rtms-observation.example.json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--start-year", type=int, default=2011)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--last-month", type=int, default=9)
    args = parser.parse_args()
    for path in prepare(args.template, args.output_dir, args.start_year, args.end_year, args.last_month):
        print(path)


if __name__ == "__main__":
    main()
