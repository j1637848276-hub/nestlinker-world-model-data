import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from worldmodel_data.manifest import file_entry, validate_published_file, validate_snapshot


def complete_entry(entry):
    return {
        **entry,
        "data_label": "observed",
        "geography": "test",
        "temporal_coverage": {"as_of": "2026-09-01"},
        "usage": "test_only",
        "limitations": ["fixture"],
    }


def complete_manifest(files):
    return {
        "schema_version": 1,
        "snapshot_id": "fixture-2026-09-01",
        "created_at": "2026-09-01T00:00:00+00:00",
        "data_label": "observed",
        "geography": "test",
        "derivation": "fixture",
        "input_repository": "https://example.test/repo",
        "input_commit": "a" * 40,
        "input_files": [{"path": "input", "sha256": "b" * 64}],
        "transformation": {"version": "test"},
        "limitations": ["fixture"],
        "files": files,
    }


class ManifestTests(unittest.TestCase):
    def test_file_entry_and_validation_detect_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "rows.json"
            data.write_text(json.dumps({"records": [{"id": 1}]}), encoding="utf-8")
            entry = complete_entry(file_entry(data, root, "source-a", "https://example.test/source"))
            (root / "manifest.json").write_text(
                json.dumps(complete_manifest([entry])), encoding="utf-8"
            )
            self.assertEqual(validate_snapshot(root, {"source-a"}), [])
            data.write_text("tampered", encoding="utf-8")
            self.assertTrue(any("sha256 mismatch" in item for item in validate_snapshot(root, {"source-a"})))

    def test_manifest_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = complete_manifest([complete_entry({
                    "path": "../secret",
                    "source_id": "source-a",
                    "source_url": "https://example.test",
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "bytes": 0,
                })])
            (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(any("unsafe path" in item for item in validate_snapshot(root, {"source-a"})))

    def test_manifest_validates_secondary_sources_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "rows.json"
            data.write_text("[]", encoding="utf-8")
            entry = complete_entry(file_entry(
                data,
                root,
                "source-a",
                "https://example.test/source-a",
                ["source-b"],
            ))
            (root / "manifest.json").write_text(
                json.dumps(complete_manifest([entry])), encoding="utf-8"
            )
            self.assertEqual(validate_snapshot(root, {"source-a", "source-b"}), [])
            (root / "unlisted.csv").write_text("secret", encoding="utf-8")
            errors = validate_snapshot(root, {"source-a", "source-b"})
            self.assertTrue(any("unlisted files" in item for item in errors))
            self.assertTrue(any("unlisted.csv" in item for item in errors))

    def test_manifest_checks_record_count_and_catalog_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "rows.json"
            data.write_text('[{"id": 1}]', encoding="utf-8")
            entry = complete_entry(file_entry(data, root, "source-a", "https://wrong.test"))
            entry["record_count"] = 999
            (root / "manifest.json").write_text(
                json.dumps(complete_manifest([entry])), encoding="utf-8"
            )
            known = {
                "source-a": {
                    "landing_url": "https://example.test/source",
                    "status": "seeded",
                }
            }
            errors = validate_snapshot(root, known)
            self.assertTrue(any("source_url mismatch" in item for item in errors))
            self.assertTrue(any("record_count mismatch" in item for item in errors))

            entry["source_url"] = "https://example.test/source-old"
            entry["record_count"] = 1
            (root / "manifest.json").write_text(
                json.dumps(complete_manifest([entry])), encoding="utf-8"
            )
            known["source-a"]["historical_landing_urls"] = [
                "https://example.test/source-old"
            ]
            self.assertEqual(validate_snapshot(root, known), [])

    def test_manifest_rejects_empty_file_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "rows.json"
            data.write_text("[]", encoding="utf-8")
            entry = complete_entry(file_entry(data, root, "source-a", "https://example.test/source"))
            entry["usage"] = ""
            entry["limitations"] = []
            entry["secondary_source_ids"] = ["source-b"]
            entry["source_composition"] = [
                {"property_type": "fixture", "source_id": "source-b", "record_count": 1}
            ]
            (root / "manifest.json").write_text(
                json.dumps(complete_manifest([entry])), encoding="utf-8"
            )
            errors = validate_snapshot(root, {"source-a", "source-b"})
            self.assertTrue(any("usage must be" in item for item in errors))
            self.assertTrue(any("limitations must contain" in item for item in errors))
            self.assertTrue(any("source_composition count mismatch" in item for item in errors))

    def test_published_schema_and_privacy_checks_reject_added_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demographics.json"
            path.write_text(
                json.dumps({"districts": [{
                    "guName": "test", "lawdCode": "11110", "totalResidents": 1,
                    "koreanCount": 1, "foreignCount": 0, "maleCount": 1,
                    "femaleCount": 0, "age": {}, "nationality": {"rare": 1},
                }]}),
                encoding="utf-8",
            )
            errors = validate_published_file(path)
            self.assertTrue(any("unexpected fields" in item for item in errors))
            self.assertTrue(any("forbidden PII key" in item for item in errors))

            gosiwon = Path(directory) / "seoul-gosiwon-registry.json"
            gosiwon.write_text(json.dumps({"listings": [{
                "id": "1", "name": "test", "guName": "test", "address": "담당자 홍길동 02-123-4567",
                "roadAddress": "", "areaSqm": 1, "floors": "1", "reportedAt": "2026-09-01",
            }]}), encoding="utf-8")
            self.assertTrue(any("Korean phone" in item for item in validate_published_file(gosiwon)))

    def test_rental_history_rejects_invalid_counts_months_and_quantiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seoul-rental-monthly.json"
            path.write_text(json.dumps({"records": [{
                "contractYear": 2024, "contractMonth": "2025-13", "guCode": "11110",
                "guName": "종로구", "buildingUse": "단독다가구", "leaseType": "monthly",
                "count": 0, "depositP25Manwon": 2000, "depositMedianManwon": 1000,
                "depositP75Manwon": 1500, "monthlyRentP25Manwon": 60,
                "monthlyRentMedianManwon": 50, "monthlyRentP75Manwon": 55,
            }]}), encoding="utf-8")

            errors = validate_published_file(path)

            self.assertTrue(any("contractMonth" in item for item in errors))
            self.assertTrue(any("count must be at least 10" in item for item in errors))
            self.assertTrue(any("deposit quantiles" in item for item in errors))
            self.assertTrue(any("rent quantiles" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
