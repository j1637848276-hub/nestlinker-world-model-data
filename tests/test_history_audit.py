import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from worldmodel_data.history_audit import audit_history
from worldmodel_data.manifest import sha256_file
from worldmodel_data.cli import main

HEADERS = ["접수년도", "자치구코드", "자치구명", "계약일", "전월세구분", "보증금(만원)", "임대료(만원)", "건물용도"]
ROW = ["2024", "11620", "관악구", "20240103", "월세", "1,000", "50", "아파트"]


def archive(root, year, rows, headers=HEADERS, encoding="cp949", delimiter=",", member="rents.txt"):
    text = io.StringIO(newline="")
    writer = csv.writer(text, delimiter=delimiter)
    writer.writerow(headers)
    writer.writerows(rows)
    path = root / f"seoul-rents-{year}.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, text.getvalue().encode(encoding))
    return path


def ledger(path, archive_path, year=2024, **overrides):
    item = {"file_year": year, "retrieved_at": "2026-09-05T00:00:00+00:00",
            "bytes": archive_path.stat().st_size, "sha256": sha256_file(archive_path), **overrides}
    path.write_text(json.dumps({"schema_version": 1, "source_id": "seoul-rental-price-files",
                                "source_page_sha256": "a" * 64, "files": [item]}), encoding="utf-8")
    return path


class HistoryAuditTests(unittest.TestCase):
    def test_duplicates_accounting_readonly_and_no_source_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            invalid = ROW.copy()
            invalid[5] = "-1"
            late = ROW.copy()
            late[0] = "2025"
            other = ROW.copy()
            other[3] = "20230103"
            path = archive(raw, 2024, [ROW, ROW, invalid, late, other])
            digest = sha256_file(path)
            result = audit_history(raw, root / "out", [2024])
            report = result["years"][0]
            self.assertEqual(sha256_file(path), digest)
            self.assertEqual(report["source_rows"], 5)
            self.assertEqual(report["exact_duplicate_rows"], 1)
            self.assertEqual(report["status"], "quarantined")
            self.assertEqual(report["accounting"], {"eligible_for_existing_replay": 2, "invalid": 1, "other_contract_year": 1, "receipt_year_mismatch": 1})
            self.assertEqual(report["contract_year_counts"], {"2023": 1, "2024": 4})
            self.assertEqual(report["receipt_year_counts"], {"2024": 4, "2025": 1})
            self.assertEqual(report["contract_to_receipt_year_counts"], {"2023->2024": 1, "2024->2024": 3, "2024->2025": 1})
            self.assertEqual(report["unknown_district_code_counts"], {})
            self.assertNotIn("20240103", json.dumps(result))
            with self.assertRaises(FileExistsError):
                audit_history(raw, root / "out", [2024])

    def test_utf8_utf16_delimiters_missing_year_and_schema_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            archive(raw, 2023, [ROW], encoding="utf-8", delimiter="\t")
            archive(raw, 2024, [ROW[1:]], headers=HEADERS[1:], encoding="utf-16", delimiter=";")
            result = audit_history(raw, root / "out", [2023, 2024, 2025])
            first, second, third = result["years"]
            self.assertEqual(first["delimiter"], "\t")
            self.assertEqual(second["encoding"], "utf-16")
            self.assertEqual(second["schema_change_from_previous"]["removed"], ["접수년도"])
            self.assertEqual(second["issue_row_counts"]["missing_or_invalid_receipt_year"], 1)
            self.assertEqual(second["status"], "requires_adapter")
            self.assertEqual(third["status"], "missing")

    def test_corrupt_archive_does_not_abort_other_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            (raw / "seoul-rents-2023.zip").write_bytes(b"not a ZIP")
            archive(raw, 2024, [ROW])
            result = audit_history(raw, root / "out", [2023, 2024])
            self.assertFalse(result["years"][0]["complete"])
            self.assertTrue(result["years"][1]["complete"])

    def test_cross_file_overlap_and_uneven_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            archive(raw, 2023, [ROW])
            archive(raw, 2024, [ROW, ROW + ["extra"]])
            result = audit_history(raw, root / "out", [2023, 2024])
            self.assertEqual(result["adjacent_archive_overlaps"][0]["shared_exact_field_groups"], 1)
            self.assertEqual(result["years"][1]["accounting"]["invalid"], 1)

    def test_rejects_nan_threshold_and_output_inside_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                audit_history(root, root / "out", [2024])
            with self.assertRaises(ValueError):
                audit_history(root / "raw", root / "out", [2024], float("nan"))

    def test_cli_missing_year_emits_report_and_nonzero_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = main(["audit-seoul-history", "--raw-dir", str(root / "raw"),
                         "--output-dir", str(root / "out"), "--years", "2025"])
            self.assertEqual(code, 2)
            result = json.loads((root / "out" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(result["years"][0]["status"], "missing")

    def test_bad_member_structure_and_duplicate_headers_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            path = archive(raw, 2023, [ROW])
            with zipfile.ZipFile(path, "a") as z:
                z.writestr("second.csv", "x\n1\n")
            archive(raw, 2024, [ROW], headers=HEADERS[:-1] + [HEADERS[0]])
            result = audit_history(raw, root / "out", [2023, 2024])
            self.assertTrue(all(r["status"] == "quarantined" for r in result["years"]))
            self.assertTrue(all(not r["complete"] for r in result["years"]))

    def test_acquisition_ledger_binds_hash_size_and_retrieval_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            source = archive(raw, 2024, [ROW])
            acquisition = ledger(root / "ledger.json", source)
            result = audit_history(raw, root / "out", [2024], acquisition_ledger=acquisition)
            report = result["years"][0]
            self.assertEqual(report["status"], "needs_review")
            self.assertNotIn("acquisition_record_mismatch", report["issues"])
            self.assertEqual(report["acquisition"]["sha256"], sha256_file(source))
            self.assertEqual(result["acquisition_source_page_sha256"], "a" * 64)

    def test_acquisition_mismatch_or_missing_year_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            source = archive(raw, 2024, [ROW])
            acquisition = ledger(root / "ledger.json", source, bytes=source.stat().st_size + 1)
            result = audit_history(raw, root / "out", [2024, 2025], acquisition_ledger=acquisition)
            self.assertEqual(result["years"][0]["issues"], ["acquisition_record_mismatch"])
            self.assertEqual(result["years"][1]["issues"], ["missing_archive", "missing_acquisition_record"])
            self.assertTrue(all(r["status"] == "quarantined" for r in result["years"]))


if __name__ == "__main__":
    unittest.main()
