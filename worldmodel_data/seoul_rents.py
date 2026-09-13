from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .manifest import file_entry, sha256_file


MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
REQUIRED_COLUMNS = {
    "접수년도", "자치구코드", "자치구명", "계약일", "전월세구분", "보증금(만원)",
    "임대료(만원)", "건물용도",
}
LEASE_TYPES = {"월세": "monthly", "전세": "jeonse"}
SOURCE_ID = "seoul-rental-price-files"
SOURCE_URL = "https://data.seoul.go.kr/dataList/OA-21276/A/1/datasetView.do"
INPUT_REPOSITORY = "https://github.com/sunruize93-cmyk/nestlinker-world-model-data"


def _quantile(values: Iterable[int], probability: float) -> int | float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile of an empty sequence")
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return int(result) if result.is_integer() else round(result, 2)


def _rows_from_archive(path: Path) -> Iterable[dict[str, str]]:
    # Lazy import avoids the audit module's constants import cycle.
    from .history_audit import _encoding

    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{path}: archive must be a regular file")
    with zipfile.ZipFile(path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith((".csv", ".txt")):
            raise ValueError(f"{path}: expected exactly one CSV or TXT file")
        if members[0].file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"{path}: uncompressed CSV exceeds safety limit")
        encoding = _encoding(archive, members[0])
        with archive.open(members[0]) as binary:
            with io.TextIOWrapper(binary, encoding=encoding, newline="") as text:
                sample = text.read(65536)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            delimiter = ","
        with archive.open(members[0]) as binary:
            with io.TextIOWrapper(binary, encoding=encoding, newline="") as text:
                reader = csv.DictReader(text, delimiter=delimiter, strict=True)
                if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                    raise ValueError(f"{path}: empty or duplicate headers")
                missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
                if missing:
                    raise ValueError(f"{path}: missing columns: {sorted(missing)}")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"{path}: row width mismatch")
                    yield row


