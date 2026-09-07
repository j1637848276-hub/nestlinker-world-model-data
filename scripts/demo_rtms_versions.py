"""Create a two-version synthetic observation demo under an unused directory."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worldmodel_data.observation import audit_rtms_versions, collect_rtms_observation


def response(days):
    items = "".join(
        "<item><umdNm>synthetic-dong</umdNm><dealYear>2026</dealYear>"
        f"<dealMonth>08</dealMonth><dealDay>{day}</dealDay>"
        "<deposit>1000</deposit><monthlyRent>50</monthlyRent></item>"
        for day in days
    )
    return (
        "<response><header><resultCode>000</resultCode></header><body><items>"
        f"{items}</items><totalCount>{len(days)}</totalCount></body></response>"
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = {
        "schema_version": 1,
        "display_timezone": "Asia/Seoul",
        "cadence_days": 7,
        "minimum_observation_months": 3,
        "target_observation_months": 6,
        "sources": ["apartment"],
        "lawd_codes": ["11620"],
        "contract_months": ["202608"],
        "rolling_contract_months": 1,
        "fixed_contract_months": [],
        "low_frequency_contract_months": [],
        "include_low_frequency": False,
        "delay_seconds": 0.0,
        "request_timeout_seconds": 5,
        "retry_attempts": 1,
        "retry_backoff_seconds": 0.0,
        "maximum_pages_per_partition": 5,
    }
    versions = {"synthetic-v1": ["14"], "synthetic-v2": ["14", "15"]}
    times = {
        "synthetic-v1": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "synthetic-v2": datetime(2026, 9, 8, tzinfo=timezone.utc),
    }
    for run_id, days in versions.items():
        collect_rtms_observation(
            config=config,
            storage_root=root / "storage",
            service_key="synthetic-key-never-sent",
            code_commit="0" * 40,
            observed_at=times[run_id],
            run_id=run_id,
            data_label="synthetic",
            requester=lambda url, timeout, days=days: response(days),
            sleep=lambda seconds: None,
        )
    report = audit_rtms_versions(
        storage_root=root / "storage",
        output_dir=root / "audit",
        cadence_days=7,
    )
    print(root / "audit" / "SUMMARY.md")
    return 0 if report["synthetic_run_count"] == 2 and report["real_observed_run_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
