import json
import os
import tempfile
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from worldmodel_data.observation import (
    _collector_lock,
    _full_calendar_months,
    audit_rtms_versions,
    collect_rtms_observation,
    load_observation_config,
    load_resume_config,
    observe_annual_archives,
)
from worldmodel_data.rtms import normalize_item


def xml(items, total=None):
    body = "".join(
        "<item>" + "".join(f"<{key}>{value}</{key}>" for key, value in item.items()) + "</item>"
        for item in items
    )
    total_value = len(items) if total is None else total
    return (
        "<?xml version='1.0' encoding='UTF-8'?><response>"
        "<header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>"
        f"<body><items>{body}</items><totalCount>{total_value}</totalCount></body></response>"
    )


def row(day="14", deposit="1000", rent="55", **extra):
    value = {
        "umdNm": "신림동",
        "dealYear": "2026",
        "dealMonth": "08",
        "dealDay": day,
        "deposit": deposit,
        "monthlyRent": rent,
    }
    value.update(extra)
    return value


def config(months=None):
    return {
        "schema_version": 1,
        "display_timezone": "Asia/Seoul",
        "cadence_days": 7,
        "minimum_observation_months": 3,
        "target_observation_months": 6,
        "sources": ["apartment"],
        "lawd_codes": ["11620"],
        "contract_months": months or ["202608"],
        "rolling_contract_months": 12,
        "fixed_contract_months": [],
        "low_frequency_contract_months": [],
        "include_low_frequency": False,
        "delay_seconds": 0.0,
        "request_timeout_seconds": 5,
        "retry_attempts": 2,
        "retry_backoff_seconds": 0.0,
        "maximum_pages_per_partition": 10,
    }


