import json
import tempfile
import unittest
from pathlib import Path

from tests.test_observation import config, row, xml
from worldmodel_data.observation import collect_rtms_observation
from worldmodel_data.observation_export import export_observation


class ExportTests(unittest.TestCase):
    def test_local_hash_checked_immutable_export_with_distinct_area(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "data/raw/source"
            cfg = config()
            cfg["sources"] = ["single_multi"]
            collect_rtms_observation(config=cfg, storage_root=storage, service_key="secret",
                code_commit="a" * 40, run_id="baseline",
                requester=lambda *_: xml([row(totalFloorAr="45.5", rent="0")]), sleep=lambda _: None)
            output = root / "data/work/export"
            summary = export_observation(storage, "baseline", output)
            self.assertEqual(summary["record_count"], 1)
            record = json.loads((output / "records.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["reported_area_sqm"], 45.5)
            self.assertIsNone(record["exclusive_area_sqm"])
            self.assertIsNone(record["first_seen_at"])
            self.assertEqual(record["monthly_rent_manwon"], 0)
            with self.assertRaises(FileExistsError):
                export_observation(storage, "baseline", output)
            with self.assertRaises(ValueError):
                export_observation(storage, "baseline", root / "data/snapshots/public")
            obj = next((storage / "objects").rglob("*.xml"))
            obj.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                export_observation(storage, "baseline", root / "data/work/tampered")
