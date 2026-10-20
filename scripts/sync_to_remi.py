import csv
import hashlib
import math
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse, urlunparse

import requests

BG_MONTHS = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}

DEAL_TYPE_MAP = {"продажба": "sale", "наем": "rent"}

SOURCE_BY_HOST = {"olx.bg": "olx_bg", "alo.bg": "alo_bg", "imot.bg": "imot_bg"}
BATCH_SIZE = 50


def supported_source_from_url(url):
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return SOURCE_BY_HOST.get(host)


def source_from_url(url):
    # Backwards-compatible helper; the sync path uses the strict variant above
    # so unsupported domains are never silently mislabeled as imot.bg.
    return supported_source_from_url(url) or "imot_bg"


def listing_id_from_url(url):
    """Return the portal's stable listing id, with a canonical URL fallback."""
    parsed = urlparse(url or "")
    source = supported_source_from_url(url)
    path = parsed.path or ""
    match = None
    if source == "olx_bg":
        match = re.search(r"-(ID[A-Za-z0-9]+)\.html(?:$|/)", path, re.IGNORECASE)
        if not match:
            match = re.search(r"-(\d+)\.html(?:$|/)", path)
    elif source == "alo_bg":
        match = re.search(r"-(\d+)/?$", path) or re.search(r"/(\d+)/?$", path)
    elif source == "imot_bg":
        match = re.search(r"/obiava-([A-Za-z0-9]+)-", path, re.IGNORECASE)
        if not match:
            ids = parse_qs(parsed.query).get("adv") or []
            if ids and ids[0].strip():
                return ids[0].strip()
    if match:
        return match.group(1)

    # Query parameters on listing pages can be tracking/search metadata; omit
    # them from the fallback key so the same listing keeps one identity.
    canonical = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path.rstrip("/"), "", "", ""))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24] if canonical else ""


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
    # The scraper's published CSV keeps the canonical listing URL in "Източник";
    # "Линк" and "ID_обява" are internal columns omitted from that export.
    link = (row.get("Линк") or row.get("Източник") or "").strip()
    description = (row.get("Описание") or "").strip()
    title = description.split(".")[0].strip()[:200] if description else (link or "Пазарен имот")

    price_eur = None
    price_raw = row.get("Цена")
    if price_raw not in (None, ""):
        try:
            price_eur = int(float(str(price_raw).replace(" ", "").replace("\xa0", "")))
        except (ValueError, TypeError, OverflowError):
            price_eur = None
    if price_eur is not None and not 0 <= price_eur <= 100_000_000:
        price_eur = None

    area_sqm = None
    area_raw = row.get("Площ_квм")
    if area_raw not in (None, ""):
        try:
            area_sqm = float(str(area_raw).replace(" ", "").replace(",", "."))
        except (ValueError, TypeError, OverflowError):
            area_sqm = None
    if area_sqm is not None and (not math.isfinite(area_sqm) or not 0 <= area_sqm <= 100_000):
        area_sqm = None

    photo_url = None
    img = (row.get("Снимка_URL") or "").strip()
    if img and is_http_url(img):
        photo_url = img

    return {
        "source_listing_id": (row.get("ID_обява") or "").strip() or listing_id_from_url(link),
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


def chunks(items, size=BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def is_http_url(value):
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def property_quality(prop):
    """Prefer the richer copy when a search page returns the same listing twice."""
    return (
        sum(prop.get(key) not in (None, "") for key in (
            "price_eur", "area_sqm", "property_type", "transaction_type",
            "neighborhood", "photo_url", "listing_date",
        )),
        len(prop.get("description") or ""),
        len(prop.get("title") or ""),
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

    by_source = {}
    skipped_unsupported = 0
    skipped_invalid = 0
    collapsed_duplicates = 0
    sanitized_prices = 0
    sanitized_photos = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            prop = row_to_property(row)
            source = supported_source_from_url(prop["source_url"])
            if source is None:
                skipped_unsupported += 1
                continue
            if not prop["source_listing_id"] or not is_http_url(prop["source_url"]):
                skipped_invalid += 1
                continue
            if (row.get("Цена") or "").strip() and prop["price_eur"] is None:
                sanitized_prices += 1
            raw_photo = (row.get("Снимка_URL") or "").strip()
            if raw_photo and prop["photo_url"] is None:
                sanitized_photos += 1
            source_properties = by_source.setdefault(source, {})
            listing_id = prop["source_listing_id"]
            existing = source_properties.get(listing_id)
            if existing is None:
                source_properties[listing_id] = prop
            else:
                collapsed_duplicates += 1
                if property_quality(prop) > property_quality(existing):
                    source_properties[listing_id] = prop

    by_source = {source: list(properties.values()) for source, properties in by_source.items()}

    row_count = sum(len(props) for props in by_source.values())
    if skipped_unsupported or skipped_invalid or collapsed_duplicates or sanitized_prices or sanitized_photos:
        print(
            f"Предварителна проверка: пропуснати неподдържани източници {skipped_unsupported}, "
            f"невалидни URL/ID {skipped_invalid}, слети дубликати {collapsed_duplicates}, "
            f"невалидни цени, оставени неизвестни {sanitized_prices}, "
            f"невалидни снимки, пропуснати {sanitized_photos}"
        )
    if not row_count:
        print("Няма валидни обяви за импорт; съпоставянето няма да бъде стартирано.")
        return 5

    batch_id = f"scraper-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    total_inserted = 0
    total_updated = 0
    total_price_changes = 0
    total_rejected = 0

    for source, props in by_source.items():
        source_batches = list(chunks(props))
        for batch_no, batch in enumerate(source_batches, start=1):
            source_payload = {"batch_id": batch_id, "source": source, "properties": batch}
            try:
                resp = post_json(f"{api_url}/api/v1/market/import", source_payload, secret)
            except requests.RequestException as err:
                print(f"ГРЕШКА при ingestion за {source}, партида {batch_no}: {err}")
                return 5
            if resp.status_code == 401:
                print("ГРЕШКА: неуспешна автентикация към REMI ingestion")
                return 3
            if resp.status_code >= 400:
                print(
                    f"ГРЕШКА при ingestion за {source}, партида {batch_no}: "
                    f"HTTP {resp.status_code} {resp.text[:200]}"
                )
                return 5
            try:
                data = resp.json().get("data", {})
            except (ValueError, AttributeError):
                print(f"ГРЕШКА: невалиден JSON от ingestion за {source}, партида {batch_no}")
                return 5
            if not isinstance(data, dict) or data.get("received") != len(batch):
                print(f"ГРЕШКА: REMI не потвърди всички записи от {source}, партида {batch_no}")
                return 5
            total_inserted += data.get("inserted", 0)
            total_updated += data.get("updated", 0)
            total_price_changes += data.get("price_changes", 0)
            total_rejected += data.get("rejected", 0)
            print(
                f"Импорт {source}, партида {batch_no}/{len(source_batches)}: "
                f"получени {data.get('received', len(batch))}, "
                f"вкарани {data.get('inserted', 0)}, актуализирани {data.get('updated', 0)}, "
                f"отхвърлени {data.get('rejected', 0)}"
            )
            if data.get("rejected_rows"):
                print(f"Отхвърлени редове ({source}): {data['rejected_rows']}")
            if data.get("rejected", 0):
                print("Съпоставянето няма да бъде стартирано заради отхвърлени обяви.")
                return 5

    if skipped_invalid:
        print("Съпоставянето няма да бъде стартирано заради обяви с невалидни URL/ID.")
        return 5

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

    if total_rejected:
        return 5
    if skipped_invalid:
        return 5
    if skipped_unsupported:
        print("ЗАБЕЛЕЖКА: неподдържаните източници не са изпратени към REMI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
