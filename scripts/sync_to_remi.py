import csv
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

BG_MONTHS = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}

DEAL_TYPE_MAP = {"продажба": "sale", "наем": "rent"}

SOURCE_BY_HOST = {"olx.bg": "olx_bg", "alo.bg": "alo_bg", "imot.bg": "imot_bg"}


def source_from_url(url):
    host = (urlparse(url).hostname or "").replace("www.", "")
    return SOURCE_BY_HOST.get(host, "imot_bg")


def parse_published_at(raw):
    if not raw:
        return None
    text = raw.strip().lower()
    if "днес" in text:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    m = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", text)
    if m:
        month = BG_MONTHS.get(m.group(2))
        if month:
            try:
                return (
                    datetime(int(m.group(3)), month, int(m.group(1)), tzinfo=timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
            except ValueError:
                return None
    return None


def row_to_property(row):
    link = (row.get("Линк") or "").strip()
    description = (row.get("Описание") or "").strip()
    title = description.split(".")[0].strip()[:200] if description else (link or "Пазарен имот")

    price_eur = None
    price_raw = row.get("Цена")
    if price_raw not in (None, ""):
        try:
            price_eur = int(float(str(price_raw).replace(" ", "").replace("\xa0", "")))
        except (ValueError, TypeError):
            price_eur = None

    area_sqm = None
    area_raw = row.get("Площ_квм")
    if area_raw not in (None, ""):
        try:
            area_sqm = float(str(area_raw).replace(" ", "").replace(",", "."))
        except (ValueError, TypeError):
            area_sqm = None

    photo_url = None
    img = (row.get("Снимка_URL") or "").strip()
    if img:
        photo_url = img

    return {
        "source_listing_id": (row.get("ID_обява") or "").strip(),
        "source_url": link,
        "title": title,
        "description": description,
        "price_eur": price_eur,
        "area_sqm": area_sqm,
        "property_type": (row.get("Тип_имот") or "").strip() or None,
        "transaction_type": DEAL_TYPE_MAP.get((row.get("Тип_сделка") or "").strip().lower(), "sale"),
        "city": "Варна",
        "neighborhood": (row.get("Локация-район") or "").strip() or None,
        "urgency": (row.get("Спешност") or "").strip() or None,
        "is_new_construction": (row.get("Ново_строителство") or "").strip().upper() == "YES",
        "is_from_investor": (row.get("От_инвеститор") or "").strip().upper() == "YES",
        "photo_url": photo_url,
        "listing_date": parse_published_at(row.get("Дата_обява")),
    }


def post_json(url, payload, secret, timeout=60):
    return requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        timeout=timeout,
    )


def main():
    api_url = os.environ.get("REMI_API_URL", "").rstrip("/")
    secret = os.environ.get("REMI_INGESTION_SECRET", "")
    csv_path = os.environ.get("REMI_CSV_PATH", "data/properties_varna.csv")

    if not api_url:
        print("ГРЕШКА: REMI_API_URL не е зададен")
        return 2
    if not secret:
        print("ГРЕШКА: REMI_INGESTION_SECRET не е зададен")
        return 2
    if not os.path.exists(csv_path):
        print(f"ГРЕШКА: CSV файл не е намерен: {csv_path}")
        return 2

    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row_to_property(row))

    if not rows:
        print("Няма редове за импорт.")
        return 0

    batch_id = f"scraper-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    by_source = {}
    for prop in rows:
        by_source.setdefault(source_from_url(prop["source_url"]), []).append(prop)

    total_inserted = 0
    total_updated = 0
    total_price_changes = 0
    total_rejected = 0
    failed_sources = 0

    for source, props in by_source.items():
        source_payload = {"batch_id": batch_id, "source": source, "properties": props}
        try:
            resp = post_json(f"{api_url}/api/v1/market/import", source_payload, secret)
        except requests.RequestException as err:
            print(f"ГРЕШКА при извикване на ingestion за {source}: {err}")
            total_rejected += len(props)
            failed_sources += 1
            continue
        if resp.status_code == 401:
            print("ГРЕШКА: неуспешна автентикация към REMI ingestion")
            return 3
        if resp.status_code >= 400:
            print(f"ГРЕШКА при ingestion за {source}: HTTP {resp.status_code} {resp.text[:200]}")
            total_rejected += len(props)
            failed_sources += 1
            continue
        data = resp.json().get("data", {})
        total_inserted += data.get("inserted", 0)
        total_updated += data.get("updated", 0)
        total_price_changes += data.get("price_changes", 0)
        total_rejected += data.get("rejected", 0)
        if data.get("rejected_rows"):
            print(f"Отхвърлени редове ({source}): {data['rejected_rows']}")

    print(
        f"Импорт завершен: вкарани {total_inserted}, актуализирани {total_updated}, "
        f"промени на цените {total_price_changes}, отхвърлени {total_rejected}"
    )

    try:
        sync_resp = post_json(f"{api_url}/api/v1/market/sync", {}, secret, timeout=120)
        if sync_resp.status_code == 200:
            sync_data = sync_resp.json().get("data", {})
            print(
                f"Съпоставяне стартирано: клиенти {sync_data.get('clients_processed')}, "
                f"предложения {sync_data.get('proposals_created')}, "
                f"пропуснати {sync_data.get('notifications_skipped')}"
            )
        else:
            print(f"Съпоставянето върна HTTP {sync_resp.status_code}: {sync_resp.text[:200]}")
            return 4
    except requests.RequestException as err:
        print(f"ГРЕШКА при извикване на съпоставяне: {err}")
        return 4

    if failed_sources:
        return 5
    if total_rejected:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
