from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

BASE_URL = "https://apis.data.go.kr/1613000"
PATHS = {
    "apartment": "/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
    "officetel": "/RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent",
    "rowhouse": "/RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
    "single_multi": "/RTMSDataSvcSHRent/getRTMSDataSvcSHRent",
}
SOURCE_IDS = {
    "apartment": "molit-rtms-rent",
    "officetel": "molit-rtms-officetel-rent",
    "rowhouse": "molit-rtms-rowhouse-rent",
    "single_multi": "molit-rtms-single-multi-rent",
}
SOURCE_LANDING_URLS = {
    "apartment": "https://www.data.go.kr/data/15126474/openapi.do",
    "officetel": "https://www.data.go.kr/data/15126475/openapi.do",
    "rowhouse": "https://www.data.go.kr/data/15126473/openapi.do",
    "single_multi": "https://www.data.go.kr/data/15126472/openapi.do",
}
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


def rolling_months(count: int, today: date | None = None) -> list[str]:
    if count < 1 or count > 60:
        raise ValueError("months must be between 1 and 60")
    cursor = (today or date.today()).replace(day=1)
    values: list[str] = []
    for _ in range(count):
        values.append(cursor.strftime("%Y%m"))
        previous = cursor.month - 1 or 12
        year = cursor.year - (1 if cursor.month == 1 else 0)
        cursor = date(year, previous, 1)
    return values


def parse_xml_items(xml_text: str) -> tuple[list[dict[str, str]], int, str | None]:
    root = ET.fromstring(xml_text)
    code = root.findtext(".//resultCode") or root.findtext(".//CODE")
    message = root.findtext(".//resultMsg") or root.findtext(".//MESSAGE")
    if code not in (None, "00", "000", "INFO-000"):
        raise RuntimeError(f"RTMS error {code}: {message or 'unknown'}")
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        items.append({child.tag: (child.text or "").strip() for child in item})
    total_text = root.findtext(".//totalCount")
    total = int(total_text) if total_text and total_text.isdigit() else len(items)
    return items, total, message


def _pick(record: dict[str, str], *keys: str) -> str:
    lowered = {key.lower(): value.strip() for key, value in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value:
            return value
    return ""


def _number(value: str, *, integer: bool = False) -> int | float | None:
    cleaned = "".join(ch for ch in value if ch.isdigit() or ch in ".-")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return int(number) if integer else number


def normalize_item(
    source: str,
    raw: dict[str, str],
    lawd_code: str,
    occurrence_index: int = 0,
) -> dict[str, object] | None:
    legal_dong = _pick(raw, "umdNm", "법정동", "법정동명")
    year = _pick(raw, "dealYear", "년")
    month = _pick(raw, "dealMonth", "월")
    day = _pick(raw, "dealDay", "일")
    if not legal_dong or not (year and month and day):
        return None
    try:
        deal_date = date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None
    deposit_text = _pick(raw, "deposit", "보증금액")
    rent_text = _pick(raw, "monthlyRent", "월세금액")
    deposit = _number(deposit_text, integer=True)
    rent = _number(rent_text, integer=True)
    # An explicit zero is valid.  Missing or malformed amounts are not zero and
    # must not be converted into a jeonse observation.
    if deposit is None or rent is None:
        return None
    building_name = _pick(raw, "aptNm", "아파트", "offiNm", "단지", "houseType", "주택유형") or None
    identity = json.dumps(
        {
            "source": source,
            "lawd_code": lawd_code,
            "raw": raw,
            "occurrence_index": occurrence_index,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "id": "rtms-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        "source": source,
        "lawd_code": lawd_code,
        "legal_dong": legal_dong,
        "building_name": building_name,
        "lease_type": "monthly" if rent > 0 else "jeonse",
        "deal_date": deal_date,
        "deposit_manwon": deposit,
        "monthly_rent_manwon": rent,
        "exclusive_area_sqm": _number(_pick(raw, "excluUseAr", "전용면적")),
        "floor": _number(_pick(raw, "floor", "층"), integer=True),
        "build_year": _number(_pick(raw, "buildYear", "건축년도"), integer=True),
        "contract_term": _pick(raw, "contractTerm", "계약기간") or None,
    }


def _request(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "NestLinkerWorldData/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            raise RuntimeError("RTMS response exceeds size limit")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise RuntimeError("RTMS response exceeds size limit")
        return payload.decode("utf-8", errors="replace")


def fetch_rtms(
    *, service_key: str, lawd_codes: Iterable[str], months: Iterable[str], output_dir: Path, delay: float = 0.15
) -> dict[str, object]:
    key = urllib.parse.unquote(service_key.strip())
    if not key:
        raise ValueError("DATA_GO_KR_SERVICE_KEY is required")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_file = output_dir / "rtms-raw.jsonl"
    normalized_file = output_dir / "rtms-normalized.jsonl"
    raw_count = 0
    normalized_count = 0
    if delay < 0 or delay > 5:
        raise ValueError("delay must be between 0 and 5 seconds")
    with raw_file.open("w", encoding="utf-8") as raw_handle, normalized_file.open("w", encoding="utf-8") as norm_handle:
        for lawd_code in lawd_codes:
            if not (lawd_code.isdigit() and len(lawd_code) == 5):
                raise ValueError(f"invalid LAWD code: {lawd_code}")
            for deal_month in months:
                if len(deal_month) != 6 or not deal_month.isdigit():
                    raise ValueError(f"invalid month: {deal_month}")
                try:
                    date(int(deal_month[:4]), int(deal_month[4:]), 1)
                except ValueError as exc:
                    raise ValueError(f"invalid month: {deal_month}") from exc
                for source, path in PATHS.items():
                    page = 1
                    occurrences: dict[str, int] = {}
                    while True:
                        query = urllib.parse.urlencode({
                            "serviceKey": key,
                            "LAWD_CD": lawd_code,
                            "DEAL_YMD": deal_month,
                            "pageNo": page,
                            "numOfRows": 1000,
                        }, safe="%")
                        text = _request(f"{BASE_URL}{path}?{query}")
                        items, total, _ = parse_xml_items(text)
                        for item in items:
                            raw_handle.write(json.dumps({"source": source, "source_id": SOURCE_IDS[source], "lawd_code": lawd_code, "month": deal_month, "record": item}, ensure_ascii=False) + "\n")
                            raw_count += 1
                            canonical = json.dumps(
                                {"source": source, "lawd_code": lawd_code, "raw": item},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            occurrence_index = occurrences.get(canonical, 0)
                            occurrences[canonical] = occurrence_index + 1
                            normalized = normalize_item(source, item, lawd_code, occurrence_index)
                            if normalized:
                                norm_handle.write(json.dumps(normalized, ensure_ascii=False) + "\n")
                                normalized_count += 1
                        if page * 1000 >= total or not items:
                            break
                        page += 1
                        time.sleep(delay)
                    time.sleep(delay)
    result = {
        "schema_version": 1,
        "sources": [
            {
                "property_type": source,
                "source_id": SOURCE_IDS[source],
                "endpoint": f"{BASE_URL}{path}",
                "landing_url": SOURCE_LANDING_URLS[source],
            }
            for source, path in PATHS.items()
        ],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "raw_records": raw_count,
        "normalized_records": normalized_count,
        "output_dir": str(output_dir),
    }
    (output_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def service_key_from_env() -> str:
    return os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
