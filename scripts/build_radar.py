#!/usr/bin/env python3
"""
Генерира docs/index.html (радарът, публикуван през GitHub Pages) от
history-файловете в data/history/. Пуска се след всяко скрейпване от
.github/workflows/scraper.yml.

Чете ВСИЧКИ дневни CSV-та в data/history/, слива обявите по линк
(Източник) и за всяка пази: първо и последно видяна дата, брой пъти
видяна, дали е активна в последния обход, и дали цената е падала.
Резултатът се вкарва в scripts/radar_shell.html (готовата страница с
PIN-защита) на мястото на __RAW_DATA_JSON__ / __META_DATA_JSON__.

Ако страницата стане по-голяма от ~15 MB, описанията на обявите се
съкращават допълнително и/или най-старите еднократно видени
неактивни обяви се изрязват, за да остане под лимита на GitHub Pages
и на Claude артефакти (16 MB).
"""
import csv
import glob
import json
import os
import re
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DIR = os.path.join(ROOT, "data", "history")
SHELL_PATH = os.path.join(ROOT, "scripts", "radar_shell.html")
OUT_PATH = os.path.join(ROOT, "docs", "index.html")

MAX_BYTES = 15_000_000  # запас под лимита от 16 MB

ROOM_PATTERNS = [
    (re.compile(r"\b1[-\s]?СТАЕН|едностаен|гарсониер", re.I), "едностаен"),
    (re.compile(r"\b2[-\s]?СТАЕН|двустаен", re.I), "двустаен"),
    (re.compile(r"\b3[-\s]?СТАЕН|тристаен", re.I), "тристаен"),
    (re.compile(r"\b4[-\s]?СТАЕН|четиристаен", re.I), "четиристаен"),
    (re.compile(r"многостаен|\b5[-\s]?СТАЕН|\b6[-\s]?СТАЕН", re.I), "многостаен"),
    (re.compile(r"мезонет", re.I), "мезонет"),
]


def tip_fine_of(tip, desc):
    if tip != "апартамент":
        return tip
    for pat, label in ROOM_PATTERNS:
        if pat.search(desc or ""):
            return label
    return "апартамент"


