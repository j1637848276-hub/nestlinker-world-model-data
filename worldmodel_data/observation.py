"""Versioned RTMS observations and aggregate timeliness diagnostics.

Raw responses, record fingerprints, and per-partition details belong under the
gitignored data/raw tree.  Public reports contain counts only.  A fingerprint
identifies exact field content, not a real-world transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .manifest import sha256_file
from .rtms import BASE_URL, PATHS, SOURCE_IDS, SOURCE_LANDING_URLS, normalize_item, parse_xml_items, rolling_months

OBSERVATION_VERSION = "rtms-continuous-observation-v1"
ANNUAL_OBSERVATION_VERSION = "seoul-annual-file-observation-v1"
SEOUL_ANNUAL_LANDING_URL = "https://data.seoul.go.kr/dataList/OA-21276/A/1/datasetView.do"
DEFAULT_SEEDED_SOURCES = ("apartment", "officetel", "single_multi")
MONTH_PATTERN = re.compile(r"\d{6}")
RUN_ID_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,79}")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(_json_bytes(value))


def _replace_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with temporary.open("xb") as handle:
        handle.write(_json_bytes(value))
    os.replace(temporary, path)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Optional[datetime] = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("observation time must include timezone")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_month(value: str) -> bool:
    if not MONTH_PATTERN.fullmatch(value):
        return False
    try:
        date(int(value[:4]), int(value[4:]), 1)
    except ValueError:
        return False
    return True


def load_observation_config(path: Path, *, include_backfill: bool = False, today: Optional[date] = None) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("observation config schema_version must be 1")
    if payload.get("display_timezone") != "Asia/Seoul":
        raise ValueError("display_timezone must be Asia/Seoul")
    cadence_days = payload.get("cadence_days")
    rolling_count = payload.get("rolling_contract_months")
    minimum_months = payload.get("minimum_observation_months", 3)
    target_months = payload.get("target_observation_months", 6)
    if not isinstance(cadence_days, int) or not 1 <= cadence_days <= 31:
        raise ValueError("cadence_days must be between 1 and 31")
    if not isinstance(rolling_count, int) or not 0 <= rolling_count <= 60:
        raise ValueError("rolling_contract_months must be between 0 and 60")
    if not isinstance(minimum_months, int) or minimum_months < 3:
        raise ValueError("minimum_observation_months must be at least 3")
    if not isinstance(target_months, int) or target_months < minimum_months:
        raise ValueError("target_observation_months must be at least the minimum")
    sources = payload.get("sources", list(DEFAULT_SEEDED_SOURCES))
    lawd_codes = payload.get("lawd_codes")
    if not isinstance(sources, list) or not sources or any(source not in PATHS for source in sources):
        raise ValueError("sources must be a non-empty list of known RTMS property types")
    if len(sources) != len(set(sources)):
        raise ValueError("sources must not contain duplicates")
    if not isinstance(lawd_codes, list) or not lawd_codes:
        raise ValueError("lawd_codes must be a non-empty list")
    if any(not isinstance(code, str) or not re.fullmatch(r"\d{5}", code) for code in lawd_codes):
        raise ValueError("lawd_codes must contain five-digit strings")
    if len(lawd_codes) != len(set(lawd_codes)):
        raise ValueError("lawd_codes must not contain duplicates")
    fixed = payload.get("fixed_contract_months", [])
    backfill = payload.get("low_frequency_contract_months", []) if include_backfill else []
    if not isinstance(fixed, list) or not isinstance(backfill, list):
        raise ValueError("fixed month queues must be lists")
    months = rolling_months(rolling_count, today) if rolling_count else []
    for month in [*fixed, *backfill]:
        if not isinstance(month, str) or not _valid_month(month):
            raise ValueError(f"invalid configured contract month: {month!r}")
        if month not in months:
            months.append(month)
    if not months:
        raise ValueError("observation config must select at least one contract month")
    delay = payload.get("delay_seconds", 0.15)
    timeout = payload.get("request_timeout_seconds", 30)
    retries = payload.get("retry_attempts", 3)
    backoff = payload.get("retry_backoff_seconds", 1.0)
    max_pages = payload.get("maximum_pages_per_partition", 1000)
    if not isinstance(delay, (int, float)) or not 0 <= delay <= 5:
        raise ValueError("delay_seconds must be between 0 and 5")
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ValueError("request_timeout_seconds must be between 1 and 300")
    if not isinstance(retries, int) or not 1 <= retries <= 10:
        raise ValueError("retry_attempts must be between 1 and 10")
    if not isinstance(backoff, (int, float)) or not 0 <= backoff <= 60:
        raise ValueError("retry_backoff_seconds must be between 0 and 60")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 10000:
        raise ValueError("maximum_pages_per_partition must be between 1 and 10000")
    return {
        "schema_version": 1,
        "display_timezone": "Asia/Seoul",
        "cadence_days": cadence_days,
        "minimum_observation_months": minimum_months,
        "target_observation_months": target_months,
        "sources": sources,
        "lawd_codes": lawd_codes,
        "contract_months": months,
        "rolling_contract_months": rolling_count,
        "fixed_contract_months": fixed,
        "low_frequency_contract_months": backfill,
        "include_low_frequency": include_backfill,
        "delay_seconds": float(delay),
        "request_timeout_seconds": timeout,
        "retry_attempts": retries,
        "retry_backoff_seconds": float(backoff),
        "maximum_pages_per_partition": max_pages,
    }


def load_resume_config(storage_root: Path, run_id: str) -> dict:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id contains unsupported characters")
    path = storage_root.resolve() / "runs" / run_id / "run.json"
    if not path.is_file():
        raise ValueError(f"cannot resume missing run: {run_id}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("observation_version") != OBSERVATION_VERSION:
        raise ValueError("resume run uses an unsupported observation version")
    if state.get("status") == "finalized":
        raise ValueError("finalized observation runs are immutable")
    config = state.get("config")
    if not isinstance(config, dict):
        raise ValueError("resume run has no resolved config")
    return config


def _request_url(source: str, lawd_code: str, month: str, page: int, service_key: str) -> str:
    query = urllib.parse.urlencode({
        "serviceKey": urllib.parse.unquote(service_key.strip()),
        "LAWD_CD": lawd_code,
        "DEAL_YMD": month,
        "pageNo": page,
        "numOfRows": 1000,
    }, safe="%")
    return f"{BASE_URL}{PATHS[source]}?{query}"


def _redacted_error(exc: BaseException, service_key: str) -> str:
    value = f"{type(exc).__name__}: {exc}"
    candidates = {service_key, urllib.parse.unquote(service_key), urllib.parse.quote(service_key, safe="")}
    for candidate in sorted((item for item in candidates if item), key=len, reverse=True):
        value = value.replace(candidate, "[REDACTED]")
    return value[:500]


def _redact_secret_text(value: str, service_key: str) -> Tuple[str, bool]:
    changed = False
    candidates = {service_key, urllib.parse.unquote(service_key), urllib.parse.quote(service_key, safe="")}
    for candidate in sorted((item for item in candidates if item), key=len, reverse=True):
        if candidate in value:
            value = value.replace(candidate, "[REDACTED]")
            changed = True
    return value, changed


def _page_total(xml_text: str) -> int:
    root = ET.fromstring(xml_text)
    value = root.findtext(".//totalCount")
    if value is None or not value.strip().isdigit():
        raise ValueError("RTMS response has no valid totalCount")
    return int(value.strip())


def _record_fingerprint(source: str, lawd_code: str, raw: Mapping[str, str]) -> str:
    identity = json.dumps(
        {"source": source, "lawd_code": lawd_code, "raw": raw},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _source_reported_at(raw: Mapping[str, str]) -> Optional[str]:
    lowered = {str(key).lower(): str(value).strip() for key, value in raw.items()}
    for key in ("rgstdate", "receiptdate", "접수일", "신고일"):
        value = lowered.get(key)
        if not value:
            continue
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) == 8:
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:])).isoformat()
            except ValueError:
                return None
    return None


def _raw_quality(raw: Mapping[str, str]) -> Dict[str, bool]:
    lowered = {str(key).lower(): str(value).strip() for key, value in raw.items()}
    return {
        "contract_date": not all(lowered.get(key, "") for key in ("dealyear", "dealmonth", "dealday")),
        "legal_dong": not any(lowered.get(key, "") for key in ("umdnm", "법정동", "법정동명")),
        "deposit": not any(lowered.get(key, "") for key in ("deposit", "보증금액")),
        "monthly_rent": not any(lowered.get(key, "") for key in ("monthlyrent", "월세금액")),
    }


def _store_object(storage_root: Path, suffix: str, payload: bytes) -> Tuple[str, Path]:
    digest = hashlib.sha256(payload).hexdigest()
    target = storage_root / "objects" / "sha256" / digest[:2] / f"{digest}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or sha256_file(target) != digest:
            raise ValueError(f"content-addressed object mismatch: {target}")
        return digest, target
    temporary = target.with_name(target.name + f".{os.getpid()}.partial")
    try:
        if temporary.exists():
            temporary.unlink()
        with temporary.open("xb") as handle:
            handle.write(payload)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest, target


def _process_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if pid < 1:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def _collector_lock(storage_root: Path, run_id: str):
    storage_root.mkdir(parents=True, exist_ok=True)
    lock = storage_root / ".collector.lock"
    owner = {"run_id": run_id, "pid": os.getpid(), "created_at": _timestamp()}
    for attempt in range(2):
        try:
            with lock.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(owner, separators=(",", ":")) + "\n")
            break
        except FileExistsError as exc:
            raw_owner = lock.read_text(encoding="utf-8", errors="replace").strip() if lock.is_file() else ""
            try:
                prior = json.loads(raw_owner)
                prior_pid = int(prior["pid"])
                running = _process_is_running(prior_pid)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError):
                # Do not remove a lock while its creator may still be writing it.
                running = lock.is_file() and time.time() - lock.stat().st_mtime < 300
            if running or attempt:
                label = prior.get("run_id", "unknown") if isinstance(prior, dict) else "unknown"
                raise RuntimeError(f"another collector holds the lock: {label}") from exc
            lock.unlink(missing_ok=True)
    try:
        yield
    finally:
        if lock.is_file():
            try:
                current_owner = json.loads(lock.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                current_owner = {}
            if current_owner.get("pid") == os.getpid() and current_owner.get("run_id") == run_id:
                lock.unlink()


def _partition_relative(source: str, lawd_code: str, month: str) -> Path:
    return Path("partitions") / source / lawd_code / f"{month}.json"


def _fetch_partition(
    *, source: str, lawd_code: str, month: str, service_key: str, storage_root: Path,
    requester: Callable[[str, int], str], timeout: int, retry_attempts: int,
    retry_backoff: float, delay: float, maximum_pages: int,
    sleep: Callable[[float], None],
) -> dict:
    pages: List[dict] = []
    issues: List[str] = []
    fingerprints: Counter = Counter()
    fingerprint_dates: Dict[str, Optional[str]] = {}
    fingerprint_source_reported: Dict[str, Optional[str]] = {}
    missing_fields: Counter = Counter()
    schema_ids = set()
    normalized_count = 0
    invalid_normalized_count = 0
    totals: List[int] = []
    page_content_seen = set()
    page = 1
    while page <= maximum_pages:
        text = None
        error = None
        attempts = 0
        for attempts in range(1, retry_attempts + 1):
            try:
                text = requester(_request_url(source, lawd_code, month, page, service_key), timeout)
                error = None
                break
            except Exception as exc:  # Network and source protocol failures share bounded retry handling.
                error = _redacted_error(exc, service_key)
                if attempts < retry_attempts:
                    sleep(retry_backoff * (2 ** (attempts - 1)))
        if text is None:
            issues.append(f"page_{page}_failed_after_{attempts}_attempts: {error}")
            break
        text, response_redacted = _redact_secret_text(text, service_key)
        response_bytes = text.encode("utf-8")
        object_sha, object_path = _store_object(storage_root, "xml", response_bytes)
        try:
            items, parsed_total, _ = parse_xml_items(text)
            total = _page_total(text)
            if parsed_total != total:
                raise ValueError("inconsistent totalCount parsing")
        except Exception as exc:
            issues.append(f"page_{page}_invalid_response: {_redacted_error(exc, service_key)}")
            pages.append({"page": page, "attempts": attempts, "object_sha256": object_sha,
                          "object_path": object_path.relative_to(storage_root).as_posix(), "status": "invalid"})
            break
        totals.append(total)
        canonical_items = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_sha = hashlib.sha256(canonical_items.encode("utf-8")).hexdigest()
        duplicate_page_content = bool(items) and content_sha in page_content_seen
        if duplicate_page_content:
            issues.append(f"page_{page}_duplicates_prior_page_content")
        page_content_seen.add(content_sha)
        pages.append({
            "page": page,
            "attempts": attempts,
            "status": "received",
            "response_item_count": len(items),
            "source_total_count": total,
            "object_sha256": object_sha,
            "object_path": object_path.relative_to(storage_root).as_posix(),
            "content_sha256": content_sha,
            "credential_text_redacted": response_redacted,
        })
        for raw in items:
            schema_ids.add(hashlib.sha256(json.dumps(sorted(raw), ensure_ascii=False).encode("utf-8")).hexdigest())
            fingerprint = _record_fingerprint(source, lawd_code, raw)
            fingerprints[fingerprint] += 1
            normalized = normalize_item(source, raw, lawd_code, fingerprints[fingerprint] - 1)
            if normalized is None:
                invalid_normalized_count += 1
                fingerprint_dates.setdefault(fingerprint, None)
            else:
                normalized_count += 1
                fingerprint_dates.setdefault(fingerprint, str(normalized["deal_date"]))
            fingerprint_source_reported.setdefault(fingerprint, _source_reported_at(raw))
            for field, missing in _raw_quality(raw).items():
                if missing:
                    missing_fields[field] += 1
        collected = sum(item.get("response_item_count", 0) for item in pages)
        if total == 0:
            if items:
                issues.append("zero_total_with_items")
            break
        if not items:
            issues.append("unexpected_empty_page")
            break
        if collected >= total:
            break
        page += 1
        sleep(delay)
    if page > maximum_pages:
        issues.append("maximum_pages_exceeded")
    if len(set(totals)) > 1:
        issues.append("source_total_changed_during_pagination")
    source_total = totals[-1] if totals else None
    observed_items = sum(item.get("response_item_count", 0) for item in pages)
    if source_total is not None and observed_items != source_total:
        issues.append("observed_item_count_does_not_match_total")
    status = "complete" if not issues and source_total is not None else "incomplete"
    return {
        "schema_version": 1,
        "source": source,
        "source_id": SOURCE_IDS[source],
        "source_landing_url": SOURCE_LANDING_URLS[source],
        "lawd_code": lawd_code,
        "contract_month": month,
        "status": status,
        "issues": issues,
        "pages": pages,
        "source_total_count": source_total,
        "observed_item_count": observed_items,
        "normalized_item_count": normalized_count,
        "invalid_normalized_item_count": invalid_normalized_count,
        "schema_ids": sorted(schema_ids),
        "missing_field_counts": dict(sorted(missing_fields.items())),
        "exact_content_counts": dict(sorted(fingerprints.items())),
        "fingerprint_contract_dates": dict(sorted(fingerprint_dates.items())),
        "fingerprint_source_reported_at": dict(sorted(fingerprint_source_reported.items())),
        "exact_duplicate_extra_count": sum(count - 1 for count in fingerprints.values()),
        "query": {"source": source, "lawd_code": lawd_code, "contract_month": month, "page_size": 1000},
    }


def collect_rtms_observation(
    *, config: Mapping[str, object], storage_root: Path, service_key: str,
    code_commit: str, code_dirty: bool = False, observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None, resume: bool = False,
    data_label: str = "observed",
    requester: Optional[Callable[[str, int], str]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if not service_key.strip():
        raise ValueError("DATA_GO_KR_SERVICE_KEY is required")
    if not re.fullmatch(r"[0-9a-f]{40}", code_commit):
        raise ValueError("code_commit must be a full lowercase Git SHA")
    if data_label not in {"observed", "synthetic"}:
        raise ValueError("data_label must be observed or synthetic")
    current_time = observed_at or datetime.now(timezone.utc)
    observed = _timestamp(current_time)
    chosen_run_id = run_id or current_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not RUN_ID_PATTERN.fullmatch(chosen_run_id):
        raise ValueError("run_id contains unsupported characters")
    storage_root = storage_root.resolve()
    run_dir = storage_root / "runs" / chosen_run_id
    config_digest = hashlib.sha256(_json_bytes(config)).hexdigest()
    actual_requester = requester
    if actual_requester is None:
        from .rtms import _request
        actual_requester = _request
    with _collector_lock(storage_root, chosen_run_id):
        if run_dir.exists():
            if not resume:
                raise FileExistsError(f"observation run already exists: {run_dir}")
            state_path = run_dir / "run.json"
            if not state_path.is_file():
                raise ValueError("cannot resume a run without run.json")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status") == "finalized":
                raise ValueError("finalized observation runs are immutable")
            if state.get("config_sha256") != config_digest:
                raise ValueError("resume config does not match the original run")
            if state.get("data_label", "observed") != data_label:
                raise ValueError("resume data_label does not match the original run")
        else:
            run_dir.mkdir(parents=True)
            state = {
                "schema_version": 1,
                "observation_version": OBSERVATION_VERSION,
                "run_id": chosen_run_id,
                "status": "running",
                "started_at": observed,
                "observed_at": observed,
                "display_timezone": config["display_timezone"],
                "data_label": data_label,
                "code_commit": code_commit,
                "code_dirty": bool(code_dirty),
                "config_sha256": config_digest,
                "config": dict(config),
            }
            _write_new_json(run_dir / "run.json", state)
        partitions = []
        for source in config["sources"]:  # type: ignore[index]
            for lawd_code in config["lawd_codes"]:  # type: ignore[index]
                for month in config["contract_months"]:  # type: ignore[index]
                    relative = _partition_relative(str(source), str(lawd_code), str(month))
                    target = run_dir / relative
                    if target.is_file():
                        existing = json.loads(target.read_text(encoding="utf-8"))
                        if existing.get("status") == "complete":
                            partitions.append(existing)
                            continue
                    result = _fetch_partition(
                        source=str(source), lawd_code=str(lawd_code), month=str(month),
                        service_key=service_key, storage_root=storage_root,
                        requester=actual_requester,
                        timeout=int(config["request_timeout_seconds"]),
                        retry_attempts=int(config["retry_attempts"]),
                        retry_backoff=float(config["retry_backoff_seconds"]),
                        delay=float(config["delay_seconds"]),
                        maximum_pages=int(config["maximum_pages_per_partition"]), sleep=sleep,
                    )
                    if target.exists():
                        _replace_json(target, result)
                    else:
                        _write_new_json(target, result)
                    partitions.append(result)
                    sleep(float(config["delay_seconds"]))
        complete_count = sum(item["status"] == "complete" for item in partitions)
        raw_count = sum(int(item["observed_item_count"]) for item in partitions)
        invalid_count = sum(int(item["invalid_normalized_item_count"]) for item in partitions)
        finalized = {
            **state,
            "status": "finalized" if complete_count == len(partitions) else "incomplete",
            "finished_at": _timestamp(),
            "planned_partition_count": len(partitions),
            "complete_partition_count": complete_count,
            "incomplete_partition_count": len(partitions) - complete_count,
            "raw_record_count": raw_count,
            "normalized_record_count": sum(int(item["normalized_item_count"]) for item in partitions),
            "collection_succeeded": True,
            "version_complete": complete_count == len(partitions),
            "audit_passed": complete_count == len(partitions) and invalid_count == 0,
            "public_release_eligible": False,
            "public_release_reason": "raw observations require aggregate privacy and snapshot admission",
        }
        _replace_json(run_dir / "run.json", finalized)
        return finalized


def observe_annual_archives(
    *, input_dir: Path, storage_root: Path, run_id: str, code_commit: str,
    observed_at: Optional[datetime] = None,
) -> dict:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id contains unsupported characters")
    if not re.fullmatch(r"[0-9a-f]{40}", code_commit):
        raise ValueError("code_commit must be a full lowercase Git SHA")
    input_dir = input_dir.resolve()
    storage_root = storage_root.resolve()
    run_dir = storage_root / "annual-runs" / run_id
    with _collector_lock(storage_root, "annual-" + run_id):
        sources = []
        for source in sorted(input_dir.glob("seoul-rents-*.zip")):
            if not source.is_file() or source.is_symlink():
                continue
            match = re.fullmatch(r"seoul-rents-(\d{4})\.zip", source.name)
            if not match:
                continue
            if not zipfile.is_zipfile(source):
                raise ValueError(f"invalid annual ZIP: {source.name}")
            sources.append((source, int(match.group(1))))
        if not sources:
            raise ValueError("no seoul-rents-YYYY.zip files found")
        run_dir.mkdir(parents=True, exist_ok=False)
        files = []
        for source, file_year in sources:
            payload = source.read_bytes()
            digest, target = _store_object(storage_root, "zip", payload)
            files.append({
                "file_year": file_year,
                "source_file": source.name,
                "sha256": digest,
                "bytes": len(payload),
                "object_path": target.relative_to(storage_root).as_posix(),
            })
        result = {
            "schema_version": 1,
            "observation_version": ANNUAL_OBSERVATION_VERSION,
            "run_id": run_id,
            "observed_at": _timestamp(observed_at),
            "source_id": "seoul-rental-price-files",
            "source_landing_url": SEOUL_ANNUAL_LANDING_URL,
            "code_commit": code_commit,
            "files": files,
            "collection_succeeded": True,
            "version_complete": True,
            "audit_passed": False,
            "public_release_eligible": False,
            "limitations": [
                "This records file observations only; annual row-level audit is separate.",
                "Repeated content hashes record repeated observation, not a new data revision.",
            ],
        }
        _write_new_json(run_dir / "run.json", result)
        return result


def _load_runs(storage_root: Path) -> List[Tuple[dict, Path]]:
    result = []
    for path in sorted((storage_root / "runs").glob("*/run.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        if run.get("observation_version") == OBSERVATION_VERSION and run.get("status") in {"running", "finalized", "incomplete"}:
            result.append((run, path.parent))
    result.sort(key=lambda item: _parse_timestamp(item[0]["observed_at"]))
    return result


def _partition_map(run_dir: Path) -> Dict[Tuple[str, str, str], dict]:
    result = {}
    for path in run_dir.glob("partitions/*/*/*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        key = (item["source"], item["lawd_code"], item["contract_month"])
        result[key] = item
    return result


def _full_calendar_months(start: datetime, end: datetime) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if (end.day, end.time()) < (start.day, start.time()):
        months -= 1
    return max(0, months)


def audit_rtms_versions(*, storage_root: Path, output_dir: Path, cadence_days: int = 7) -> dict:
    if not 1 <= cadence_days <= 31:
        raise ValueError("cadence_days must be between 1 and 31")
    output_dir.mkdir(parents=True, exist_ok=False)
    runs = _load_runs(storage_root.resolve())
    comparisons = []
    baselines = []
    seen_partitions = set()
    previous_complete: Dict[Tuple[str, str, str, str], Tuple[dict, dict]] = {}
    run_summaries = []
    total_missing_fields: Counter = Counter()
    total_raw_records = 0
    total_exact_duplicate_extra = 0
    total_source_reported = 0
    complete_run_times = []
    real_complete_run_times = []
    for run, run_dir in runs:
        if run.get("version_complete"):
            complete_run_times.append(_parse_timestamp(run["observed_at"]))
            if run.get("data_label", "observed") == "observed":
                real_complete_run_times.append(_parse_timestamp(run["observed_at"]))
        partition_values = list(_partition_map(run_dir).values())
        run_missing: Counter = Counter()
        for item in partition_values:
            run_missing.update({key: int(value) for key, value in item.get("missing_field_counts", {}).items()})
            total_exact_duplicate_extra += int(item.get("exact_duplicate_extra_count", 0))
            total_source_reported += sum(
                value is not None for value in item.get("fingerprint_source_reported_at", {}).values()
            )
        total_missing_fields.update(run_missing)
        total_raw_records += int(run.get("raw_record_count", 0))
        run_summaries.append({
            "run_id": run["run_id"],
            "status": run.get("status"),
            "observed_at": run["observed_at"],
            "data_label": run.get("data_label", "observed"),
            "collection_succeeded": bool(run.get("collection_succeeded")),
            "version_complete": bool(run.get("version_complete")),
            "audit_passed": bool(run.get("audit_passed")),
            "public_release_eligible": bool(run.get("public_release_eligible")),
            "planned_partitions": int(run.get("planned_partition_count", (
                len(run.get("config", {}).get("sources", []))
                * len(run.get("config", {}).get("lawd_codes", []))
                * len(run.get("config", {}).get("contract_months", []))
            ))),
            "complete_partitions": int(run.get("complete_partition_count", sum(
                item.get("status") == "complete" for item in partition_values
            ))),
            "raw_records": int(run.get("raw_record_count", 0)),
            "missing_field_counts": dict(sorted(run_missing.items())),
        })
    missed_runs = 0
    for prior, current in zip(real_complete_run_times, real_complete_run_times[1:]):
        missed_runs += max(0, math.floor((current - prior).total_seconds() / (cadence_days * 86400)) - 1)
    for run, run_dir in runs:
        if run.get("status") == "running":
            continue
        current_map = _partition_map(run_dir)
        for key, item in current_map.items():
            if item.get("status") != "complete":
                continue
            label = str(run.get("data_label", "observed"))
            history_key = (*key, label)
            if history_key not in previous_complete:
                seen_partitions.add(history_key)
                previous_complete[history_key] = (run, item)
                baselines.append({
                    "run_id": run["run_id"],
                    "data_label": label,
                    "source": key[0],
                    "lawd_code": key[1],
                    "contract_month": key[2],
                    "baseline_existing_occurrences": int(item.get("observed_item_count", 0)),
                    "included_in_discovery_delay": False,
                })
                continue
            prior_run, before = previous_complete[history_key]
            after = item
            before_counts = Counter({key: int(value) for key, value in before["exact_content_counts"].items()})
            after_counts = Counter({key: int(value) for key, value in after["exact_content_counts"].items()})
            added = after_counts - before_counts
            removed = before_counts - after_counts
            same_data_label = prior_run.get("data_label", "observed") == run.get("data_label", "observed")
            schema_stable = before.get("schema_ids") == after.get("schema_ids")
            prior_time = _parse_timestamp(prior_run["observed_at"])
            current_time = _parse_timestamp(run["observed_at"])
            new_dates = [after.get("fingerprint_contract_dates", {}).get(fingerprint) for fingerprint in added]
            upper_delays = []
            for value in new_dates:
                if value:
                    upper_delays.append((current_time.date() - date.fromisoformat(value)).days)
            added_count = sum(added.values())
            removed_count = sum(removed.values())
            denominator = max(sum(before_counts.values()), sum(after_counts.values()), 1)
            comparisons.append({
                "prior_run_id": prior_run["run_id"],
                "current_run_id": run["run_id"],
                "source": key[0],
                "lawd_code": key[1],
                "contract_month": key[2],
                "comparison_reliable": schema_stable and same_data_label,
                "schema_stable": schema_stable,
                "same_data_label": same_data_label,
                "data_label": run.get("data_label", "observed"),
                "baseline_existing": False,
                "prior_occurrences": sum(before_counts.values()),
                "current_occurrences": sum(after_counts.values()),
                "added_occurrences": added_count,
                "removed_occurrences": removed_count,
                "possible_revision_candidates": min(added_count, removed_count),
                "content_change_rate": round((added_count + removed_count) / denominator, 8),
                "first_seen_interval": {"after": _timestamp(prior_time), "on_or_before": _timestamp(current_time)},
                "system_discovery_upper_days": {
                    "count": len(upper_delays),
                    "minimum": min(upper_delays) if upper_delays else None,
                    "maximum": max(upper_delays) if upper_delays else None,
                },
                "limitations": [
                    "Exact-content changes do not prove a transaction revision without a stable source ID.",
                    "The first-seen interval measures this collector, not the source's first publication time.",
                ],
            })
            previous_complete[history_key] = (run, item)
    partition_total = sum(item["planned_partitions"] for item in run_summaries)
    partition_complete = sum(item["complete_partitions"] for item in run_summaries)
    real_span_days = 0
    real_span_full_months = 0
    if len(real_complete_run_times) >= 2:
        real_span_days = (real_complete_run_times[-1] - real_complete_run_times[0]).days
        real_span_full_months = _full_calendar_months(real_complete_run_times[0], real_complete_run_times[-1])
    latest_real_config = next(
        (run.get("config", {}) for run, _ in reversed(runs) if run.get("data_label", "observed") == "observed"),
        {},
    )
    minimum_months = latest_real_config.get("minimum_observation_months")
    target_months = latest_real_config.get("target_observation_months")
    reliable = [item for item in comparisons if item["comparison_reliable"]]
    reliable_change_denominator = sum(max(item["prior_occurrences"], item["current_occurrences"], 1) for item in reliable)
    reliable_change_numerator = sum(item["added_occurrences"] + item["removed_occurrences"] for item in reliable)
    discovery_values = [
        bound
        for item in reliable
        for bound in (item["system_discovery_upper_days"]["minimum"], item["system_discovery_upper_days"]["maximum"])
        if bound is not None
    ]
    report = {
        "schema_version": 1,
        "audit_version": "rtms-version-audit-v1",
        "generated_at": _timestamp(),
        "run_count": len(runs),
        "unfinished_run_count": sum(run.get("status") == "running" for run, _ in runs),
        "complete_run_count": len(complete_run_times),
        "real_observed_run_count": sum(run.get("data_label", "observed") == "observed" for run, _ in runs),
        "synthetic_run_count": sum(run.get("data_label") == "synthetic" for run, _ in runs),
        "actual_observation_span_days": real_span_days,
        "actual_observation_full_calendar_months": real_span_full_months,
        "actual_observation_span_months_approx": round(real_span_days / 30.4375, 3),
        "minimum_observation_months": minimum_months,
        "target_observation_months": target_months,
        "minimum_continuous_duration_met": bool(
            isinstance(minimum_months, int) and real_span_full_months >= minimum_months and missed_runs == 0
        ),
        "target_continuous_duration_met": bool(
            isinstance(target_months, int) and real_span_full_months >= target_months and missed_runs == 0
        ),
        "cadence_days": cadence_days,
        "estimated_missed_scheduled_runs": missed_runs,
        "collection_success_rate": round(
            sum(item["collection_succeeded"] for item in run_summaries) / len(run_summaries), 8
        ) if run_summaries else None,
        "complete_version_rate": round(len(complete_run_times) / len(runs), 8) if runs else None,
        "partition_count": partition_total,
        "complete_partition_count": partition_complete,
        "partition_completeness_rate": round(partition_complete / partition_total, 8) if partition_total else None,
        "baseline_partition_count": len(seen_partitions),
        "comparison_count": len(comparisons),
        "reliable_comparison_count": sum(item["comparison_reliable"] for item in comparisons),
        "added_occurrences": sum(item["added_occurrences"] for item in comparisons if item["comparison_reliable"]),
        "removed_occurrences": sum(item["removed_occurrences"] for item in comparisons if item["comparison_reliable"]),
        "possible_revision_candidates": sum(item["possible_revision_candidates"] for item in reliable),
        "aggregate_content_change_rate": round(reliable_change_numerator / reliable_change_denominator, 8) if reliable_change_denominator else None,
        "system_discovery_upper_days_observed_range": {
            "minimum": min(discovery_values) if discovery_values else None,
            "maximum": max(discovery_values) if discovery_values else None,
        },
        "raw_record_observations": total_raw_records,
        "exact_duplicate_extra_observations": total_exact_duplicate_extra,
        "exact_duplicate_observation_rate": round(total_exact_duplicate_extra / total_raw_records, 8) if total_raw_records else None,
        "source_reported_at_fingerprint_count": total_source_reported,
        "missing_field_counts": dict(sorted(total_missing_fields.items())),
        "missing_field_rates": {
            key: round(value / total_raw_records, 8) if total_raw_records else None
            for key, value in sorted(total_missing_fields.items())
        },
        "states": {
            "collection_succeeded": "the collector finalized a run manifest",
            "version_complete": "every planned source/district/month partition matched its source total",
            "audit_passed": "the complete version also had no normalization-invalid records",
            "public_release_eligible": "always false here; aggregate privacy and snapshot admission are separate",
        },
        "metric_definitions": {
            "collection_success_rate": "finalized run manifests divided by attempted runs visible in storage",
            "complete_version_rate": "complete versions divided by all visible attempted runs, including unfinished runs",
            "partition_completeness_rate": "complete partitions divided by planned partitions in all visible attempted runs; unfinished plans use the saved resolved configuration",
            "content_change_rate": "added plus removed exact-content occurrences divided by the larger partition size",
            "possible_revision_candidates": "the smaller of added and removed occurrences in one comparable partition; this is a review queue count, not a confirmed revision",
            "estimated_missed_scheduled_runs": "whole cadence intervals absent between complete runs; unscheduled downtime before the first and after the last run is unknowable",
        },
        "limitations": [
            "Querying old contract months does not create historical observation time; duration uses actual successful run timestamps.",
            "Baseline records are excluded from discovery-lag estimates.",
            "Window exits and incomplete partitions are not interpreted as removals.",
            "Fingerprints represent exact source fields and multiplicity, not stable transaction identity.",
            "No risk score, safety claim, or official reporting-delay probability is produced.",
        ],
        "runs": run_summaries,
        "baselines": baselines,
        "comparisons": comparisons,
    }
    _write_new_json(output_dir / "report.json", report)
    lines = [
        "# RTMS 连续版本数据时效审计", "",
        f"真实完整运行：{len(real_complete_run_times)}；实际观察跨度：{real_span_days} 天；估算漏跑：{missed_runs}。", "",
        f"可见运行尝试：{len(runs)}；未结束批次：{report['unfinished_run_count']}。未结束可能正在执行或已中断，均不计为完整版本。估算漏跑只计算完整版本之间的间隔，不代表尾部没有失败或漏跑。", "",
        f"分区完整率：{partition_complete}/{partition_total}。可靠版本比较：{report['reliable_comparison_count']}/{len(comparisons)}。", "",
        "首轮记录只标为 baseline_existing，不用于估计迟报。first_seen 是本采集器的观察区间，不是官方首次发布时间。", "",
        "本报告只描述采集与内容变化，不批准公开发布，也不生成房源安全或押金风险评分。", "",
    ]
    with (output_dir / "SUMMARY.md").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return report