class ObservationTests(unittest.TestCase):
    def test_full_calendar_month_boundary_is_not_rounded_up(self):
        start = datetime(2026, 9, 7, 3, tzinfo=timezone.utc)
        self.assertEqual(_full_calendar_months(start, datetime(2026, 12, 6, 3, tzinfo=timezone.utc)), 2)
        self.assertEqual(_full_calendar_months(start, datetime(2026, 12, 7, 3, tzinfo=timezone.utc)), 3)

    def test_config_unions_rolling_fixed_and_optional_backfill(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            payload = {
                "schema_version": 1,
                "display_timezone": "Asia/Seoul",
                "cadence_days": 7,
                "minimum_observation_months": 3,
                "target_observation_months": 6,
                "rolling_contract_months": 2,
                "sources": ["apartment"],
                "lawd_codes": ["11620"],
                "fixed_contract_months": ["202401"],
                "low_frequency_contract_months": ["202301"],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            regular = load_observation_config(path, today=date(2026, 2, 3))
            backfill = load_observation_config(path, include_backfill=True, today=date(2026, 2, 3))
            self.assertEqual(regular["contract_months"], ["202602", "202601", "202401"])
            self.assertEqual(backfill["contract_months"], ["202602", "202601", "202401", "202301"])

    def test_missing_amount_is_not_normalized_as_zero(self):
        missing = row()
        missing.pop("monthlyRent")
        self.assertIsNone(normalize_item("apartment", missing, "11620"))
        explicit_zero = normalize_item("apartment", row(rent="0"), "11620")
        self.assertIsNotNone(explicit_zero)
        self.assertEqual(explicit_zero["monthly_rent_manwon"], 0)
        self.assertEqual(explicit_zero["lease_type"], "jeonse")

    def test_complete_zero_partition_and_run_are_distinct_from_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret",
                code_commit="a" * 40, observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                run_id="run-zero", requester=lambda _url, _timeout: xml([], 0), sleep=lambda _: None,
            )
            self.assertTrue(result["collection_succeeded"])
            self.assertTrue(result["version_complete"])
            self.assertTrue(result["audit_passed"])
            self.assertFalse(result["public_release_eligible"])

    def test_run_id_is_immutable_after_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            kwargs = dict(
                config=config(), storage_root=Path(temporary), service_key="secret", code_commit="a" * 40,
                observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc), run_id="same-run",
                requester=lambda _url, _timeout: xml([], 0), sleep=lambda _: None,
            )
            collect_rtms_observation(**kwargs)
            with self.assertRaises(FileExistsError):
                collect_rtms_observation(**kwargs)
            with self.assertRaises(ValueError):
                collect_rtms_observation(**kwargs, resume=True)

    def test_bounded_retry_and_resume_incomplete_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []

            def failure(url, _timeout):
                calls.append(url)
                raise RuntimeError("failed with secret")

            first = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret",
                code_commit="a" * 40, run_id="resume-run", requester=failure, sleep=lambda _: None,
            )
            self.assertEqual(len(calls), 2)
            self.assertFalse(first["version_complete"])
            manifest_text = (Path(temporary) / "runs" / "resume-run" / "partitions" / "apartment" / "11620" / "202608.json").read_text(encoding="utf-8")
            self.assertNotIn("secret", manifest_text)
            resumed = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret",
                code_commit="a" * 40, run_id="resume-run", resume=True,
                requester=lambda _url, _timeout: xml([row()], 1), sleep=lambda _: None,
            )
            self.assertTrue(resumed["version_complete"])

    def test_transient_failure_retries_then_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = 0

            def requester(_url, _timeout):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise TimeoutError("temporary")
                return xml([], 0)

            result = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret",
                code_commit="a" * 40, run_id="transient", requester=requester, sleep=lambda _: None,
            )
            self.assertTrue(result["version_complete"])
            self.assertEqual(calls, 2)

    def test_response_echoing_credential_is_redacted_before_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            echoed = xml([], 0).replace("</header>", "<echo>secret</echo></header>")
            collect_rtms_observation(
                config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                run_id="echo", requester=lambda _url, _timeout: echoed, sleep=lambda _: None,
            )
            stored = "".join(path.read_text(encoding="utf-8") for path in (root / "objects").rglob("*.xml"))
            self.assertNotIn("secret", stored)
            partition = json.loads(next((root / "runs" / "echo" / "partitions").rglob("*.json")).read_text(encoding="utf-8"))
            self.assertTrue(partition["pages"][0]["credential_text_redacted"])

    def test_missing_amount_keeps_raw_version_complete_but_fails_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = row()
            missing.pop("monthlyRent")
            result = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret", code_commit="a" * 40,
                run_id="missing-amount", requester=lambda _url, _timeout: xml([missing], 1), sleep=lambda _: None,
            )
            self.assertTrue(result["version_complete"])
            self.assertFalse(result["audit_passed"])
            self.assertEqual(result["normalized_record_count"], 0)

    def test_resume_uses_original_resolved_months_across_calendar_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = config(["202609"])
            collect_rtms_observation(
                config=original, storage_root=root, service_key="secret", code_commit="a" * 40,
                run_id="month-end", requester=lambda _url, _timeout: xml([], 1), sleep=lambda _: None,
            )
            restored = load_resume_config(root, "month-end")
            self.assertEqual(restored["contract_months"], ["202609"])
            self.assertNotIn("202610", restored["contract_months"])

    def test_pagination_total_change_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            def requester(url, _timeout):
                page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pageNo"][0])
                if page == 1:
                    return xml([row()] * 1000, 1001)
                return xml([row(day="15")], 1002)

            result = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret", code_commit="a" * 40,
                run_id="changing-total", requester=requester, sleep=lambda _: None,
            )
            self.assertFalse(result["version_complete"])
            partition = json.loads(next((Path(temporary) / "runs" / "changing-total" / "partitions").rglob("*.json")).read_text(encoding="utf-8"))
            self.assertIn("source_total_changed_during_pagination", partition["issues"])

    def test_empty_page_before_reported_total_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret", code_commit="a" * 40,
                run_id="empty-page", requester=lambda _url, _timeout: xml([], 1), sleep=lambda _: None,
            )
            self.assertFalse(result["version_complete"])
            partition = json.loads(next((Path(temporary) / "runs" / "empty-page" / "partitions").rglob("*.json")).read_text(encoding="utf-8"))
            self.assertIn("unexpected_empty_page", partition["issues"])

    def test_duplicate_pagination_content_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            repeated = [row()] * 1000
            result = collect_rtms_observation(
                config=config(), storage_root=Path(temporary), service_key="secret", code_commit="a" * 40,
                run_id="duplicate-pages", requester=lambda _url, _timeout: xml(repeated, 1500), sleep=lambda _: None,
            )
            self.assertFalse(result["version_complete"])
            partition = json.loads(next((Path(temporary) / "runs" / "duplicate-pages" / "partitions").rglob("*.json")).read_text(encoding="utf-8"))
            self.assertTrue(any("duplicates_prior_page_content" in issue for issue in partition["issues"]))

    def test_concurrent_collector_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with _collector_lock(root, "owner"):
                with self.assertRaises(RuntimeError):
                    collect_rtms_observation(
                        config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                        run_id="blocked", requester=lambda _url, _timeout: xml([], 0), sleep=lambda _: None,
                    )

    def test_dead_process_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(parents=True, exist_ok=True)
            lock = root / ".collector.lock"
            lock.write_text(json.dumps({"run_id": "dead", "pid": 2147483646}), encoding="utf-8")
            old = datetime.now().timestamp() - 600
            os.utime(lock, (old, old))
            result = collect_rtms_observation(
                config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                run_id="recovered", requester=lambda _url, _timeout: xml([], 0), sleep=lambda _: None,
            )
            self.assertTrue(result["version_complete"])

    def test_multiset_diff_excludes_baseline_and_reports_first_seen_interval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collect_rtms_observation(
                config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc), run_id="r1",
                requester=lambda _url, _timeout: xml([row()], 1), sleep=lambda _: None,
            )
            collect_rtms_observation(
                config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                observed_at=datetime(2026, 9, 8, tzinfo=timezone.utc), run_id="r2",
                requester=lambda _url, _timeout: xml([row(), row(day="15")], 2), sleep=lambda _: None,
            )
            output = root / "audit"
            report = audit_rtms_versions(storage_root=root, output_dir=output, cadence_days=7)
            self.assertEqual(report["added_occurrences"], 1)
            self.assertEqual(report["removed_occurrences"], 0)
            self.assertEqual(report["baseline_partition_count"], 1)
            comparison = report["comparisons"][0]
            self.assertEqual(comparison["first_seen_interval"]["after"], "2026-09-01T00:00:00Z")
            self.assertEqual(comparison["first_seen_interval"]["on_or_before"], "2026-09-08T00:00:00Z")

    def test_window_exit_is_not_interpreted_as_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collect_rtms_observation(
                config=config(["202607"]), storage_root=root, service_key="secret", code_commit="a" * 40,
                observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc), run_id="old-window",
                requester=lambda _url, _timeout: xml([row()], 1), sleep=lambda _: None,
            )
            collect_rtms_observation(
                config=config(["202608"]), storage_root=root, service_key="secret", code_commit="a" * 40,
                observed_at=datetime(2026, 9, 8, tzinfo=timezone.utc), run_id="new-window",
                requester=lambda _url, _timeout: xml([row()], 1), sleep=lambda _: None,
            )
            report = audit_rtms_versions(storage_root=root, output_dir=root / "audit")
            self.assertEqual(report["comparison_count"], 0)
            self.assertEqual(report["removed_occurrences"], 0)

    def test_schema_change_is_not_counted_as_reliable_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_id, when, item in (
                ("schema-a", datetime(2026, 9, 1, tzinfo=timezone.utc), row()),
                ("schema-b", datetime(2026, 9, 8, tzinfo=timezone.utc), row(newField="value")),
            ):
                collect_rtms_observation(
                    config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                    observed_at=when, run_id=run_id,
                    requester=lambda _url, _timeout, item=item: xml([item], 1), sleep=lambda _: None,
                )
            report = audit_rtms_versions(storage_root=root, output_dir=root / "audit")
            self.assertEqual(report["reliable_comparison_count"], 0)
            self.assertEqual(report["added_occurrences"], 0)

    def test_real_cadence_gap_is_reported_but_synthetic_time_is_not_real_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_id, day, label in (("real-a", 1, "observed"), ("real-b", 22, "observed"), ("fake", 29, "synthetic")):
                collect_rtms_observation(
                    config=config(), storage_root=root, service_key="secret", code_commit="a" * 40,
                    observed_at=datetime(2026, 9, day, tzinfo=timezone.utc), run_id=run_id, data_label=label,
                    requester=lambda _url, _timeout: xml([], 0), sleep=lambda _: None,
                )
            report = audit_rtms_versions(storage_root=root, output_dir=root / "audit", cadence_days=7)
            self.assertEqual(report["estimated_missed_scheduled_runs"], 2)
            self.assertEqual(report["actual_observation_span_days"], 21)
            self.assertEqual(report["synthetic_run_count"], 1)

    def test_annual_identical_bytes_reuse_object_but_record_two_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            source.mkdir()
            with zipfile.ZipFile(source / "seoul-rents-2025.zip", "w") as archive:
                archive.writestr("data.csv", "header\n")
            first = observe_annual_archives(
                input_dir=source, storage_root=root / "store", run_id="annual-1",
                code_commit="a" * 40, observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            second = observe_annual_archives(
                input_dir=source, storage_root=root / "store", run_id="annual-2",
                code_commit="a" * 40, observed_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
            )
            self.assertEqual(first["files"][0]["object_path"], second["files"][0]["object_path"])
            self.assertEqual(len(list((root / "store" / "objects").rglob("*.zip"))), 1)


if __name__ == "__main__":
    unittest.main()
