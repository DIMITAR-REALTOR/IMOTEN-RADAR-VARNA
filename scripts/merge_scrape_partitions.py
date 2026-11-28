"""Validate and publish the two staged site-group scrapes as one snapshot."""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper import PHOTOS_DIR, download_cover_photos, site_from_link
from scraper_safety import (
    FINAL_COLUMNS,
    atomic_create_csv,
    atomic_write_csv,
    build_report,
    mark_full_snapshot,
    validate_snapshot,
    write_diagnostics,
)


PARTITIONS = {
    "non_olx": {
        "csv": "data/staging/non_olx.csv",
        "report": "data/staging/non_olx_report.json",
        "diagnostic_report": "data/staging/diagnostics_non_olx/staging/non_olx_report.json",
        "sites": ("alo.bg", "imot.bg"),
    },
    "olx": {
        "csv": "data/staging/olx.csv",
        "report": "data/staging/olx_report.json",
        "diagnostic_report": "data/staging/diagnostics_olx/staging/olx_report.json",
        "sites": ("olx.bg",),
    },
}

SOURCE_PRIORITY = {"olx.bg": 0, "imot.bg": 1, "alo.bg": 2}


def load_partition(name, config):
    if os.path.exists(config["csv"]):
        frame = pd.read_csv(config["csv"], encoding="utf-8-sig")
    else:
        frame = pd.DataFrame(columns=FINAL_COLUMNS)
    report_path = next(
        (path for path in (config["report"], config["diagnostic_report"]) if os.path.exists(path)),
        None,
    )
    if report_path:
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
    else:
        report = build_report({site: True for site in config["sites"]})
        for site in config["sites"]:
            report["sites"][site]["status"] = "неуспешен"
            report["sites"][site]["reason"] = "Липсват отчет и CSV от scrape job"
        report["scope"] = "partial"
        report["complete"] = False
    if report.get("scope") != "partial":
        raise ValueError(f"Невалиден report за {name}: очаква се scope=partial")
    return frame, report


def merge_reports(non_olx_report, olx_report):
    report = build_report({"olx.bg": True, "alo.bg": True, "imot.bg": True})
    report["scope"] = "full"
    report["started_at"] = min(
        non_olx_report.get("started_at", ""), olx_report.get("started_at", "")
    )
    report["sites"]["alo.bg"] = non_olx_report["sites"]["alo.bg"]
    report["sites"]["imot.bg"] = non_olx_report["sites"]["imot.bg"]
    report["sites"]["olx.bg"] = olx_report["sites"]["olx.bg"]
    report["out_of_scope_listings_removed"] = sum(
        int(part.get("out_of_scope_listings_removed", 0))
        for part in (non_olx_report, olx_report)
    )
    location_counts = {}
    for part in (non_olx_report, olx_report):
        for location, count in part.get("out_of_scope_location_counts", {}).items():
            location_counts[location] = location_counts.get(location, 0) + int(count)
    report["out_of_scope_location_counts"] = location_counts
    return report


def merge_rows(frames):
    df = pd.concat(frames, ignore_index=True)
    for column in FINAL_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    df["_source_priority"] = (
        df["Източник"].map(site_from_link).map(SOURCE_PRIORITY).fillna(9)
    )
    df = df.sort_values("_source_priority", kind="stable")
    df = df.drop_duplicates(subset=["Източник"], keep="first")
    df["_description_key"] = (
        df["Описание"].fillna("").astype(str).str[:50].str.lower().str.strip()
    )
    df = df.drop_duplicates(subset=["Цена", "_description_key"], keep="first")
    df = df.drop(columns=["_description_key", "_source_priority"])

    phone_key = df["Телефон"].fillna("").astype(str).str.split("|").str[0].str.strip()
    has_phone = phone_key != ""
    duplicate_phone = has_phone & pd.DataFrame(
        {"phone": phone_key, "price": df["Цена"], "area": df["Площ_квм"]}
    ).duplicated(keep="first")
    df = df.loc[~duplicate_phone].reset_index(drop=True)

    df["Цена"] = pd.to_numeric(df["Цена"], errors="coerce").astype("Int64")
    df["Площ_квм"] = pd.to_numeric(df["Площ_квм"], errors="coerce").astype("Int64")
    df["Цена_кв.м"] = pd.to_numeric(df["Цена_кв.м"], errors="coerce")
    mask = df["Цена_кв.м"].isna() & df["Цена"].notna() & df["Площ_квм"].notna()
    df.loc[mask, "Цена_кв.м"] = (
        df.loc[mask, "Цена"] / df.loc[mask, "Площ_квм"]
    ).round(2)
    return df[FINAL_COLUMNS]


def site_is_complete(report, site, frame):
    entry = report.get("sites", {}).get(site, {})
    if entry.get("status") != "успешен":
        return False
    if entry.get("limit_reached") or entry.get("timeouts") or entry.get("parse_errors"):
        return False
    if int(entry.get("in_scope_listings", entry.get("listings", 0)) or 0) <= 0:
        return False
    if site == "olx.bg":
        coverage = entry.get("agency_coverage", {})
        if any(not coverage.get(deal, {}).get("verified") for deal in ("продажба", "наем")):
            return False
    for pass_data in entry.get("passes", {}).values():
        if pass_data.get("limit_reached") or pass_data.get("timeouts") or pass_data.get("parse_errors"):
            return False
        if pass_data.get("stop_reason") in {"page_limit_with_results", "timeout", "site_error"}:
            return False
    if "Източник" not in frame:
        return False
    return bool(frame["Източник"].fillna("").astype(str).map(site_from_link).eq(site).any())


