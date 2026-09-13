import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from worldmodel_data.manifest import sha256_file
from worldmodel_data.seoul_rents import load_acquisition_ledger, publish_monthly_snapshot


class HistoryBackfillTests(unittest.TestCase):
    def test_txt_adapter_audit_binding_accounting_and_immutability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "seoul-rents-2011.zip"
            text = io.StringIO(newline="")
            writer = csv.writer(text, delimiter="\t")
            writer.writerow(["접수년도", "자치구코드", "자치구명", "계약일", "전월세구분",
                             "보증금(만원)", "임대료(만원)", "건물용도"])
            for day in range(1, 12):
                writer.writerow([2011, "11110", "종로구", f"201101{day:02d}", "월세", 1000, 50, "아파트"])
            writer.writerow([2012, "11110", "종로구", "20110112", "월세", 1000, 50, "아파트"])
            writer.writerow([2011, "11110", "종로구", "20100112", "월세", 1000, 50, "아파트"])
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("rents.txt", text.getvalue().encode("cp949"))
            acquisition = {"file_year": 2011, "sha256": sha256_file(archive),
                           "bytes": archive.stat().st_size, "retrieved_at": "2026-09-05T00:00:00Z"}
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({"schema_version": 1, "source_id": "seoul-rental-price-files",
                                          "files": [acquisition]}), encoding="utf-8")
            acquisitions = load_acquisition_ledger(ledger)
            audit = {"year": 2011, "complete": True, "status": "needs_review", "source_rows": 13,
                     "sha256": acquisition["sha256"], "bytes": acquisition["bytes"],
                     "missing_required_columns": []}
            report = root / "2011.audit.json"
            report.write_text(json.dumps(audit), encoding="utf-8")
            arguments = dict(created_at="2026-09-14T00:00:00Z", input_commit="a" * 40,
                             acquisitions=acquisitions, audit_dir=root)
            snapshot = root / "snapshot"
            publish_monthly_snapshot({2011: archive}, snapshot, **arguments)
            payload = json.loads((snapshot / "seoul-rental-monthly.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["sourceRowCount"], 13)
            self.assertEqual(payload["excludedWrongContractYear"], 1)
            self.assertEqual(payload["excludedReceiptYearMismatch"], 1)
            self.assertEqual(payload["records"][0]["count"], 11)
            with self.assertRaisesRegex(ValueError, "immutable"):
                publish_monthly_snapshot({2011: archive}, snapshot, **arguments)
            audit["sha256"] = "b" * 64
            report.write_text(json.dumps(audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mismatched"):
                publish_monthly_snapshot({2011: archive}, root / "rejected", **arguments)
            self.assertFalse((root / "rejected").exists())
            audit["sha256"] = acquisition["sha256"]
            audit["complete"] = False
            report.write_text(json.dumps(audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                publish_monthly_snapshot({2011: archive}, root / "incomplete", **arguments)
            audit["complete"] = True
            audit["source_rows"] = 12
            report.write_text(json.dumps(audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source-row counts"):
                publish_monthly_snapshot({2011: archive}, root / "wrong-count", **arguments)
            self.assertFalse((root / "wrong-count").exists())

    def test_pre_2022_publish_requires_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "audit reports"):
                publish_monthly_snapshot({2011: root / "missing.zip"}, root / "snapshot",
                                         created_at="2026-09-14T00:00:00Z", input_commit="a" * 40,
                                         acquisitions={})
