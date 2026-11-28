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


def normalize_seller_type(raw):
    value = (raw or "").strip().lower()
    if "част" in value or "private" in value:
        return "private"
    if "аген" in value or "фирм" in value or "agency" in value:
        return "agency"
    return "unknown"


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
        "seller_type": normalize_seller_type(row.get("Тип_продавач")),
        "seller_phone": (row.get("Телефон") or "").strip() or None,
        "source_metadata": {str(key): value or "" for key, value in row.items()},
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
            "price_eur", "area_sqm", "property_type", "transaction_type", "seller_type",
            "neighborhood", "photo_url", "listing_date",
        )),
        len(prop.get("description") or ""),
        len(prop.get("title") or ""),
    )


def is_existing_url_duplicate(rejected_row):
    """Return True only for the known idempotent raw-URL unique conflict."""
    if not isinstance(rejected_row, dict):
        return False
    reason = str(rejected_row.get("reason") or rejected_row.get("error") or "").lower()
    return (
        "23505" in reason
        and "market_listings_raw_url_key" in reason
        and "duplicate key" in reason
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

    max_listings_raw = os.environ.get("REMI_MAX_LISTINGS", "").strip()
    if max_listings_raw:
        try:
            max_listings = int(max_listings_raw)
        except ValueError:
            print("ГРЕШКА: REMI_MAX_LISTINGS трябва да е цяло число от 1 до 50")
            return 2
        if not 1 <= max_listings <= BATCH_SIZE:
            print("ГРЕШКА: REMI_MAX_LISTINGS трябва да е цяло число от 1 до 50")
            return 2
        limited = []
        for source, props in by_source.items():
            for prop in props:
                if len(limited) >= max_listings:
                    break
                limited.append((source, prop))
            if len(limited) >= max_listings:
                break
        by_source = {}
        for source, prop in limited:
            by_source.setdefault(source, []).append(prop)
        print(f"Ограничен sync run: ще бъдат изпратени най-много {len(limited)} обяви.")

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

    batch_started_at = datetime.now(timezone.utc)
    batch_id = f"scraper-{batch_started_at.strftime('%Y%m%d-%H%M%S')}"

    total_inserted = 0
    total_updated = 0
    total_price_changes = 0
    total_rejected = 0
    total_existing_url_duplicates = 0
    ingestion_failures = []

    for source, props in by_source.items():
        pending_batches = list(chunks(props))
        batch_no = 0
        batch_index = 0
        while batch_index < len(pending_batches):
            batch = pending_batches[batch_index]
            batch_no += 1
            source_payload = {"batch_id": batch_id, "source": source, "properties": batch}
            try:
                resp = post_json(f"{api_url}/api/v1/market/import", source_payload, secret)
            except requests.RequestException as err:
                print(f"ГРЕШКА при ingestion за {source}, партида {batch_no}: {err}")
                ingestion_failures.append(f"{source} партида {batch_no}: мрежова грешка")
                break
            if resp.status_code in (400, 413) and len(batch) > 1:
                midpoint = len(batch) // 2
                pending_batches[batch_index:batch_index + 1] = [
                    batch[:midpoint], batch[midpoint:]
                ]
                print(
                    f"REMI върна HTTP {resp.status_code} за {source}, партида от {len(batch)} обяви; "
                    "разделям я, за да продължа с валидните записи."
                )
                continue
            if resp.status_code == 413:
                if len(batch) == 1:
                    listing_id = batch[0].get("source_listing_id") or "неизвестен ID"
                    print(
                        f"ГРЕШКА: REMI отхвърли и единичната обява {listing_id} "
                        f"като твърде голям payload: {resp.text[:200]}"
                    )
                    ingestion_failures.append(f"{source} listing {listing_id}: payload твърде голям")
                    batch_index += 1
                continue
            if resp.status_code == 401:
                print("ГРЕШКА: неуспешна автентикация към REMI ingestion")
                return 3
            if resp.status_code >= 400:
                print(
                    f"ГРЕШКА при ingestion за {source}, партида {batch_no}: "
                    f"HTTP {resp.status_code} {resp.text[:200]}"
                )
                ingestion_failures.append(f"{source} партида {batch_no}: HTTP {resp.status_code}")
                batch_index += 1
                continue
            try:
                data = resp.json().get("data", {})
            except (ValueError, AttributeError):
                print(f"ГРЕШКА: невалиден JSON от ingestion за {source}, партида {batch_no}")
                ingestion_failures.append(f"{source} партида {batch_no}: невалиден JSON")
                batch_index += 1
                continue
            if not isinstance(data, dict) or data.get("received") != len(batch):
                print(f"ГРЕШКА: REMI не потвърди всички записи от {source}, партида {batch_no}")
                ingestion_failures.append(f"{source} партида {batch_no}: непълно потвърждение")
                batch_index += 1
                continue
            total_inserted += data.get("inserted", 0)
            total_updated += data.get("updated", 0)
            total_price_changes += data.get("price_changes", 0)
            total_rejected += data.get("rejected", 0)
            print(
                f"Импорт {source}, партида {batch_no}: "
                f"получени {data.get('received', len(batch))}, "
                f"вкарани {data.get('inserted', 0)}, актуализирани {data.get('updated', 0)}, "
                f"отхвърлени {data.get('rejected', 0)}"
            )
            if data.get("rejected_rows"):
                print(f"Отхвърлени редове ({source}): {data['rejected_rows']}")
            if data.get("rejected", 0):
                rejected_count = int(data.get("rejected", 0))
                rejected_rows = data.get("rejected_rows")
                if (
                    isinstance(rejected_rows, list)
                    and len(rejected_rows) == rejected_count
                    and all(is_existing_url_duplicate(row) for row in rejected_rows)
                ):
                    total_existing_url_duplicates += rejected_count
                    print(
                        f"{rejected_count} обяви вече съществуват в REMI по URL; "
                        "продължавам със съпоставянето."
                    )
                else:
                    print("Съпоставянето няма да бъде стартирано заради отхвърлени обяви.")
                    ingestion_failures.append(f"{source} партида {batch_no}: отхвърлени редове")
            batch_index += 1

    if skipped_invalid:
        print("Съпоставянето няма да бъде стартирано заради обяви с невалидни URL/ID.")
        return 5

    print(
        f"Импорт завершен: вкарани {total_inserted}, актуализирани {total_updated}, "
        f"промени на цените {total_price_changes}, отхвърлени {total_rejected}, "
        f"вече съществуващи URL дубликати {total_existing_url_duplicates}"
    )

    if ingestion_failures:
        print(
            "Съпоставянето няма да бъде стартирано заради грешки при импорта; "
            "останалите източници и партиди бяха обработени."
        )
        return 5

    if os.environ.get("REMI_SKIP_MATCHING", "").strip().lower() in {"1", "true", "yes"}:
        print("Съпоставянето е пропуснато по настройка на ограничен sync run.")
        return 0

    try:
        expected_sources = set(SOURCE_BY_HOST.values())
        observed_sources = set(by_source)
        complete_snapshot = (
            not bool(max_listings_raw)
            and not skipped_unsupported
            and observed_sources == expected_sources
        )
        if not complete_snapshot:
            print(
                "Непълен snapshot: няма да маркирам обяви като неактивни "
                f"(източници: {', '.join(sorted(observed_sources)) or 'няма'})."
            )
        sync_payload = {
            "complete_snapshot": complete_snapshot,
            "snapshot_started_at": batch_started_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "snapshot_sources": sorted(observed_sources),
        }
        sync_resp = post_json(f"{api_url}/api/v1/market/sync", sync_payload, secret, timeout=120)
        if sync_resp.status_code == 200:
            sync_data = sync_resp.json().get("data", {})
            print(
                f"Съпоставяне стартирано: клиенти {sync_data.get('clients_processed')}, "
                f"предложения {sync_data.get('proposals_created')}, "
                f"неактивни обяви {sync_data.get('inactive_listings', 0)}, "
                f"пропуснати {sync_data.get('notifications_skipped')}"
            )
        else:
            print(f"Съпоставянето върна HTTP {sync_resp.status_code}: {sync_resp.text[:200]}")
            return 4
    except requests.RequestException as err:
        print(f"ГРЕШКА при извикване на съпоставяне: {err}")
        return 4

    if skipped_invalid:
        return 5
    if skipped_unsupported:
        print("ЗАБЕЛЕЖКА: неподдържаните източници не са изпратени към REMI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
