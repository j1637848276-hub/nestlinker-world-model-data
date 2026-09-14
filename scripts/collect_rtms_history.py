"""Bounded, restartable yearly RTMS backfill. Raw/control outputs stay local."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldmodel_data.observation import (
    _collector_lock, _json_bytes, _redact_secret_text, _replace_json, _store_object,
    audit_rtms_versions, collect_rtms_observation, load_observation_config,
)
from worldmodel_data.rtms import _request


class CollectionStopped(BaseException):
    """Bypass partition retries for exhausted budgets or denied access."""


class GuardedRequester:
    def __init__(self, storage, key, state, persist, budget=6000, request=_request):
        self.storage, self.key, self.state = storage, key, state
        self.persist, self.budget, self.request = persist, budget, request

    def __call__(self, url, timeout):
        endpoint = urllib.parse.urlsplit(url).path
        counts = self.state.setdefault("request_attempts_by_endpoint", {})
        if counts.get(endpoint, 0) >= self.budget:
            raise CollectionStopped("engineering_request_budget_reached")
        counts[endpoint] = counts.get(endpoint, 0) + 1
        self.persist()
        try:
            text = self.request(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403, 429):
                raise
            text = exc.read(1024 * 1024).decode("utf-8", errors="replace")
            self._save_error(text)
            raise CollectionStopped("http_access_or_rate_limit_" + str(exc.code)) from None
        try:
            root = ET.fromstring(text)
            code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode")
        except ET.ParseError:
            return text  # Collector stores and diagnoses malformed XML.
        if code and code.strip() not in ("00", "000"):
            self._save_error(text)
            safe_code = code.strip() if code.strip().isdigit() and len(code.strip()) <= 3 else "invalid"
            raise CollectionStopped("source_error_code_" + safe_code)
        return text

    def _save_error(self, text):
        safe, _ = _redact_secret_text(text, self.key)
        digest, path = _store_object(self.storage, "xml", safe.encode("utf-8"))
        self.state["last_source_error_object"] = {
            "sha256": digest, "path": path.relative_to(self.storage).as_posix(),
        }
        self.persist()


def matching_runs(storage, digest):
    if not (storage / "runs").exists():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in
            sorted((storage / "runs").glob("*/run.json"))
            if json.loads(path.read_text(encoding="utf-8")).get("config_sha256") == digest]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", required=True)
    parser.add_argument("--storage-root", default="data/raw/rtms-history-observations")
    parser.add_argument("--request-budget-per-api", type=int, default=6000)
    args = parser.parse_args()
    if not 1 <= args.request_budget_per_api <= 9000:
        parser.error("request budget must be between 1 and 9000")
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        parser.error("DATA_GO_KR_SERVICE_KEY is not configured")
    plan = Path(args.plan_dir).resolve()
    root = Path(args.storage_root).resolve()
    paths = sorted(plan.glob("rtms-history-*.json"), reverse=True)
    if not paths:
        parser.error("no history configurations found")
    configurations = [(path, load_observation_config(path)) for path in paths]
    if any(config["rolling_contract_months"] != 0 for _, config in configurations):
        parser.error("history configurations must disable rolling months")
    repo = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip())
    execution = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    control = root / "control"
    state_path = control / "executions" / (execution + ".json")
    state = {"execution_id": execution, "status": "running", "plan_dir": str(plan),
             "request_budget_per_api": args.request_budget_per_api,
             "request_attempts_by_endpoint": {}, "year_results": [],
             "code_commit": commit, "code_dirty": dirty,
             "started_at": datetime.now(timezone.utc).isoformat()}
    persist = lambda: _replace_json(state_path, state)
    with _collector_lock(control, execution):
        persist()
        try:
            for path, config in configurations:
                year = path.stem.rsplit("-", 1)[-1]
                digest = hashlib.sha256(_json_bytes(config)).hexdigest()
                storage = root / "by-year" / year
                # Reuse the already acquired initial 2025 baseline without copying it.
                existing = matching_runs(root, digest) + matching_runs(storage, digest)
                complete = [run for run in existing if run.get("version_complete") and run.get("audit_passed")]
                if complete:
                    state["year_results"].append({"year": year, "status": "already_complete",
                                                  "run_id": complete[-1]["run_id"],
                                                  "raw_record_count": complete[-1]["raw_record_count"]})
                    persist()
                    continue
                partial = matching_runs(storage, digest)
                resumable = [run for run in partial if run.get("status") != "finalized"]
                state["active_year"] = year
                state["active_storage_root"] = str(storage)
                persist()
                guarded = GuardedRequester(storage, key, state, persist, args.request_budget_per_api)
                result = collect_rtms_observation(
                    config=config, storage_root=storage, service_key=key,
                    code_commit=commit, code_dirty=dirty, requester=guarded,
                    run_id=resumable[-1]["run_id"] if resumable else None,
                    resume=bool(resumable),
                )
                state["year_results"].append({"year": year, "status": result["status"],
                                              "run_id": result["run_id"],
                                              "raw_record_count": result["raw_record_count"],
                                              "version_complete": result["version_complete"],
                                              "audit_passed": result["audit_passed"]})
                persist()
                if not result["version_complete"] or not result["audit_passed"]:
                    raise CollectionStopped("partition_incomplete_or_normalization_failed")
                audit_rtms_versions(storage_root=storage, output_dir=storage / "audits" / execution,
                                    cadence_days=31)
                print(year, "complete", result["raw_record_count"], flush=True)
            state["status"] = "completed"
        except CollectionStopped as exc:
            state["status"] = "stopped"
            state["stop_reason"] = str(exc)
        except Exception as exc:
            state["status"] = "failed"
            state["stop_reason"] = type(exc).__name__  # Never print exception URLs or credentials.
        finally:
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
            persist()
    print("execution", execution, state["status"], flush=True)
    return 0 if state["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
