from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


PUBLISHED_RECORD_FIELDS = {
    "seoul-rental-monthly.json": {
        "buildingUse", "contractMonth", "contractYear", "count", "depositMedianManwon",
        "depositP25Manwon", "depositP75Manwon", "guCode", "guName", "leaseType",
        "monthlyRentMedianManwon", "monthlyRentP25Manwon", "monthlyRentP75Manwon",
    },
    "rtms-sample.json": {
        "buildYear", "buildingName", "contractTerm", "dealDate", "depositManwon",
        "exclusiveAreaSqm", "floor", "guName", "id", "lawdCode", "leaseType",
        "legalDong", "monthlyRentManwon", "propertyType",
    },
    "rtms-summaries.json": {
        "byPropertyType", "count", "guName", "jeonseCount", "lawdCode", "maxDealDate",
        "medianJeonseDeposit", "medianMonthlyDeposit", "medianMonthlyRent", "minDealDate",
        "monthlyCount", "p25JeonseDeposit", "p25MonthlyRent", "p75JeonseDeposit",
        "p75MonthlyRent", "sampleCount",
    },
    "demographics.json": {
        "age", "femaleCount", "foreignCount", "guName", "koreanCount", "lawdCode",
        "maleCount", "totalResidents",
    },
    "market-regions.json": {
        "lawdCode", "sido", "cityKey", "guName", "zh", "ko", "en", "lat", "lng",
    },
    "seoul-gosiwon-registry.json": {
        "id", "name", "guName", "address", "roadAddress", "areaSqm", "floors", "reportedAt",
    },
}
PUBLISHED_RECORD_KEYS = {
    "seoul-rental-monthly.json": "records",
    "rtms-sample.json": "listings",
    "rtms-summaries.json": "districts",
    "demographics.json": "districts",
    "seoul-gosiwon-registry.json": "listings",
}
FORBIDDEN_PII_KEYS = {
    "nationality", "othercountries", "phone", "phonenumber", "mobile", "email",
    "representative", "representativename",
}
KOREAN_PHONE_PATTERN = (
    r"(?<![0-9A-Za-z])(?:01[016789][ -]?\d{3,4}[ -]?\d{4}"
    r"|0(?:2|[3-8]\d)[ -]?\d{3,4}[ -]?\d{4})(?![0-9A-Za-z])"
)


