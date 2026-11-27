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
    cleanup_expired_photos,
    mark_full_snapshot,
    validate_snapshot,
    write_diagnostics,
)


PARTITIONS = {
    "non_olx": {
        "csv": "data/staging/non_olx.csv",
        "report": "data/staging/non_olx_report.json",
        "sites": ("alo.bg", "imot.bg"),
    },
    "olx": {
        "csv": "data/staging/olx.csv",
        "report": "data/staging/olx_report.json",
        "sites": ("olx.bg",),
    },
}

SOURCE_PRIORITY = {"olx.bg": 0, "imot.bg": 1, "alo.bg": 2}


def load_partition(name, config):
    frame = pd.read_csv(config["csv"], encoding="utf-8-sig")
    with open(config["report"], encoding="utf-8") as handle:
        report = json.load(handle)
    if report.get("scope") != "partial" or not report.get("complete"):
        raise ValueError(f"Невалиден или непълен report за {name}")
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


def main():
    frames = []
    loaded = {}
    for name, config in PARTITIONS.items():
        frame, report = load_partition(name, config)
        frames.append(frame)
        loaded[name] = report

    df = merge_rows(frames)
    report = merge_reports(loaded["non_olx"], loaded["olx"])
    validation_errors = validate_snapshot(
        df,
        report,
        {"olx.bg": True, "alo.bg": True, "imot.bg": True},
    )
    write_diagnostics(report, validation_errors, path="data/scrape_report.json")
    if validation_errors:
        for error in validation_errors:
            print(f"❌ ВАЛИДАЦИЯ: {error}")
        print("Публикуването е отказано; предишните CSV, history, photos и docs са запазени.")
        return 2

    df["Снимка_локална"] = download_cover_photos(df)
    atomic_write_csv(df, "data/properties_varna.csv")
    run_id = os.environ.get("GITHUB_RUN_ID") or time.strftime("%H%M%S")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    history_file = (
        f"data/history/properties_varna_{date.today().isoformat()}"
        f"_run-{run_id}-{run_attempt}.csv"
    )
    atomic_create_csv(df, history_file)
    mark_full_snapshot("data/last_full_snapshot.json", df)
    photo_stats = cleanup_expired_photos(
        df,
        photos_dir=PHOTOS_DIR,
        history_dir="data/history",
        full_snapshot=True,
        report_errors=[],
    )
    print(
        f"✅ Общ snapshot: {len(df)} обяви; снимки запазени {photo_stats['saved']}, "
        f"изтрити {photo_stats['deleted']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