def src_of(link):
    m = re.search(r"https?://(?:www\.)?([^/]+)/", link or "")
    return m.group(1).upper() if m else "?"


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def days_between(a, b):
    ya, ma, da = map(int, a.split("-"))
    yb, mb, db = map(int, b.split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days


def build_raw_and_meta():
    files = sorted(glob.glob(os.path.join(HIST_DIR, "properties_varna_*.csv")))
    if not files:
        raise SystemExit(f"Няма history файлове в {HIST_DIR}")
    file_dates = [os.path.basename(fp)[len("properties_varna_"):-4] for fp in files]

    listings = {}
    for fdate, fpath in zip(file_dates, files):
        with open(fpath, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            seen_today = set()
            for row in reader:
                link = (row.get("Източник") or "").strip()
                if not link or link in seen_today:
                    continue
                seen_today.add(link)
                price = f(row.get("Цена"))
                sqm = f(row.get("Площ_квм"))
                ppm = f(row.get("Цена_кв.м"))
                if ppm is None and price and sqm:
                    ppm = round(price / sqm, 2)
                tip = (row.get("Тип_имот") or "друго").strip()
                desc = row.get("Описание") or ""
                rec = listings.get(link)
                if rec is None:
                    rec = {
                        "tip": tip,
                        "tip_fine": tip_fine_of(tip, desc),
                        "loc": (row.get("Локация-район") or "").strip(),
                        "sqm": sqm,
                        "price": price,
                        "ppm": ppm,
                        "phone": (row.get("Телефон") or "").strip() or None,
                        "desc": desc,
                        "link": link,
                        "date": (row.get("Дата_обява") or "").strip(),
                        "urgent": (row.get("Спешност") or "").strip().upper() == "YES",
                        "src": src_of(link),
                        "rent": (row.get("Тип_сделка") or "").strip() == "наем",
                        "newBuild": (row.get("Ново_строителство") or "").strip().upper() == "YES",
                        "fromInvestor": (row.get("От_инвеститор") or "").strip().upper() == "YES",
                        "seller": (row.get("Тип_продавач") or "").strip() or None,
                        "photo": (row.get("Снимка_URL") or "").strip() or None,
                        "firstSeen": fdate,
                        "lastSeen": fdate,
                        "timesSeen": 1,
                        "firstPrice": price,
                    }
                else:
                    rec["lastSeen"] = fdate
                    rec["timesSeen"] += 1
                    rec["tip"] = tip
                    rec["tip_fine"] = tip_fine_of(tip, desc)
                    rec["loc"] = (row.get("Локация-район") or "").strip()
                    rec["sqm"] = sqm if sqm is not None else rec["sqm"]
                    rec["price"] = price if price is not None else rec["price"]
                    rec["ppm"] = ppm if ppm is not None else rec["ppm"]
                    rec["phone"] = (row.get("Телефон") or "").strip() or rec["phone"]
                    rec["desc"] = desc
                    rec["date"] = (row.get("Дата_обява") or "").strip()
                    rec["urgent"] = (row.get("Спешност") or "").strip().upper() == "YES"
                    rec["rent"] = (row.get("Тип_сделка") or "").strip() == "наем"
                    rec["newBuild"] = (row.get("Ново_строителство") or "").strip().upper() == "YES"
                    rec["fromInvestor"] = (row.get("От_инвеститор") or "").strip().upper() == "YES"
                    rec["seller"] = (row.get("Тип_продавач") or "").strip() or rec["seller"]
                    rec["photo"] = (row.get("Снимка_URL") or "").strip() or rec["photo"]
                listings[link] = rec

    latest_date = file_dates[-1]
    earliest_date = file_dates[0]

    raw = []
    for rec in listings.values():
        first_price = rec.pop("firstPrice")
        drop = None
        if first_price is not None and rec["price"] is not None and first_price != rec["price"]:
            pct = round((rec["price"] - first_price) / first_price * 100, 1)
            drop = {"from": first_price, "to": rec["price"], "pct": pct}
        out = dict(rec)
        out["daysSpan"] = days_between(rec["firstSeen"], rec["lastSeen"])
        out["active"] = rec["lastSeen"] == latest_date
        out["isNew"] = rec["firstSeen"] == latest_date
        out["drop"] = drop
        raw.append(out)

    meta = {"earliest": earliest_date, "latest": latest_date, "runCount": len(files)}
    return raw, meta


def trim_desc(rec, n):
    d = rec.get("desc") or ""
    if len(d) > n:
        rec["desc"] = d[:n].rstrip() + "…"
    return rec


def fit_under_limit(raw, meta):
    """Намалява размера на raw-data, докато цялата страница се събере под MAX_BYTES."""
    with open(SHELL_PATH, encoding="utf-8") as fh:
        shell = fh.read()
    overhead = len(shell.encode("utf-8")) - len("__RAW_DATA_JSON__") - len("__META_DATA_JSON__")

    desc_len = 180
    drop_stale = False
    for attempt in range(6):
        rows = raw
        if drop_stale:
            rows = [r for r in raw if r["active"] or r["timesSeen"] > 1]
        trimmed = [trim_desc(dict(r), desc_len) for r in rows]
        raw_json = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
        meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        total = overhead + len(raw_json.encode("utf-8")) + len(meta_json.encode("utf-8"))
        if total <= MAX_BYTES:
            return shell, raw_json, meta_json, len(rows), len(raw)
        if desc_len > 60:
            desc_len -= 40
        else:
            drop_stale = True
            desc_len = 100
    # последен опит, каквото и да излезе
    return shell, raw_json, meta_json, len(rows), len(raw)


def main():
    raw, meta = build_raw_and_meta()
    shell, raw_json, meta_json, kept, total = fit_under_limit(raw, meta)
    html = shell.replace("__RAW_DATA_JSON__", raw_json, 1).replace("__META_DATA_JSON__", meta_json, 1)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    size_mb = os.path.getsize(OUT_PATH) / 1e6
    print(f"Радар генериран: {OUT_PATH} ({size_mb:.1f} MB, {kept}/{total} обяви, период {meta['earliest']}–{meta['latest']})")


if __name__ == "__main__":
    main()