def build_monthly_aggregates(
    archives: Mapping[int, Path], *, minimum_group_count: int = 1,
    exclude_receipt_year_mismatch: bool = True,
) -> dict[str, object]:
    """Compile annual Seoul rental CSV archives into privacy-minimal month bands."""
    grouped: dict[tuple[int, str, str, str, str, str], list[tuple[int, int]]] = defaultdict(list)
    source_rows = 0
    excluded_wrong_year = 0
    excluded_receipt_year_mismatch = 0
    included_receipt_year_mismatch = 0
    excluded_receipt_by_month: dict[str, int] = defaultdict(int)
    excluded_invalid_rows = 0
    invalid_reasons: dict[str, int] = defaultdict(int)
    archive_audits = []
    for expected_year, path in sorted(archives.items()):
        archive_rows = 0
        duplicate_rows = 0
        seen_rows: set[bytes] = set()
        for row in _rows_from_archive(path):
            source_rows += 1
            archive_rows += 1
            identity = repr(tuple(row.items())).encode("utf-8")
            digest = hashlib.blake2b(identity, digest_size=16).digest()
            if digest in seen_rows:
                duplicate_rows += 1
            else:
                seen_rows.add(digest)
            contract_date = (row.get("계약일") or "").strip()
            if len(contract_date) != 8 or not contract_date.isdigit():
                excluded_invalid_rows += 1
                invalid_reasons["invalid_date_format"] += 1
                continue
            contract_year = int(contract_date[:4])
            if contract_year != expected_year:
                excluded_wrong_year += 1
                continue
            receipt_year = (row.get("접수년도") or "").strip()
            if not receipt_year.isdigit():
                excluded_invalid_rows += 1
                invalid_reasons["invalid_receipt_year"] += 1
                continue
            if int(receipt_year) != contract_year:
                excluded_receipt_by_month[f"{contract_date[:4]}-{contract_date[4:6]}"] += 1
                if exclude_receipt_year_mismatch:
                    excluded_receipt_year_mismatch += 1
                    continue
                included_receipt_year_mismatch += 1
            try:
                date(contract_year, int(contract_date[4:6]), int(contract_date[6:8]))
            except ValueError:
                excluded_invalid_rows += 1
                invalid_reasons["invalid_date_value"] += 1
                continue
            lease_type = LEASE_TYPES.get((row.get("전월세구분") or "").strip())
            if not lease_type:
                excluded_invalid_rows += 1
                invalid_reasons["invalid_lease_type"] += 1
                continue
            try:
                deposit = int((row.get("보증금(만원)") or "").replace(",", "").strip())
                rent = int((row.get("임대료(만원)") or "").replace(",", "").strip())
            except ValueError:
                excluded_invalid_rows += 1
                invalid_reasons["invalid_amount"] += 1
                continue
            if deposit < 0 or rent < 0:
                excluded_invalid_rows += 1
                invalid_reasons["negative_amount"] += 1
                continue
            gu_code = (row.get("자치구코드") or "").strip()
            gu_name = (row.get("자치구명") or "").strip()
            building_use = (row.get("건물용도") or "").strip()
            if not (len(gu_code) == 5 and gu_code.isdigit() and gu_name and building_use):
                excluded_invalid_rows += 1
                invalid_reasons["invalid_group_key"] += 1
                continue
            key = (
                contract_year,
                f"{contract_date[:4]}-{contract_date[4:6]}",
                gu_code,
                gu_name,
                building_use,
                lease_type,
            )
            grouped[key].append((deposit, rent))
        duplicate_ratio = duplicate_rows / archive_rows if archive_rows else 0.0
        archive_audits.append({
            "contractYear": expected_year,
            "sourceRows": archive_rows,
            "exactDuplicateRows": duplicate_rows,
            "exactDuplicateRatio": round(duplicate_ratio, 6),
        })
        if duplicate_ratio > 0.01:
            raise ValueError(
                f"{path}: systemic exact duplicates ({duplicate_rows}/{archive_rows}); "
                "archive must be quarantined because multiplicity cannot be resolved"
            )
    records = []
    suppressed_group_count = 0
    suppressed_contract_count = 0
    for key, values in sorted(grouped.items()):
        if len(values) < minimum_group_count:
            suppressed_group_count += 1
            suppressed_contract_count += len(values)
            continue
        deposits = [value[0] for value in values]
        rents = [value[1] for value in values]
        year, month, gu_code, gu_name, building_use, lease_type = key
        records.append({
            "contractYear": year,
            "contractMonth": month,
            "guCode": gu_code,
            "guName": gu_name,
            "buildingUse": building_use,
            "leaseType": lease_type,
            "count": len(values),
            "depositP25Manwon": _quantile(deposits, 0.25),
            "depositMedianManwon": _quantile(deposits, 0.5),
            "depositP75Manwon": _quantile(deposits, 0.75),
            "monthlyRentP25Manwon": _quantile(rents, 0.25),
            "monthlyRentMedianManwon": _quantile(rents, 0.5),
            "monthlyRentP75Manwon": _quantile(rents, 0.75),
        })
    if not records:
        raise ValueError("no eligible rental records remained after validation")
    return {
        "schemaVersion": 1,
        "source": SOURCE_ID,
        "license": "Korea Open Government License Type 1 (attribution)",
        "sourceRowCount": source_rows,
        "excludedWrongContractYear": excluded_wrong_year,
        "excludedReceiptYearMismatch": excluded_receipt_year_mismatch,
        "includedReceiptYearMismatchForSensitivity": included_receipt_year_mismatch,
        "excludedReceiptYearMismatchByContractMonth": dict(sorted(excluded_receipt_by_month.items())),
        "excludedInvalidRows": excluded_invalid_rows,
        "excludedInvalidRowsByReason": dict(sorted(invalid_reasons.items())),
        "minimumPublishedGroupCount": minimum_group_count,
        "suppressedGroupCount": suppressed_group_count,
        "suppressedContractCount": suppressed_contract_count,
        "archiveAudits": archive_audits,
        "records": records,
    }


