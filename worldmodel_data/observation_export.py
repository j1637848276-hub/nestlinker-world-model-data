"""Local-only normalized export from finalized, hash-checked RTMS responses."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .manifest import sha256_file
from .observation import RUN_ID_PATTERN, _record_fingerprint, _source_reported_at, _write_new_json
from .rtms import _number, normalize_item, parse_xml_items


def export_observation(storage_root, run_id, output_dir):
    root = Path(storage_root).resolve()
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("invalid run_id")
    run = root / "runs" / run_id
    if run.parent != root / "runs":
        raise ValueError("run_id must identify a direct run directory")
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    if state.get("status") != "finalized" or not state.get("version_complete") or not state.get("audit_passed"):
        raise ValueError("export requires a finalized, complete, audit-passed version")
    output = Path(output_dir).resolve()
    # Operational exports are never accepted under the public snapshots tree.
    if "data" not in output.parts or not any(
        output.parts[i:i + 2] in (("data", "raw"), ("data", "work"))
        for i in range(len(output.parts) - 1)
    ):
        raise ValueError("local exports must be under data/raw or data/work")
    output.mkdir(parents=True, exist_ok=False)
    target = output / "records.jsonl"
    count = 0
    by_source, by_month, missing = Counter(), Counter(), Counter()
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        for path in sorted((run / "partitions").rglob("*.json")):
            partition = json.loads(path.read_text(encoding="utf-8"))
            if partition["status"] != "complete":
                raise ValueError("incomplete partition cannot be exported")
            occurrences, partition_count = Counter(), 0
            for page in partition["pages"]:
                raw_path = (root / page["object_path"]).resolve()
                if root not in raw_path.parents or sha256_file(raw_path) != page["object_sha256"]:
                    raise ValueError("response object path or hash mismatch")
                items, _, _ = parse_xml_items(raw_path.read_text(encoding="utf-8"))
                for raw in items:
                    fingerprint = _record_fingerprint(partition["source"], partition["lawd_code"], raw)
                    normalized = normalize_item(partition["source"], raw, partition["lawd_code"], occurrences[fingerprint])
                    occurrences[fingerprint] += 1
                    if normalized is None:
                        raise ValueError("normalization failed; export not finalized")
                    exclusive = normalized["exclusive_area_sqm"]
                    total_floor = _number(raw.get("totalFloorAr", ""))
                    normalized.update(
                        contract_date=normalized["deal_date"],
                        source_reported_at=_source_reported_at(raw),
                        observed_at=state["observed_at"], first_seen_at=None,
                        first_seen_status="not_compiled_use_version_audit",
                        run_id=run_id, content_fingerprint=fingerprint,
                        reported_area_sqm=exclusive if exclusive is not None else total_floor,
                        reported_area_basis="exclusive" if exclusive is not None else (
                            "source_totalFloorAr" if total_floor is not None else None),
                    )
                    for field in ("exclusive_area_sqm", "reported_area_sqm", "floor", "contract_term", "source_reported_at"):
                        if normalized.get(field) is None:
                            missing[field] += 1
                    handle.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n")
                    count += 1
                    partition_count += 1
                    by_source[partition["source"]] += 1
                    by_month[partition["contract_month"]] += 1
            if partition_count != partition["normalized_item_count"]:
                raise ValueError("partition record count mismatch")
        if count != state["normalized_record_count"]:
            raise ValueError("run record count mismatch")
    summary = {"schema_version": 1, "export_version": "rtms-local-export-v1",
               "created_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
               "input_manifest_sha256": sha256_file(run / "run.json"),
               "export_source_sha256": sha256_file(Path(__file__)),
               "normalizer_source_sha256": sha256_file(Path(__file__).with_name("rtms.py")),
               "record_count": count, "counts_by_source": dict(by_source),
               "counts_by_contract_month": dict(by_month), "missing_field_counts": dict(missing),
               "files": [{"path": "records.jsonl", "sha256": sha256_file(target), "bytes": target.stat().st_size}],
               "public_release_eligible": False,
               "limitations": ["Local operational export, not public admission.",
                               "IDs/fingerprints are content identities, not stable transaction IDs.",
                               "observed_at is the run observation timestamp, not official publication time.",
                               "first_seen_at is intentionally null; compile from comparable version audit.",
                               "source_totalFloorAr is not treated as exclusive area.",
                               "Missing optional fields remain null; counts retain source multiplicity."]}
    _write_new_json(output / "manifest.json", summary)
    return summary