def merge_with_previous_source_snapshots(frames, reports):
    previous_path = "data/properties_varna.csv"
    if os.path.exists(previous_path):
        previous = pd.read_csv(previous_path, encoding="utf-8-sig")
    else:
        previous = pd.DataFrame(columns=FINAL_COLUMNS)
    for column in FINAL_COLUMNS:
        if column not in previous:
            previous[column] = ""
    previous["_site"] = previous["Източник"].fillna("").astype(str).map(site_from_link)
    prior_status = {}
    status_file = "data/source_snapshot_status.json"
    if os.path.exists(status_file):
        try:
            with open(status_file, encoding="utf-8") as handle:
                prior_status = json.load(handle).get("sources", {})
        except (OSError, ValueError, json.JSONDecodeError):
            prior_status = {}
    if not prior_status and os.path.exists("data/last_full_snapshot.json"):
        try:
            with open("data/last_full_snapshot.json", encoding="utf-8") as handle:
                last_full_date = json.load(handle).get("date")
            prior_status = {
                site: {"status": "updated", "snapshot_date": last_full_date}
                for site in ("olx.bg", "imot.bg", "alo.bg")
            }
        except (OSError, ValueError, json.JSONDecodeError):
            prior_status = {}

    source_status = {}
    selected = []
    for site in ("olx.bg", "imot.bg", "alo.bg"):
        site_frames = [
            frame.loc[frame["Източник"].fillna("").astype(str).map(site_from_link).eq(site)]
            for frame in frames
            if "Източник" in frame
        ]
        fresh = pd.concat(site_frames, ignore_index=True) if site_frames else pd.DataFrame(columns=FINAL_COLUMNS)
        complete = any(site_is_complete(report, site, frame) for report, frame in reports)
        old = previous.loc[previous["_site"] == site, FINAL_COLUMNS]
        if complete:
            selected.append(fresh)
            source_status[site] = {
                "status": "updated",
                "snapshot_date": date.today().isoformat(),
                "listings": int(len(fresh)),
            }
        else:
            selected.append(old)
            source_status[site] = {
                "status": "stale",
                "snapshot_date": prior_status.get(site, {}).get("snapshot_date"),
                "listings": int(len(old)),
                "reason": "Непълен или липсващ обход; запазен е предишният snapshot.",
            }

    # Preserve legacy/unknown source rows instead of dropping them during a
    # source-aware refresh.
    known_sites = {"olx.bg", "imot.bg", "alo.bg"}
    other = previous.loc[~previous["_site"].isin(known_sites), FINAL_COLUMNS]
    selected.append(other)
    combined = merge_rows(selected)
    return combined, source_status


def main():
    frames = []
    loaded = {}
    for name, config in PARTITIONS.items():
        frame, report = load_partition(name, config)
        frames.append(frame)
        loaded[name] = report

    report = merge_reports(loaded["non_olx"], loaded["olx"])
    df, source_status = merge_with_previous_source_snapshots(
        frames,
        [(loaded["non_olx"], frames[0]), (loaded["olx"], frames[1])],
    )
    report["source_snapshots"] = source_status
    stale_sources = [site for site, status in source_status.items() if status["status"] == "stale"]
    report["partial_publication"] = bool(stale_sources)
    if stale_sources:
        report["publication_notes"] = [
            f"{site}: запазен е последният наличен snapshot; източникът не е обновен."
            for site in stale_sources
        ]

    validation_errors = validate_snapshot(
        df,
        report,
        {"olx.bg": False, "alo.bg": False, "imot.bg": False},
        check_drop_threshold=False,
    )
    all_sources_fresh = not stale_sources
    write_diagnostics(
        report,
        validation_errors + report.get("publication_notes", []),
        path="data/scrape_report.json",
        complete=all_sources_fresh and not validation_errors,
    )
    if validation_errors:
        for error in validation_errors:
            print(f"❌ ВАЛИДАЦИЯ: {error}")
        print("Публикуването е отказано; предишните CSV, history, photos и docs са запазени.")
        return 2

    df["Снимка_локална"] = download_cover_photos(df)
    atomic_write_csv(df, "data/properties_varna.csv")
    run_id = os.environ.get("GITHUB_RUN_ID") or time.strftime("%H%M%S")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    history_df = df
    if stale_sources:
        fresh_sites = {
            site for site, status in source_status.items() if status["status"] == "updated"
        }
        history_df = df.loc[
            df["Източник"].fillna("").astype(str).map(site_from_link).isin(fresh_sites)
        ].copy()
    history_file = (
        f"data/history/properties_varna_{date.today().isoformat()}"
        f"_run-{run_id}-{run_attempt}.csv"
    )
    atomic_create_csv(history_df, history_file)
    if all_sources_fresh:
        mark_full_snapshot("data/last_full_snapshot.json", df)
    status_path = "data/source_snapshot_status.json"
    status_tmp = f"{status_path}.tmp"
    with open(status_tmp, "w", encoding="utf-8") as handle:
        json.dump({"updated_at": date.today().isoformat(), "sources": source_status},
                  handle, ensure_ascii=False, indent=2)
    os.replace(status_tmp, status_path)
    print(
        f"✅ Публикуван snapshot: {len(df)} обяви; "
        f"актуални източници {sum(v['status'] == 'updated' for v in source_status.values())}/3; "
        "предишните данни за непълните източници са запазени."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
