"""Download observed official file links into a new, immutable acquisition batch.

Run from the repository root. No API key required; outputs stay in ignored raw storage.
"""
import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

LANDING = "https://data.seoul.go.kr/dataList/OA-21276/A/1/datasetView.do"
ENDPOINT = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false"


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2011, 2026)))
    args = parser.parse_args()
    destination = Path(args.output_dir)
    if not args.years or any(y < 2011 or y > 2025 for y in args.years):
        parser.error("years must be within 2011..2025")
    destination.mkdir(parents=True, exist_ok=False)
    request = urllib.request.Request(LANDING, headers={"User-Agent": "NestLinkerDataAudit/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        page_bytes = response.read(10 * 1024 * 1024 + 1)
    if len(page_bytes) > 10 * 1024 * 1024:
        raise ValueError("source page exceeds 10 MiB")
    page = page_bytes.decode("utf-8")
    (destination / "source-page.html").write_bytes(page_bytes)
    links = {int(y): seq for seq, y in re.findall(r"href=\"javascript:downloadFile\('([0-9]+)'\);\"\s+title=\"서울특별시_전월세가_([0-9]{4})\.zip\"", page)}
    ledger = {"schema_version": 1, "source_id": "seoul-rental-price-files", "landing_url": LANDING,
              "discovered_at": datetime.now(timezone.utc).date().isoformat(),
              "source_page_sha256": hashlib.sha256(page_bytes).hexdigest(),
              "official_page_partition_instruction": "접수년도",
              "file_year_semantics": "provider_defined_requires_row_level_audit", "files": []}
    for year in sorted(set(args.years)):
        record = {"file_year": year, "official_file": f"서울특별시_전월세가_{year}.zip"}
        ledger["files"].append(record)
        try:
            if year not in links:
                raise ValueError("year not present in official page")
            body = urllib.parse.urlencode({"infId": "OA-21276", "seqNo": "", "seq": links[year], "infSeq": "3"}).encode()
            request = urllib.request.Request(ENDPOINT, data=body, headers={"Referer": LANDING, "User-Agent": "NestLinkerDataAudit/1.0"})
            digest = hashlib.sha256()
            size = 0
            path = destination / f"seoul-rents-{year}.zip"
            partial = destination / f"seoul-rents-{year}.partial"
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("xb") as handle:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise ValueError("download exceeds 512 MiB")
                    digest.update(chunk)
                    handle.write(chunk)
            if not zipfile.is_zipfile(partial):
                raise ValueError("response is not a ZIP; retained as partial")
            partial.rename(path)
            record.update(status="downloaded_not_admitted", seq=links[year], download_url=ENDPOINT,
                          retrieved_at=datetime.now(timezone.utc).isoformat(), bytes=size, sha256=digest.hexdigest())
            print(f"{year}: {size:,} bytes {digest.hexdigest()}", flush=True)
        except Exception as exc:
            record.update(status="download_failed", error=str(exc))
            print(f"{year}: FAILED {exc}", flush=True)
        # A checkpoint ledger is acquisition metadata, not a published snapshot.
        (destination / "acquisition-ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 2 if any(r["status"] == "download_failed" for r in ledger["files"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
