"""Read-only annual archive audits. Reports are diagnostics, never admission approval.

No source rows or addresses are emitted. SQLite bounds duplicate-index memory.
Exact duplicates refer to identical parsed field values, not transaction identity.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import tempfile
import zipfile
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .manifest import sha256_file
from .seoul_rents import REQUIRED_COLUMNS, MAX_UNCOMPRESSED_BYTES

VERSION = "seoul-history-audit-v1"


def _write(path, text):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def load_acquisition_records(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("source_id") != "seoul-rental-price-files":
        raise ValueError(f"{path}: invalid acquisition ledger")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"{path}: acquisition ledger has no files")
    records = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: invalid acquisition record")
        file_year = item.get("file_year", item.get("contract_year"))
        if not isinstance(file_year, int) or file_year in records:
            raise ValueError(f"{path}: invalid or duplicate acquisition year")
        digest = item.get("sha256")
        size = item.get("bytes")
        retrieved_at = item.get("retrieved_at")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(size, int) or size < 1 or not isinstance(retrieved_at, str)):
            raise ValueError(f"{path}: incomplete acquisition record for {file_year}")
        try:
            parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{path}: invalid retrieved_at for {file_year}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{path}: retrieved_at must include timezone for {file_year}")
        records[file_year] = item
    return payload, records


DISTRICTS = {"11110", "11140", "11170", "11200", "11215", "11230", "11260",
             "11290", "11305", "11320", "11350", "11380", "11410", "11440",
             "11470", "11500", "11530", "11545", "11560", "11590", "11620",
             "11650", "11680", "11710", "11740"}


def _encoding(archive, member):
    with archive.open(member) as handle:
        prefix = handle.read(4)
    candidates = (["utf-8-sig"] if prefix.startswith(codecs.BOM_UTF8) else
                  ["utf-16"] if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                  else ["utf-8", "cp949"])
    for encoding in candidates:
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            with archive.open(member) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
            return encoding
        except UnicodeDecodeError:
            pass
    raise ValueError("unsupported_or_invalid_encoding")


def audit_archive(path: Path, year: int, db, duplicate_threshold=0.01):
    report = {"year": year, "file": path.name, "status": "missing", "issues": [],
              "source_rows": 0, "complete": False}
    if not path.exists():
        report["issues"].append("missing_archive")
        return report
    if not path.is_file() or path.is_symlink():
        report.update(status="quarantined", issues=["not_regular_file"])
        return report
    report.update(sha256=sha256_file(path), bytes=path.stat().st_size)
    reasons, nulls, months, districts, uses, leases = (Counter() for _ in range(6))
    contract_years, receipt_years, contract_receipt_years = (Counter() for _ in range(3))
    unknown_districts = Counter()
    duplicate_months, duplicate_districts, duplicate_gaps = (Counter() for _ in range(3))
    amounts = {key: {"count": 0, "min": None, "max": None} for key in ("보증금(만원)", "임대료(만원)")}
    eligible = invalid = wrong_year = late = 0
    report["members"] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            report["members"] = [{"name": m.filename, "bytes": m.file_size} for m in members]
            if sum(m.file_size for m in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("uncompressed_size_limit")
            if len(members) != 1 or not members[0].filename.lower().endswith((".csv", ".txt")):
                raise ValueError("requires_delimited_member_adapter")
            member = members[0]
            encoding = _encoding(archive, member)
            with archive.open(member) as binary, io.TextIOWrapper(binary, encoding=encoding, newline="") as handle:
                sample = handle.read(65536)
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
            except csv.Error:
                delimiter = ","
            with archive.open(member) as binary, io.TextIOWrapper(binary, encoding=encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter, strict=True)
                headers = next(reader, [])
                report.update(encoding=encoding, delimiter=delimiter, columns=headers,
                              schema_id=hashlib.sha256(json.dumps(sorted(headers), ensure_ascii=False).encode()).hexdigest())
                missing = sorted(REQUIRED_COLUMNS - set(headers))
                report["missing_required_columns"] = missing
                if missing:
                    report["issues"].append("requires_schema_adapter")
                if not headers or len(headers) != len(set(headers)):
                    raise ValueError("empty_or_duplicate_headers")
                for values in reader:
                    report["source_rows"] += 1
                    number = report["source_rows"]
                    # Header/value pairing avoids false cross-year matches after column reordering.
                    identity = json.dumps(sorted(zip(headers, values)), ensure_ascii=False, separators=(",", ":"))
                    if len(values) != len(headers):
                        identity = json.dumps([headers, values], ensure_ascii=False)
                    digest = hashlib.sha256(identity.encode()).digest()
                    prior = db.execute("SELECT n, first_row FROM seen WHERE year=? AND hash=?", (year, digest)).fetchone()
                    db.execute("INSERT INTO seen VALUES (?,?,1,?) ON CONFLICT(year,hash) DO UPDATE SET n=n+1", (year, digest, number))
                    if len(values) != len(headers):
                        reasons["row_width_mismatch"] += 1
                        invalid += 1
                        continue
                    row = dict(zip(headers, values))
                    for key in headers:
                        if not row[key].strip():
                            nulls[key] += 1
                    issues = []
                    parsed = None
                    raw_date = row.get("계약일", "").strip()
                    try:
                        if not re.fullmatch(r"\d{8}", raw_date):
                            raise ValueError()
                        parsed = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
                    except ValueError:
                        issues.append("invalid_contract_date")
                    month = parsed.strftime("%Y-%m") if parsed else "invalid"
                    code = row.get("자치구코드", "").strip()
                    if prior:
                        duplicate_months[month] += 1
                        duplicate_districts[code if code in DISTRICTS else "invalid"] += 1
                        duplicate_gaps[str(number - prior[1])] += 1
                    if parsed:
                        months[month] += 1
                        contract_years[str(parsed.year)] += 1
                    districts[code if code in DISTRICTS else "invalid"] += 1
                    if code not in DISTRICTS:
                        unknown_districts[code or "[blank]"] += 1
                        issues.append("invalid_seoul_district")
                    use = row.get("건물용도", "").strip()
                    lease = row.get("전월세구분", "").strip()
                    uses[use or "[blank]"] += 1
                    leases[lease or "[blank]"] += 1
                    if not use:
                        issues.append("missing_building_use")
                    if lease not in {"전세", "월세"}:
                        issues.append("invalid_lease_type")
                    for key, stats in amounts.items():
                        value = row.get(key, "").strip().replace(",", "")
                        if not re.fullmatch(r"-?\d+", value):
                            issues.append("invalid_" + ("deposit" if key == "보증금(만원)" else "rent"))
                            continue
                        value = int(value)
                        stats["count"] += 1
                        stats["min"] = value if stats["min"] is None else min(value, stats["min"])
                        stats["max"] = value if stats["max"] is None else max(value, stats["max"])
                        if value < 0:
                            issues.append("negative_" + ("deposit" if key == "보증금(만원)" else "rent"))
                    receipt = row.get("접수년도", "").strip()
                    if not re.fullmatch(r"\d{4}", receipt):
                        issues.append("missing_or_invalid_receipt_year")
                    else:
                        receipt_years[receipt] += 1
                        if parsed:
                            contract_receipt_years[f"{parsed.year}->{receipt}"] += 1
                    reasons.update(issues)
                    # Mutually exclusive accounting; issue counts above can overlap.
                    if issues:
                        invalid += 1
                    elif parsed.year != year:
                        wrong_year += 1
                    elif int(receipt) != parsed.year:
                        late += 1
                    else:
                        eligible += 1
        report["complete"] = True
    except (ValueError, OSError, UnicodeError, csv.Error, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        report["issues"].append(type(exc).__name__ + ": " + str(exc)[:200])
    db.commit()
    duplicate_rows, unique_rows = db.execute("SELECT COALESCE(SUM(n-1),0), COUNT(*) FROM seen WHERE year=?", (year,)).fetchone()
    histogram = dict(db.execute("SELECT n, COUNT(*) FROM seen WHERE year=? AND n>1 GROUP BY n", (year,)).fetchall())
    count = report["source_rows"]
    ratio = duplicate_rows / count if count else 0
    report.update(exact_duplicate_rows=duplicate_rows, unique_exact_rows=unique_rows,
                  exact_duplicate_ratio=round(ratio, 6), duplicate_multiplicity_histogram=histogram,
                  duplicate_rows_by_month=dict(sorted(duplicate_months.items())),
                  duplicate_rows_by_district=dict(sorted(duplicate_districts.items())),
                  frequent_duplicate_row_gaps=dict(duplicate_gaps.most_common(10)),
                  null_counts=dict(nulls), issue_row_counts=dict(reasons),
                  contract_year_counts=dict(sorted(contract_years.items())),
                  receipt_year_counts=dict(sorted(receipt_years.items())),
                  contract_to_receipt_year_counts=dict(sorted(contract_receipt_years.items())),
                  monthly_counts=dict(sorted(months.items())), district_counts=dict(sorted(districts.items())),
                  unknown_district_code_counts=dict(sorted(unknown_districts.items())),
                  building_use_counts=dict(uses), lease_type_counts=dict(leases), amount_ranges=amounts,
                  accounting={"eligible_for_existing_replay": eligible, "invalid": invalid,
                              "other_contract_year": wrong_year, "receipt_year_mismatch": late},
                  missing_contract_months=[f"{year}-{m:02}" for m in range(1, 13) if not months[f"{year}-{m:02}"]],
                  missing_district_codes=sorted(DISTRICTS - districts.keys()))
    if not count:
        report["issues"].append("empty_or_unreadable_archive")
    if ratio > duplicate_threshold:
        report["issues"].append("systemic_exact_duplicates_unresolved")
    report["status"] = ("quarantined" if not report["complete"] or not count or ratio > duplicate_threshold
                        else "requires_adapter" if "requires_schema_adapter" in report["issues"]
                        else "needs_review" if report["issues"] or invalid or wrong_year or late or report["missing_contract_months"] or report["missing_district_codes"]
                        else "checks_passed_pending_review")
    return report


def audit_history(raw_dir: Path, output_dir: Path, years, duplicate_threshold=0.01,
                  acquisition_ledger: Optional[Path] = None):
    years = sorted(set(years))
    if not years or any(y < 2011 or y > 2025 for y in years):
        raise ValueError("years must be within 2011..2025")
    if not math.isfinite(duplicate_threshold) or not 0 <= duplicate_threshold <= 1:
        raise ValueError("duplicate threshold must be finite and within 0..1")
    # Reserve output before reading: immutable, exclusive and never inside raw input.
    raw_dir, output_dir = raw_dir.resolve(), output_dir.resolve()
    if output_dir == raw_dir or raw_dir in output_dir.parents:
        raise ValueError("audit output must be outside raw input")
    output_dir.mkdir(parents=True, exist_ok=False)
    audit_code_sha256 = sha256_file(Path(__file__))
    acquisition_payload = acquisition_records = None
    if acquisition_ledger is not None:
        acquisition_ledger = acquisition_ledger.resolve()
        acquisition_payload, acquisition_records = load_acquisition_records(acquisition_ledger)
    with tempfile.TemporaryDirectory(prefix="seoul-audit-") as temporary:
        with closing(sqlite3.connect(str(Path(temporary) / "duplicates.sqlite"))) as db:
            # Ephemeral index only: source data remains untouched. Bound the page
            # cache while avoiding millions of random disk reads on annual files.
            db.execute("PRAGMA cache_size=-131072")
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("CREATE TABLE seen (year INTEGER, hash BLOB, n INTEGER, first_row INTEGER, PRIMARY KEY(year,hash)) WITHOUT ROWID")
            reports = []
            for year in years:
                print(f"Auditing {year}...", flush=True)
                report = audit_archive(raw_dir / f"seoul-rents-{year}.zip", year, db, duplicate_threshold)
                if acquisition_records is not None:
                    acquisition = acquisition_records.get(year)
                    if acquisition is None:
                        report["issues"].append("missing_acquisition_record")
                        report["status"] = "quarantined"
                    else:
                        report["acquisition"] = {
                            "retrieved_at": acquisition["retrieved_at"],
                            "sha256": acquisition["sha256"],
                            "bytes": acquisition["bytes"],
                            "status": acquisition.get("status"),
                        }
                        if (report.get("sha256") != acquisition["sha256"]
                                or report.get("bytes") != acquisition["bytes"]):
                            report["issues"].append("acquisition_record_mismatch")
                            report["status"] = "quarantined"
                if reports and "columns" in report and "columns" in reports[-1]:
                    old = set(reports[-1]["columns"])
                    new = set(report["columns"])
                    report["schema_change_from_previous"] = {"year": reports[-1]["year"], "added": sorted(new-old), "removed": sorted(old-new)}
                    previous_count = reports[-1]["source_rows"]
                    report["row_count_ratio_to_previous"] = round(report["source_rows"] / previous_count, 4) if previous_count else None
                reports.append(report)
                _write(output_dir / f"{year}.audit.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            overlaps = []
            for left, right in zip(years, years[1:]):
                count = db.execute("SELECT COUNT(*) FROM seen a JOIN seen b ON a.hash=b.hash WHERE a.year=? AND b.year=?", (left,right)).fetchone()[0]
                overlaps.append({"left_year": left, "right_year": right, "shared_exact_field_groups": count})
    summary = {"schema_version": 1, "audit_version": VERSION,
               "created_at": datetime.now(timezone.utc).isoformat(),
               "audit_code_sha256": audit_code_sha256,
               "acquisition_ledger_sha256": sha256_file(acquisition_ledger) if acquisition_ledger else None,
               "acquisition_source_page_sha256": acquisition_payload.get("source_page_sha256") if acquisition_payload else None,
               "duplicate_threshold": duplicate_threshold,
               "claim": "diagnostics_only_not_snapshot_admission",
               "limitations": ["Exact field equality does not identify a transaction; nothing is deduplicated.",
                               "Coverage gaps, amount extrema and year-over-year changes require source review.",
                               "Unsupported schemas require explicit adapters; missing receipt years are never inferred.",
                               "Cross-year overlap compares complete field sets, not economic contract identity.",
                               "Reports contain aggregate diagnostics; retain them in controlled work storage until publication review."],
               "adjacent_archive_overlaps": overlaps, "years": reports}
    _write(output_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 首尔年度租赁质量审计", "", "诊断结果不等于发布批准；没有删除、改写或发布原始记录。", "",
             "| 年份 | 行数 | 重复率 | 无效行 | 状态 |", "|---|---:|---:|---:|---|"]
    for r in reports:
        lines.append(f"| {r['year']} | {r['source_rows']:,} | {r.get('exact_duplicate_ratio', 0):.2%} | {r.get('accounting', {}).get('invalid', 0):,} | {r['status']} |")
    _write(output_dir / "SUMMARY.md", "\n".join(lines) + "\n")
    return summary


def command_audit_history(args):
    ledger = Path(args.acquisition_ledger) if args.acquisition_ledger else None
    result = audit_history(Path(args.raw_dir), Path(args.output_dir), args.years,
                           args.duplicate_threshold, ledger)
    print(Path(args.output_dir) / "SUMMARY.md")
    return 2 if any(r["status"] in {"missing", "quarantined", "requires_adapter"} for r in result["years"]) else 0