def validate_published_file(path: Path) -> list[str]:
    if path.name not in PUBLISHED_RECORD_FIELDS:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path}: cannot run schema/privacy validation: {exc}"]
    errors: list[str] = []
    records: object = payload
    record_key = PUBLISHED_RECORD_KEYS.get(path.name)
    if record_key:
        records = payload.get(record_key) if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return [f"{path}: published records must be a list"]
    allowed = PUBLISHED_RECORD_FIELDS[path.name]
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"{path}: record {index} must be an object")
            continue
        unexpected = sorted(set(record) - allowed)
        if unexpected:
            errors.append(f"{path}: record {index} has unexpected fields: {unexpected}")
        if path.name == "seoul-rental-monthly.json":
            year = record.get("contractYear")
            month = record.get("contractMonth")
            if not isinstance(year, int) or not isinstance(month, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month) or month[:4] != str(year):
                errors.append(f"{path}: record {index} has invalid contractMonth")
            if not isinstance(record.get("count"), int) or record["count"] < 10:
                errors.append(f"{path}: record {index} count must be at least 10")
            if not isinstance(record.get("guCode"), str) or not re.fullmatch(r"\d{5}", record["guCode"]):
                errors.append(f"{path}: record {index} has invalid guCode")
            if record.get("leaseType") not in {"monthly", "jeonse"}:
                errors.append(f"{path}: record {index} has invalid leaseType")
            for label, fields in (
                ("deposit", ("depositP25Manwon", "depositMedianManwon", "depositP75Manwon")),
                ("rent", ("monthlyRentP25Manwon", "monthlyRentMedianManwon", "monthlyRentP75Manwon")),
            ):
                values = [record.get(field) for field in fields]
                if not all(isinstance(value, (int, float)) and value >= 0 for value in values) or values != sorted(values):
                    errors.append(f"{path}: record {index} has invalid {label} quantiles")

    def walk(value: object, location: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if normalized in FORBIDDEN_PII_KEYS:
                    errors.append(f"{path}: forbidden PII key at {location}.{key}")
                walk(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}[{index}]")

    walk(payload, "$")
    serialized = json.dumps(payload, ensure_ascii=False)
    if re.search(KOREAN_PHONE_PATTERN, serialized):
        errors.append(f"{path}: Korean phone number pattern found")
    if path.name == "seoul-gosiwon-registry.json" and re.search(r"대표자|귀하", serialized):
        errors.append(f"{path}: representative detail found")
    return errors


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_record_count(path: Path) -> int | None:
    if path.suffix.lower() != ".json":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("listings", "districts", "records", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    by_city = payload.get("byCity")
    if isinstance(by_city, dict):
        unique: set[tuple[str, float | None, float | None]] = set()
        for city in by_city.values():
            if not isinstance(city, dict):
                continue
            lines = city.get("stationsByLine")
            if not isinstance(lines, dict):
                continue
            for stations in lines.values():
                if not isinstance(stations, list):
                    continue
                for station in stations:
                    if isinstance(station, dict):
                        unique.add((str(station.get("name", "")), station.get("lat"), station.get("lng")))
        return len(unique)
    return None


def file_entry(
    path: Path,
    relative_to: Path,
    source_id: str,
    source_url: str,
    secondary_source_ids: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path.relative_to(relative_to).as_posix(),
        "source_id": source_id,
        "source_url": source_url,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    if secondary_source_ids:
        entry["secondary_source_ids"] = secondary_source_ids
    count = infer_record_count(path)
    if count is not None:
        entry["record_count"] = count
    return entry


def validate_snapshot(
    snapshot_dir: Path,
    known_sources: set[str] | Mapping[str, Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"{snapshot_dir}: missing manifest.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{manifest_path}: invalid JSON: {exc}"]
    required_manifest = {
        "snapshot_id", "created_at", "data_label", "geography", "derivation",
        "input_repository", "input_commit", "input_files", "transformation", "limitations",
    }
    missing_manifest = sorted(required_manifest - manifest.keys())
    if missing_manifest:
        errors.append(f"{manifest_path}: missing manifest fields: {', '.join(missing_manifest)}")
    if manifest.get("schema_version") != 1:
        errors.append(f"{manifest_path}: schema_version must be 1")
    if not isinstance(manifest.get("limitations"), list) or not manifest.get("limitations"):
        errors.append(f"{manifest_path}: limitations must be a non-empty list")
    elif any(not isinstance(value, str) or not value.strip() for value in manifest["limitations"]):
        errors.append(f"{manifest_path}: limitations must contain non-empty strings")
    if manifest.get("data_label") not in {"observed", "derived_observed", "observed_and_derived_observed", "observed_derived_and_curated"}:
        errors.append(f"{manifest_path}: invalid data_label")
    for field in ("snapshot_id", "created_at", "geography", "derivation", "input_repository"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            errors.append(f"{manifest_path}: {field} must be a non-empty string")
    if not isinstance(manifest.get("transformation"), dict) or not manifest["transformation"]:
        errors.append(f"{manifest_path}: transformation must be a non-empty object")
    if not isinstance(manifest.get("input_commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("input_commit", "")):
        errors.append(f"{manifest_path}: input_commit must be a full Git SHA")
    input_files = manifest.get("input_files")
    if not isinstance(input_files, list) or not input_files:
        errors.append(f"{manifest_path}: input_files must be a non-empty list")
    else:
        for item in input_files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                errors.append(f"{manifest_path}: invalid input file entry")
                continue
            if item["path"].startswith("/") or ".." in Path(item["path"]).parts:
                errors.append(f"{manifest_path}: unsafe input path {item['path']!r}")
            if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
                errors.append(f"{manifest_path}: invalid input sha256 for {item['path']}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        errors.append(f"{manifest_path}: files must be a non-empty list")
        return errors
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            errors.append(f"{manifest_path}: non-object file entry")
            continue
        relative = entry.get("path")
        if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
            errors.append(f"{manifest_path}: unsafe path {relative!r}")
            continue
        if relative in seen:
            errors.append(f"{manifest_path}: duplicate path {relative}")
        seen.add(relative)
        source_id = entry.get("source_id")
        known_source_ids = set(known_sources)
        if source_id not in known_source_ids:
            errors.append(f"{manifest_path}: unknown source_id {source_id!r}")
        elif isinstance(known_sources, Mapping):
            catalog_source = known_sources[str(source_id)]
            allowed_source_urls = {catalog_source.get("landing_url")}
            historical_urls = catalog_source.get("historical_landing_urls", [])
            if isinstance(historical_urls, list):
                allowed_source_urls.update(
                    value for value in historical_urls if isinstance(value, str)
                )
            if entry.get("source_url") not in allowed_source_urls:
                errors.append(f"{manifest_path}: source_url mismatch for {source_id}")
            if catalog_source.get("status") != "seeded":
                errors.append(f"{manifest_path}: source {source_id} is not admitted as seeded")
        secondary = entry.get("secondary_source_ids", [])
        if not isinstance(secondary, list) or any(not isinstance(value, str) for value in secondary):
            errors.append(f"{manifest_path}: secondary_source_ids must be a string list")
        else:
            for value in secondary:
                if value not in known_source_ids:
                    errors.append(f"{manifest_path}: unknown secondary_source_id {value!r}")
                elif isinstance(known_sources, Mapping) and known_sources[value].get("status") != "seeded":
                    errors.append(f"{manifest_path}: secondary source {value} is not admitted as seeded")
            if source_id in secondary or len(secondary) != len(set(secondary)):
                errors.append(f"{manifest_path}: duplicate source attribution for {relative}")
        composition = entry.get("source_composition")
        if composition is not None:
            attributed = {source_id, *secondary}
            if not isinstance(composition, list) or not composition:
                errors.append(f"{manifest_path}: {relative} source_composition must be non-empty")
            else:
                composition_sources: list[str] = []
                composition_count = 0
                for part in composition:
                    if not isinstance(part, dict):
                        errors.append(f"{manifest_path}: {relative} has invalid source_composition entry")
                        continue
                    part_source = part.get("source_id")
                    part_count = part.get("record_count")
                    if part_source not in attributed:
                        errors.append(f"{manifest_path}: {relative} composition source is not attributed: {part_source!r}")
                    if not isinstance(part_count, int) or part_count < 1:
                        errors.append(f"{manifest_path}: {relative} composition record_count must be positive")
                    else:
                        composition_count += part_count
                    if isinstance(part_source, str):
                        composition_sources.append(part_source)
                if len(composition_sources) != len(set(composition_sources)):
                    errors.append(f"{manifest_path}: {relative} has duplicate composition sources")
                expected_count = entry.get("based_on_record_count", entry.get("record_count"))
                if isinstance(expected_count, int) and composition_count != expected_count:
                    errors.append(f"{manifest_path}: {relative} source_composition count mismatch")
        target = snapshot_dir / relative
        if target.is_symlink():
            errors.append(f"{target}: symlinks are forbidden")
            continue
        if not target.is_file():
            errors.append(f"{target}: file missing")
            continue
        actual = sha256_file(target)
        if entry.get("sha256") != actual:
            errors.append(f"{target}: sha256 mismatch")
        if entry.get("bytes") != target.stat().st_size:
            errors.append(f"{target}: byte count mismatch")
        actual_count = infer_record_count(target)
        if actual_count is None or entry.get("record_count") != actual_count:
            errors.append(f"{target}: record_count mismatch")
        errors.extend(validate_published_file(target))
        if target.name == "seoul-rental-monthly.json":
            minimum_cell_count = entry.get("minimum_cell_count")
            if not isinstance(minimum_cell_count, int) or minimum_cell_count < 10:
                errors.append(f"{manifest_path}: {relative} minimum_cell_count must be at least 10")
            else:
                payload = json.loads(target.read_text(encoding="utf-8"))
                if any(
                    not isinstance(record.get("count"), int)
                    or record["count"] < minimum_cell_count
                    for record in payload.get("records", [])
                    if isinstance(record, dict)
                ):
                    errors.append(f"{manifest_path}: {relative} has records below minimum_cell_count")
        required_file = {"data_label", "geography", "temporal_coverage", "usage", "limitations"}
        missing_file = sorted(required_file - entry.keys())
        if missing_file:
            errors.append(f"{manifest_path}: {relative} missing fields: {', '.join(missing_file)}")
        if entry.get("data_label") not in {"observed", "derived_observed", "curated_reference"}:
            errors.append(f"{manifest_path}: {relative} has invalid data_label")
        if not isinstance(entry.get("geography"), str) or not entry["geography"].strip():
            errors.append(f"{manifest_path}: {relative} geography must be a non-empty string")
        if not isinstance(entry.get("usage"), str) or not entry["usage"].strip():
            errors.append(f"{manifest_path}: {relative} usage must be a non-empty string")
        if not isinstance(entry.get("temporal_coverage"), dict) or not entry["temporal_coverage"]:
            errors.append(f"{manifest_path}: {relative} temporal_coverage must be a non-empty object")
        if entry.get("data_label") == "curated_reference":
            field_provenance = entry.get("field_provenance")
            if not isinstance(field_provenance, list) or not field_provenance:
                errors.append(f"{manifest_path}: {relative} curated_reference requires field_provenance")
        limitations = entry.get("limitations")
        if not isinstance(limitations, list) or not limitations or any(
            not isinstance(value, str) or not value.strip() for value in limitations
        ):
            errors.append(f"{manifest_path}: {relative} limitations must contain non-empty strings")
    actual_files = {
        item.relative_to(snapshot_dir).as_posix()
        for item in snapshot_dir.rglob("*")
        if item.is_file() and item.name != "manifest.json"
    }
    unlisted = sorted(actual_files - seen)
    if unlisted:
        errors.append(f"{manifest_path}: unlisted files: {', '.join(unlisted)}")
    return errors