def publish_monthly_snapshot(
    archives: Mapping[int, Path],
    snapshot_dir: Path,
    *,
    created_at: str,
    input_commit: str,
    acquisitions: Mapping[int, Mapping[str, object]],
    audit_dir: Path | None = None,
) -> Path:
    """Publish immutable aggregates with source hashes and no property-level rows."""
    if snapshot_dir.exists():
        raise ValueError(f"published snapshot is immutable: {snapshot_dir}")
    if len(input_commit) != 40 or any(ch not in "0123456789abcdef" for ch in input_commit):
        raise ValueError("input_commit must be a full lowercase Git SHA")
    years = tuple(sorted(archives))
    if not years or any(later != earlier + 1 for earlier, later in zip(years, years[1:])):
        raise ValueError("archive years must be consecutive")
    if years[0] < 2022 and audit_dir is None:
        raise ValueError("historical TXT backfill requires hash-bound audit reports")
    admission_audits = []
    for year, path in sorted(archives.items()):
        acquisition = acquisitions.get(year)
        if acquisition is None or acquisition.get("sha256") != sha256_file(path) or acquisition.get("bytes") != path.stat().st_size:
            raise ValueError(f"acquisition record does not match archive for {year}")
        if audit_dir is not None:
            report_path = audit_dir / f"{year}.audit.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report.get("year") != year or report.get("complete") is not True
                    or report.get("status") not in ("needs_review", "checks_passed_pending_review")
                    or report.get("sha256") != sha256_file(path)
                    or report.get("bytes") != path.stat().st_size
                    or report.get("missing_required_columns")
                    or not report.get("source_rows")):
                raise ValueError(f"{year}: incomplete, mismatched or quarantined audit")
            admission_audits.append({"file_year": year, "audit_sha256": sha256_file(report_path),
                                     "source_rows": report["source_rows"], "source_sha256": report["sha256"]})
    payload = build_monthly_aggregates(archives, minimum_group_count=10)
    if admission_audits:
        expected_counts = {item["file_year"]: item["source_rows"] for item in admission_audits}
        if any(item["sourceRows"] != expected_counts[item["contractYear"]]
               for item in payload["archiveAudits"]):
            raise ValueError("aggregation source-row counts differ from bound audits")
    published_rows = sum(int(record["count"]) for record in payload["records"])
    accounted_rows = (published_rows + payload["suppressedContractCount"]
                      + payload["excludedWrongContractYear"]
                      + payload["excludedReceiptYearMismatch"] + payload["excludedInvalidRows"])
    if accounted_rows != payload["sourceRowCount"]:
        raise ValueError("source-row conservation failed")
    payload["generatedAt"] = created_at
    payload["contractYears"] = list(years)
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{snapshot_dir.name}-", dir=snapshot_dir.parent) as staging:
        staging_dir = Path(staging)
        data_path = staging_dir / "seoul-rental-monthly.json"
        with data_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        entry = file_entry(data_path, staging_dir, SOURCE_ID, SOURCE_URL)
        entry.update({
            "data_label": "derived_observed",
            "geography": "Seoul, 25 districts",
            "temporal_coverage": {"start": f"{years[0]}-01", "end": f"{years[-1]}-12"},
            "usage": "historical_time_sliced_distribution",
            "based_on_record_count": sum(int(record["count"]) for record in payload["records"]),
            "minimum_cell_count": 10,
            "source_columns_used": {
                "접수년도": "annual availability guard",
                "자치구코드": "guCode",
                "자치구명": "guName",
                "계약일": "contractYear and contractMonth",
                "전월세구분": "leaseType",
                "보증금(만원)": "deposit quantiles",
                "임대료(만원)": "monthly-rent quantiles",
                "건물용도": "buildingUse",
            },
            "field_dictionary": {
                "contractYear": "calendar year of the reported contract date",
                "contractMonth": "calendar month of the reported contract date",
                "guCode": "five-digit Seoul district code from the source",
                "guName": "Seoul district name from the source",
                "buildingUse": "source housing-use category",
                "leaseType": "monthly or jeonse",
                "count": "source rows in the aggregate after validation; minimum 10",
                "depositP25Manwon": "25th percentile deposit in 10,000 KRW",
                "depositMedianManwon": "median deposit in 10,000 KRW",
                "depositP75Manwon": "75th percentile deposit in 10,000 KRW",
                "monthlyRentP25Manwon": "25th percentile monthly rent in 10,000 KRW",
                "monthlyRentMedianManwon": "median monthly rent in 10,000 KRW",
                "monthlyRentP75Manwon": "75th percentile monthly rent in 10,000 KRW",
            },
            "limitations": [
                "Retrospective reported contracts can be corrected or cancelled after publication.",
                "Contract dates are not listing, move-in, filing, or data-availability dates.",
                "Aggregates describe historical reported contracts, not current inventory or individual contract safety.",
                "Quantiles use linear interpolation within district/month/building-use/lease-type groups.",
            ],
        })
        input_files = []
        input_sources = []
        for year, path in sorted(archives.items()):
            digest = sha256_file(path)
            acquisition = acquisitions.get(year)
            if acquisition is None:
                raise ValueError(f"missing acquisition record for {year}")
            if acquisition.get("sha256") != digest or acquisition.get("bytes") != path.stat().st_size:
                raise ValueError(f"acquisition record does not match archive for {year}")
            retrieved_at = acquisition.get("retrieved_at")
            if not isinstance(retrieved_at, str):
                raise ValueError(f"invalid retrieved_at for {year}")
            try:
                parsed_retrieved_at = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid retrieved_at for {year}") from exc
            if parsed_retrieved_at.tzinfo is None:
                raise ValueError(f"retrieved_at must include timezone for {year}")
            input_files.append({
                "path": f"data/raw/seoul-rental-files/seoul-rents-{year}.zip",
                "sha256": digest,
            })
        input_sources.append({
                "contract_year": year,
                "file_year": year,
                "landing_url": SOURCE_URL,
                "official_file": acquisition.get("official_file"),
                "retrieved_at": retrieved_at,
                "sha256": digest,
                "bytes": path.stat().st_size,
            })
        manifest = {
            "schema_version": 1,
            "snapshot_id": f"seoul-rental-history-{snapshot_dir.parent.name}",
            "created_at": created_at,
            "data_label": "derived_observed",
            "geography": "Seoul, 25 districts",
            "derivation": "Streamed official annual CSV files, retained only rows whose contract year and receipt year match the annual cohort, removed property-level fields, suppressed groups below 10 rows, and aggregated quantiles by contract month, district, building use and lease type.",
            "input_repository": INPUT_REPOSITORY,
            "input_commit": input_commit,
            "input_files": input_files,
            "input_sources": input_sources,
            "transformation": {
                "command": "python3 -m worldmodel_data publish-seoul-history --raw-dir data/raw/seoul-rental-files --acquisition-ledger data/acquisitions/<date>/seoul-rental-files.json --snapshot-date <date> --years 2022 2023 2024",
                "version": "seoul-rental-history-v1",
            },
            "limitations": [
                "This snapshot supports aggregate market replay only, not deposit-loss or contract-safety validation.",
                "The 2025 official file downloaded on 2026-09-03 was quarantined because 446,244 of 1,084,942 rows were exact duplicates and multiplicity could not be resolved.",
                "Out-of-year contract records are excluded to prevent overlap between annual cohorts.",
                "Receipt-year mismatches are excluded so records known to enter in a later year cannot leak into the prior-year fold.",
            ],
            "files": [entry],
        }
        if admission_audits:
            manifest["admission_review"] = {
                "policy": "same_file_contract_receipt_year_valid_rows_v2",
                "audits": admission_audits,
                "exact_duplicates": "retain source multiplicity below systemic quarantine threshold; not unique transactions",
                "cross_file_overlap": "disjoint file-year-equals-contract-year cohorts; no transaction matching or deduplication",
                "late_reports": "retained in raw archives; excluded from this conservative price-replay view",
            }
            manifest["transformation"]["version"] = "seoul-rental-history-v2"
            manifest["transformation"]["command"] = (
                "python -m worldmodel_data publish-seoul-history --raw-dir <batch> "
                "--acquisition-ledger <ledger> --audit-dir <audit> --snapshot-date <date> --years "
                + " ".join(str(year) for year in years)
            )
            manifest["limitations"].extend([
                "File years are provider export labels, not a universal contract-year definition.",
                "This same-year subset excludes late reports and is not a complete contract-history reconstruction.",
                "Receipt year is only annual information; neither historical as-of availability nor first publication is recovered.",
                "Exact-field multiplicity is retained; aggregate counts are source rows, not confirmed unique transactions.",
            ])
        with (staging_dir / "manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(staging_dir, snapshot_dir)
    return snapshot_dir


def load_acquisition_ledger(path: Path) -> dict[int, dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("source_id") != SOURCE_ID:
        raise ValueError(f"{path}: invalid acquisition ledger")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"{path}: acquisition ledger has no files")
    records: dict[int, dict[str, object]] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: invalid acquisition record")
        year = item.get("file_year", item.get("contract_year"))
        if not isinstance(year, int):
            raise ValueError(f"{path}: invalid acquisition file year")
        if year in records:
            raise ValueError(f"{path}: duplicate acquisition year {year}")
        records[year] = item
    return records
