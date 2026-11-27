import csv
import hashlib
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit


def env_int(name, default, minimum=0):
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value >= minimum else default
    except (TypeError, ValueError):
        return default


def env_float(name, default, minimum=0.0):
    try:
        value = float(os.environ.get(name, str(default)))
        return value if value >= minimum else default
    except (TypeError, ValueError):
        return default


FINAL_COLUMNS = [
    "Тип_имот", "Тип_продавач", "Тип_сделка", "Локация-район",
    "Площ_квм", "Цена", "Цена_кв.м", "Телефон", "Описание",
    "Източник", "Снимка_URL", "Снимка_локална", "Дата_обява",
    "Спешност", "Ново_строителство", "От_инвеститор",
    "Неопределено", "Класификация_сигурна", "ID_обява",
]


def stable_listing_id(row):
    # Always derive the persisted key from the URL. Older history CSVs do not
    # contain ID_обява, and source-local numeric IDs can collide across sites.
    link = str(row.get("Източник") or row.get("Линк") or "").strip()
    if not link:
        return str(row.get("ID_обява") or "").strip()
    try:
        parsed = urlsplit(link)
        # Query parameters are tracking/session noise for listing identity.
        link = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                           parsed.path.rstrip("/"), "", ""))
    except ValueError:
        pass
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def classify_unknown(row):
    kind = str(row.get("Тип_имот") or "").strip().lower()
    if not kind or kind in {"друго", "unknown", "неопределено"}:
        return True
    return False


CORRECTIONS_COLUMNS = [
    "ID_обява", "Тип_преди", "Тип_коригирана", "Дата_корекция",
    "Бележка", "Източник",
]


def load_corrections(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            str(row.get("ID_обява") or "").strip(): row
            for row in reader
            if str(row.get("ID_обява") or "").strip()
        }


def apply_manual_corrections(df, corrections_path):
    corrections = load_corrections(corrections_path)
    if "Тип_преди" not in df.columns:
        df["Тип_преди"] = ""
    if not corrections:
        return df

    for index, row in df.iterrows():
        listing_id = stable_listing_id(row)
        correction = corrections.get(listing_id)
        # Backward compatibility for an existing correction keyed by the
        # source's old ID_обява value. New corrections should use the stable
        # URL-derived ID written to the CSV.
        if not correction:
            legacy_id = str(row.get("ID_обява") or "").strip()
            legacy = corrections.get(legacy_id)
            if legacy:
                correction_source = str(legacy.get("Източник") or "").strip()
                row_source = str(row.get("Източник") or row.get("Линк") or "").strip()
                if correction_source and row_source and urlsplit(correction_source).netloc.lower() == urlsplit(row_source).netloc.lower():
                    correction = legacy
        if not correction:
            continue
        original = str(row.get("Тип_имот") or "").strip()
        correction_type = str(correction.get("Тип_коригирана") or "").strip()
        if not correction_type:
            continue
        df.at[index, "Тип_преди"] = correction.get("Тип_преди") or original
        df.at[index, "Тип_имот"] = correction_type
        df.at[index, "Неопределено"] = ""
        df.at[index, "Класификация_сигурна"] = "YES"
    return df


