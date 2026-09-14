import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from scripts.collect_rtms_history import CollectionStopped, GuardedRequester
from worldmodel_data.observation import collect_rtms_observation
from tests.test_observation import config


class HistoryCollectionTests(unittest.TestCase):
    def test_budget_stop_preserves_partial_run_without_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guarded = GuardedRequester(root, "secret", {}, lambda: None, 1,
                request=lambda *_: "<response><resultCode>000</resultCode><totalCount>0</totalCount></response>")
            with self.assertRaises(CollectionStopped):
                collect_rtms_observation(config=config(["202501", "202502"]), storage_root=root,
                    service_key="secret", code_commit="a" * 40, run_id="budget-stop",
                    requester=guarded, sleep=lambda _: None)
            state = json.loads((root / "runs/budget-stop/run.json").read_text())
            self.assertEqual(state["status"], "running")
            self.assertFalse(state.get("version_complete", False))
            self.assertEqual(len(list((root / "runs/budget-stop/partitions").rglob("*.json"))), 1)

    def test_budget_counts_attempts_and_stops_before_next_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {}
            calls = []
            def request(url, timeout):
                calls.append(url)
                return "<response><resultCode>000</resultCode></response>"
            guarded = GuardedRequester(Path(directory), "secret", state, lambda: None, 1, request)
            guarded("https://example.test/api?serviceKey=secret", 10)
            with self.assertRaises(CollectionStopped):
                guarded("https://example.test/api?serviceKey=secret", 10)
            self.assertEqual(len(calls), 1)

    def test_auth_error_stops_and_stored_error_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, state = Path(directory), {}
            def request(url, timeout):
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"secret denied"))
            guarded = GuardedRequester(root, "secret", state, lambda: None, request=request)
            with self.assertRaises(CollectionStopped):
                guarded("https://example.test/api?serviceKey=secret", 10)
            payload = (root / state["last_source_error_object"]["path"]).read_text()
            self.assertNotIn("secret", payload)

    def test_source_quota_error_does_not_become_zero_records(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {}
            guarded = GuardedRequester(Path(directory), "secret", state, lambda: None,
                                      request=lambda *_: "<response><returnReasonCode>22</returnReasonCode></response>")
            with self.assertRaises(CollectionStopped):
                guarded("https://example.test/api", 10)


if __name__ == "__main__":
    unittest.main()
