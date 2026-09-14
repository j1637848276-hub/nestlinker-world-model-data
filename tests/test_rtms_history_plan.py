import json
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_rtms_history import prepare
from worldmodel_data.observation import load_observation_config


class HistoryPlanTests(unittest.TestCase):
    def test_year_batches_and_immutable_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "template.json"
            template.write_text(json.dumps({
                "schema_version": 1, "display_timezone": "Asia/Seoul",
                "cadence_days": 7, "rolling_contract_months": 12,
                "sources": ["apartment"], "lawd_codes": ["11110"],
            }), encoding="utf-8")
            paths = prepare(template, root / "plan", 2024, 2025, 9)
            self.assertEqual(len(load_observation_config(paths[0])["contract_months"]), 12)
            self.assertEqual(load_observation_config(paths[1])["contract_months"][-1], "202509")
            self.assertEqual(len(load_observation_config(paths[1])["contract_months"]), 9)
            self.assertNotIn("202609", load_observation_config(paths[1])["contract_months"])
            with self.assertRaises(FileExistsError):
                prepare(template, root / "plan", 2024, 2025, 9)
            with self.assertRaises(ValueError):
                prepare(template, root / "invalid", 2025, 2024, 9)


if __name__ == "__main__":
    unittest.main()