def save_manual_correction(corrections_path, listing_id, original_type,
                           corrected_type, note="", source="", corrected_at=None):
    os.makedirs(os.path.dirname(corrections_path) or ".", exist_ok=True)
    corrections = load_corrections(corrections_path)
    corrections[str(listing_id)] = {
        "ID_обява": str(listing_id),
        "Тип_преди": str(original_type),
        "Тип_коригирана": str(corrected_type),
        "Дата_корекция": corrected_at or date.today().isoformat(),
        "Бележка": str(note),
        "Източник": str(source),
    }

    fd, tmp_path = tempfile.mkstemp(prefix=".corrections-", suffix=".csv",
                                    dir=os.path.dirname(corrections_path) or ".")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CORRECTIONS_COLUMNS)
            writer.writeheader()
            for key in sorted(corrections):
                writer.writerow({column: corrections[key].get(column, "") for column in CORRECTIONS_COLUMNS})
        os.replace(tmp_path, corrections_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_csv(df, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".properties-", suffix=".csv",
                                    dir=os.path.dirname(path) or ".")
    os.close(fd)
    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_create_csv(df, path):
    """Atomically create an immutable CSV; fail instead of replacing a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".history-", suffix=".csv",
                                    dir=os.path.dirname(path) or ".")
    os.close(fd)
    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
        # A hard link is atomic and fails if the destination already exists.
        # Both paths are in the same directory/filesystem.
        os.link(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".radar-", suffix=".html",
                                    dir=os.path.dirname(path) or ".")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def build_report(enabled_sites):
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "sites": {
            name: {
                "status": "изключен" if not enabled else "не стартиран",
                "pages": 0,
                "listings": 0,
                "categories": {},
                "passes": {},
                "timeouts": 0,
                "parse_errors": 0,
                "limit_reached": False,
                "reason": "",
            }
            for name, enabled in enabled_sites.items()
        },
        "complete": False,
    }


def update_site(report, site, status, listings=None, pages=None,
                timeouts=None, parse_errors=None, limit_reached=None,
                reason="", categories=None):
    entry = report["sites"].setdefault(site, {})
    if status == "успешен" and entry.get("status") in {"timeout", "грешка"}:
        status = entry["status"]
    entry["status"] = status
    for key, value in (("pages", pages), ("listings", listings),
                       ("timeouts", timeouts), ("parse_errors", parse_errors),
                       ("limit_reached", limit_reached)):
        if value is not None:
            entry[key] = int(value) if key != "limit_reached" else bool(value)
    if reason:
        entry["reason"] = str(reason)
    if categories is not None:
        entry["categories"] = categories
    if status in {"грешка", "timeout", "неуспешен"}:
        for pass_name, pass_entry in entry.get("passes", {}).items():
            if "_started_monotonic" in pass_entry:
                record_pass_stop(report, site, pass_name, "site_error")


def record_page(report, site, pass_name, page_number, page_limit, has_cards):
    entry = report["sites"][site]
    pass_entry = entry["passes"].setdefault(pass_name, {"pages": 0, "listings": 0,
                                                        "timeouts": 0, "parse_errors": 0,
                                                        "limit_reached": False})
    pass_entry["pages"] += 1
    entry["pages"] += 1
    if has_cards and page_number >= page_limit:
        pass_entry["limit_reached"] = True
        pass_entry["stop_reason"] = "page_limit_with_results"
        entry["limit_reached"] = True


def record_pass_start(report, site, pass_name):
    entry = report["sites"][site]
    pass_entry = entry["passes"].setdefault(pass_name, {"pages": 0, "listings": 0,
                                                        "timeouts": 0, "parse_errors": 0,
                                                        "limit_reached": False})
    pass_entry["_started_monotonic"] = time.monotonic()


def record_pass_stop(report, site, pass_name, reason):
    entry = report["sites"][site]
    pass_entry = entry["passes"].setdefault(pass_name, {"pages": 0, "listings": 0,
                                                        "timeouts": 0, "parse_errors": 0,
                                                        "limit_reached": False})
    pass_entry["stop_reason"] = reason
    started = pass_entry.pop("_started_monotonic", None)
    if started is not None:
        pass_entry["duration_seconds"] = round(time.monotonic() - started, 2)


def record_listing(report, site, pass_name):
    entry = report["sites"][site]
    entry["passes"].setdefault(pass_name, {"pages": 0, "listings": 0,
                                           "timeouts": 0, "parse_errors": 0,
                                           "limit_reached": False})["listings"] += 1


def record_timeout(report, site, pass_name, reason):
    entry = report["sites"][site]
    pass_entry = entry["passes"].setdefault(pass_name, {"pages": 0, "listings": 0,
                                                        "timeouts": 0, "parse_errors": 0,
                                                        "limit_reached": False})
    pass_entry["timeouts"] += 1
    pass_entry["stop_reason"] = "timeout"
    started = pass_entry.pop("_started_monotonic", None)
    if started is not None:
        pass_entry["duration_seconds"] = round(time.monotonic() - started, 2)
    entry["timeouts"] += 1
    entry["status"] = "timeout"
    entry["reason"] = str(reason)


def record_parse_error(report, site, pass_name):
    entry = report["sites"][site]
    pass_entry = entry["passes"].setdefault(pass_name, {"pages": 0, "listings": 0,
                                                        "timeouts": 0, "parse_errors": 0,
                                                        "limit_reached": False})
    pass_entry["parse_errors"] += 1
    entry["parse_errors"] += 1


def write_diagnostics(report, validation_errors=None, path="data/scrape_report.json"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = dict(report)
    payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
    payload["errors"] = list(validation_errors or [])
    payload["complete"] = not bool(validation_errors)
    fd, tmp_path = tempfile.mkstemp(prefix=".scrape-report-", suffix=".json",
                                    dir=os.path.dirname(path) or ".")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def validate_snapshot(df, report, enabled_sites,
                      previous_full_path="data/last_full_snapshot.json",
                      check_drop_threshold=True):
    errors = []
    missing = [column for column in FINAL_COLUMNS if column not in df.columns]
    if missing:
        errors.append("Липсват очакваните CSV колони: " + ", ".join(missing))

    if df.empty:
        errors.append("Резултатът е празен")

    for site, enabled in enabled_sites.items():
        if not enabled:
            continue
        site_report = report.get("sites", {}).get(site, {})
        from_rows = int(site_report.get(
            "in_scope_listings", site_report.get("listings", 0)
        ))
        total = from_rows or (int(df["Източник"].fillna("").astype(str).str.contains(
            site.replace(".bg", r"\.bg"), case=False, regex=True
        ).sum()) if "Източник" in df.columns else 0)
        if total == 0:
            errors.append(f"Включеният сайт {site} има 0 обяви")

    if not df.empty:
        minimum = env_int("MIN_LISTINGS", 50, 1)
        if len(df) < minimum:
            errors.append(
                f"Резултатът съдържа само {len(df)} обяви; минимумът е {minimum}"
            )

    previous_count = None
    if os.path.exists(previous_full_path):
        try:
            with open(previous_full_path, encoding="utf-8") as handle:
                previous_count = int(json.load(handle).get("listings", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            previous_count = None

    if check_drop_threshold and previous_count and previous_count > 0:
        threshold = env_float("DROP_THRESHOLD_PERCENT", 30.0, 0.0)
        allowed = previous_count * (1.0 - threshold / 100.0)
        if len(df) < allowed:
            errors.append(
                f"Рязък спад: {len(df)} срещу {previous_count} в предишния пълен обход "
                f"(праг {threshold:.1f}%)"
            )

    for site, entry in report["sites"].items():
        if enabled_sites.get(site, False) and entry.get("limit_reached"):
            errors.append(f"{site} достигна лимита на страници")
        if enabled_sites.get(site, False) and (entry.get("timeouts", 0) or entry.get("status") in {"грешка", "неуспешен", "timeout"}):
            errors.append(f"{site}: {entry.get('reason') or entry.get('status')}")
        if enabled_sites.get(site, False) and entry.get("parse_errors", 0):
            errors.append(f"{site}: {entry['parse_errors']} грешки при обработка на обяви")

    return errors


def mark_full_snapshot(output_path, df, report_path="data/last_full_snapshot.json"):
    payload = {
        "date": date.today().isoformat(),
        "listings": int(len(df)),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "report": report_path,
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".full-snapshot-", suffix=".json",
                                    dir=os.path.dirname(output_path) or ".")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def photo_is_safe_to_cleanup(full_snapshot, report_errors):
    return bool(full_snapshot) and not report_errors


def cleanup_expired_photos(df, photos_dir="data/photos",
                           history_dir="data/history",
                           full_snapshot=True, report_errors=None,
                           retention_days=None, today=None):
    saved = int(len(df))
    deleted = 0
    if not photo_is_safe_to_cleanup(full_snapshot, report_errors or []):
        return {"saved": saved, "deleted": 0, "skipped": "непълен или неуспешен обход"}

    retention = env_int("PHOTO_RETENTION_DAYS", 14, 1) if retention_days is None else retention_days
    today = today or date.today()
    cutoff = today - timedelta(days=retention)

    listing_history = {}
    if os.path.isdir(history_dir):
        for name in sorted(os.listdir(history_dir)):
            if not name.startswith("properties_varna_") or not name.endswith(".csv"):
                continue
            try:
                # Supports immutable per-run names such as
                # properties_varna_2026-11-27_run-123.csv.
                snapshot_date = date.fromisoformat(name[len("properties_varna_"):len("properties_varna_") + 10])
            except (ValueError, TypeError):
                continue
            path = os.path.join(history_dir, name)
            with open(path, encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    listing_id = stable_listing_id(row)
                    if not listing_id:
                        continue
                    item = listing_history.setdefault(
                        listing_id, {"last_seen": date.min, "photos": set()}
                    )
                    item["last_seen"] = max(item["last_seen"], snapshot_date)
                    local_path = str(row.get("Снимка_локална") or "").strip()
                    if local_path:
                        item["photos"].add(local_path)

    active_ids = {stable_listing_id(row) for _, row in df.iterrows()}
    expired_paths = set()
    for listing_id, item in listing_history.items():
        if listing_id in active_ids or item["last_seen"] > cutoff:
            continue
        expired_paths.update(item["photos"])

    for local_path in expired_paths:
        if not os.path.isabs(local_path):
            local_path = os.path.join(
                photos_dir,
                os.path.basename(os.path.normpath(local_path)),
            )
        photos_root = os.path.realpath(photos_dir)
        local_path = os.path.realpath(local_path)
        try:
            if os.path.commonpath([photos_root, local_path]) != photos_root:
                continue
        except ValueError:
            continue
        if os.path.isfile(local_path):
            os.unlink(local_path)
            deleted += 1
    return {"saved": saved, "deleted": deleted, "skipped": ""}
