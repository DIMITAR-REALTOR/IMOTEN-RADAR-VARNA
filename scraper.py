"""
Scraper: OLX.bg | ALO.bg | imot.bg
Варна | Само частни лица | Само продажби | Всички типове имоти
"""

import re
import time
import random
import os
import hashlib
import pandas as pd
import sys
from datetime import date
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ── Настройки ───────────────────────────────────────────────────
OLX_EMAIL    = os.environ.get("OLX_EMAIL", "")
OLX_PASSWORD = os.environ.get("OLX_PASSWORD", "")
OUTPUT_FILE  = "data/properties_varna.csv"
MAX_PAGES    = int(os.environ.get("MAX_PAGES", "25"))
BGN_TO_EUR   = 1.95583

PHONE_RE  = re.compile(r'(?:0|\+359)[\s\-]?\d[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}[\s\-]?\d{2}')
URGENT_RE = re.compile(r'спешно|бързо|намалена\s+цена|веднага', re.I)

PROPERTY_TYPES = {
    "апартамент": ["апартамент", "ап.", "двустаен", "тристаен", "едностаен", "четиристаен", "мезонет", "студио"],
    "къща":       ["къща", "вила", "хижа", "кантон"],
    "парцел":     ["парцел", "УПИ", "ПИ", "земя", "дворно място"],
    "земеделска": ["нива", "земеделска", "лозе", "градина", "ливада", "декар", "дка"],
    "гараж":      ["гараж", "паркомясто", "паркинг"],
    "офис":       ["офис", "кабинет"],
    "търговски":  ["магазин", "склад", "търговски", "заведение", "ресторант", "хотел"],
}

AGENCY_WORDS = re.compile(
    r'агенция|брокер|имоти\b|недвижими\s+имоти|real\s+estate|agency', re.I
)
PRIVATE_WORDS = re.compile(
    r'собственик|частно\s+лице|без\s+посредник|лично', re.I
)

os.makedirs("data", exist_ok=True)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


# ── Помощни функции ─────────────────────────────────────────────

def rand_sleep(a=1.2, b=3.0):
    time.sleep(random.uniform(a, b))


def clean_phone(raw: str) -> str:
    return re.sub(r'[\s\-\(\)]', '', raw).replace("tel:", "")


def extract_phones(text: str) -> str:
    found = PHONE_RE.findall(text or "")
    cleaned = list(dict.fromkeys(clean_phone(p) for p in found))
    return " | ".join(cleaned)


def to_eur(price_str: str) -> int | None:
    if not price_str:
        return None
    s = str(price_str).replace(" ", "").replace("\xa0", "")
    m = re.search(r'([\d]+(?:[.,]\d+)?)', s)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
        if "лв" in s or "BGN" in s:
            val = val / BGN_TO_EUR
        return int(round(val))
    except Exception:
        return None


def detect_type(text: str) -> str:
    t = (text or "").lower()
    for ptype, keywords in PROPERTY_TYPES.items():
        if any(kw in t for kw in keywords):
            return ptype
    return "друго"


def is_urgent(text: str) -> str:
    return "YES" if URGENT_RE.search(text or "") else "NO"


def extract_id_from_url(url: str, source: str) -> str:
    if source == "olx":
        m = re.search(r'-(\d+)\.html', url) or re.search(r'/d/[^/]+-(\d+)', url)
    elif source == "imot":
        m = re.search(r'adv=(\d+)', url)
    elif source == "alo":
        m = re.search(r'/(\d+)/?$', url) or re.search(r'-(\d+)\.html', url)
    else:
        m = None
    if m:
        return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:10]


def click_first(page, selectors: list[str], timeout=4000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count():
                locator.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


def fill_first(page, selectors: list[str], value: str, timeout=7000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count():
                locator.fill(value, timeout=timeout)
                return True
        except Exception:
            pass
    return False


def save_debug_page(page, name: str):
    try:
        page.screenshot(path=f"data/{name}.png", full_page=True)
    except Exception:
        pass
    try:
        with open(f"data/{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def is_private(text: str) -> bool:
    if AGENCY_WORDS.search(text or ""):
        return False
    return True


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


# ══════════════════════════════════════════════════════════════════
# OLX.BG
# ══════════════════════════════════════════════════════════════════

def scrape_olx(context) -> list[dict]:
    rows = []
    page = context.new_page()
    olx_logged_in = False

    # Вход с акаунт
    if OLX_EMAIL and OLX_PASSWORD:
        print("  OLX: влизане в акаунт...")
        try:
            page.goto("https://www.olx.bg/accounts/login/", wait_until="domcontentloaded", timeout=30000)
            rand_sleep(1, 2)

            click_first(page, [
                "#onetrust-accept-btn-handler",
                "button:has-text('Accept')",
                "button:has-text('Приемам')",
                "button:has-text('Съгласен')",
                "button:has-text('Разбрах')",
            ], timeout=3000)

            email_filled = fill_first(page, [
                'input[name="username"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[autocomplete="username"]',
                'input[data-testid*="email"]',
                'input[data-testid*="username"]',
                '#username',
                '#email',
            ], OLX_EMAIL)
            if not email_filled:
                save_debug_page(page, "olx_login_debug")
                raise RuntimeError("Не намерих поле за email/username на OLX login страницата")

            rand_sleep(0.5, 1)

            password_filled = fill_first(page, [
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]',
                'input[data-testid*="password"]',
                '#password',
            ], OLX_PASSWORD)
            if not password_filled:
                save_debug_page(page, "olx_login_debug")
                raise RuntimeError("Не намерих поле за парола на OLX login страницата")

            rand_sleep(0.5, 1)
            clicked_submit = click_first(page, [
                'button[data-testid="login-submit-button"]',
                'button[type="submit"]',
                "button:has-text('Вход')",
                "button:has-text('Влез')",
                "button:has-text('Login')",
                "button:has-text('Log in')",
            ], timeout=7000)
            if not clicked_submit:
                save_debug_page(page, "olx_login_debug")
                raise RuntimeError("Не намерих бутон за вход на OLX login страницата")

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            rand_sleep(2, 4)

            body_text = page.inner_text("body")
            if re.search(r'грешна|невалидн|incorrect|invalid|captcha|robot', body_text, re.I):
                save_debug_page(page, "olx_login_debug")
                raise RuntimeError("OLX отказа входа или показа captcha/anti-bot проверка")

            olx_logged_in = True
            print("  OLX: влязохме успешно")
        except Exception as e:
            print(f"  OLX login грешка: {e}")
            print(f"  OLX login debug: data/olx_login_debug.png и data/olx_login_debug.html")

    base_url = (
        "https://www.olx.bg/nedvizhimi-imoti/varna/"
        "?search%5Bprivate_business%5D=private"
        "&search%5Border%5D=created_at:desc"
    )

    current_page = 1
    while current_page <= MAX_PAGES:
        url = base_url if current_page == 1 else f"{base_url}&page={current_page}"
        print(f"  OLX стр.{current_page}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Скрол за lazy load
            page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            rand_sleep(0.5, 1)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            rand_sleep(1, 2)
        except PwTimeout:
            print("  OLX: timeout")
            break

        cards = page.query_selector_all('[data-cy="l-card"]')
        if not cards:
            print("  OLX: няма карти")
            break

        for card in cards:
            try:
                link_el = card.query_selector('a[href*="/d/"]')
                link = link_el.get_attribute("href") if link_el else ""
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.olx.bg" + link

                title_el = card.query_selector('[data-cy="ad-card-title"]')
                title = title_el.inner_text().strip() if title_el else ""

                price_el = card.query_selector('[data-testid="ad-price"]')
                price_raw = price_el.inner_text().strip() if price_el else ""

                loc_el = card.query_selector('[data-testid="location-date"]')
                loc_raw = loc_el.inner_text().strip() if loc_el else ""
                parts = loc_raw.split("-")
                loc = parts[0].strip()
                date_raw = parts[-1].strip() if len(parts) > 1 else ""

                img_el = card.query_selector("img[src]")
                img = img_el.get_attribute("src") if img_el else ""

                eur = to_eur(price_raw)

                # Телефон — само ако сме влезли в акаунт
                phone = ""
                sqm = None
                if olx_logged_in:
                    try:
                        detail = context.new_page()
                        detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                        rand_sleep(1, 2)

                        # Площ от детайл страница
                        detail_text = detail.inner_text("body")
                        ma = re.search(r'([\d]+)\s*кв\.?м', detail_text)
                        if ma:
                            sqm = int(ma.group(1))

                        # Натискаме "Покажи телефон"
                        click_first(detail, [
                            '[data-testid="show-phone"]',
                            'button[data-testid*="phone"]',
                            'button:has-text("Покажи")',
                            'button:has-text("телефон")',
                            'button:has-text("phone")',
                            'a:has-text("Покажи")',
                            '[role="button"]:has-text("Покажи")',
                        ], timeout=5000)
                        rand_sleep(1, 2)

                        phone_el = detail.query_selector('[data-testid="seller-phone-number"]')
                        if phone_el:
                            phone = clean_phone(phone_el.inner_text())
                        else:
                            phone = extract_phones(detail.inner_text("body"))

                        detail.close()
                    except Exception:
                        pass

                ppm = round(eur / sqm, 2) if eur and sqm else None

                rows.append({
                    "Цена":        eur,
                    "Площ_квм":   sqm,
                    "Цена_на_квм": ppm,
                    "Локация":    loc,
                    "Описание":   title,
                    "Линк":       link,
                    "Снимка_URL": img,
                    "Дата_обява": date_raw,
                    "Източник":   "olx",
                    "ID_обява":   extract_id_from_url(link, "olx"),
                    "Спешност":   is_urgent(title),
                    "Телефон":    phone,
                    "Тип_имот":   detect_type(title),
                })
            except Exception as e:
                print(f"  OLX card грешка: {e}")

        next_btn = page.query_selector('[data-cy="pagination-forward"]')
        if not next_btn:
            break
        current_page += 1
        rand_sleep(2, 3)

    page.close()
    print(f"  OLX: {len(rows)} обяви")
    return rows


# ══════════════════════════════════════════════════════════════════
# ALO.BG
# ══════════════════════════════════════════════════════════════════

def scrape_alo(context) -> list[dict]:
    rows = []
    page = context.new_page()

    # adv_by=1 = само частни лица, adv_type=sale = продажба
    base_url = "https://www.alo.bg/obiavi/nedvizhimi-imoti/varna/?adv_type=sale&adv_by=1"

    current_page = 1
    while current_page <= MAX_PAGES:
        url = base_url if current_page == 1 else f"{base_url}&page={current_page}"
        print(f"  ALO стр.{current_page}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            rand_sleep(1, 2)
        except PwTimeout:
            print("  ALO: timeout")
            break

        cards = page.query_selector_all(".listvip, .listing-item, [class*='ad-item']")
        if not cards:
            print("  ALO: няма карти")
            break

        for card in cards:
            try:
                link_el = card.query_selector("a[href*='/obiava/'], a[href*='/ad/']")
                if not link_el:
                    link_el = card.query_selector("a[href]")
                link = link_el.get_attribute("href") if link_el else ""
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.alo.bg" + link

                title_el = card.query_selector("[class*='title']")
                title = title_el.inner_text().strip() if title_el else ""

                price_el = card.query_selector("[class*='price']")
                price_raw = price_el.inner_text().strip() if price_el else ""
                eur = to_eur(price_raw)

                loc_el = card.query_selector("[class*='address'], [class*='location']")
                loc = loc_el.inner_text().strip() if loc_el else "Варна"

                img_el = card.query_selector("img[src]")
                img = img_el.get_attribute("src") if img_el else ""

                date_el = card.query_selector("[class*='date'], [class*='time']")
                date_raw = date_el.inner_text().strip() if date_el else ""

                # Отваряме детайл страница за телефон и площ
                phone, sqm, desc_full = "", None, title
                try:
                    detail = context.new_page()
                    detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                    rand_sleep(1.5, 2.5)

                    detail_text = detail.inner_text("body")

                    # Частно лице проверка
                    if AGENCY_WORDS.search(detail_text) and not PRIVATE_WORDS.search(detail_text):
                        detail.close()
                        continue

                    # Площ
                    ma = re.search(r'([\d]+)\s*кв\.?м', detail_text)
                    if ma:
                        sqm = int(ma.group(1))

                    # Описание
                    desc_el = detail.query_selector("[class*='desc'], [class*='description']")
                    if desc_el:
                        desc_full = title + " | " + desc_el.inner_text().strip()[:300]

                    # Телефон
                    try:
                        show_btn = detail.query_selector("[class*='show-phone'], [class*='showPhone'], [data-action*='phone']")
                        if show_btn:
                            show_btn.click()
                            rand_sleep(1, 1.5)
                    except Exception:
                        pass
                    phone_el = detail.query_selector("a[href^='tel:']")
                    if phone_el:
                        phone = clean_phone(phone_el.get_attribute("href") or "")
                    if not phone:
                        phone = extract_phones(detail_text)

                    # Снимка
                    if not img:
                        img_el2 = detail.query_selector("img[src*='alo']")
                        if img_el2:
                            img = img_el2.get_attribute("src") or ""

                    detail.close()
                except Exception:
                    pass

                ppm = round(eur / sqm, 2) if eur and sqm else None

                rows.append({
                    "Цена":        eur,
                    "Площ_квм":   sqm,
                    "Цена_на_квм": ppm,
                    "Локация":    loc,
                    "Описание":   desc_full,
                    "Линк":       link,
                    "Снимка_URL": img,
                    "Дата_обява": date_raw,
                    "Източник":   "alo",
                    "ID_обява":   extract_id_from_url(link, "alo"),
                    "Спешност":   is_urgent(desc_full),
                    "Телефон":    phone,
                    "Тип_имот":   detect_type(desc_full),
                })
            except Exception as e:
                print(f"  ALO card грешка: {e}")

        next_btn = page.query_selector("a[rel='next'], a.next, [class*='next-page']")
        if not next_btn:
            break
        current_page += 1
        rand_sleep(2, 3)

    page.close()
    print(f"  ALO: {len(rows)} обяви")
    return rows


# ══════════════════════════════════════════════════════════════════
# IMOT.BG
# ══════════════════════════════════════════════════════════════════

def scrape_imot(context) -> list[dict]:
    rows = []
    page = context.new_page()

    # act=11 продажба, f1=1 Варна, f2=1 само собственик
    base_url = (
        "https://www.imot.bg/pcgi/imot.cgi"
        "?act=11&f1=1&f2=1"
    )

    current_page = 1
    while current_page <= MAX_PAGES:
        url = base_url if current_page == 1 else f"{base_url}&f21={current_page}"
        print(f"  imot.bg стр.{current_page}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            rand_sleep(1, 2)
        except PwTimeout:
            print("  imot.bg: timeout")
            break

        links_on_page = page.query_selector_all('a[href*="act=5"]')
        if not links_on_page:
            print("  imot.bg: няма повече")
            break

        # Вземаме уникалните линкове
        hrefs = []
        seen_hrefs = set()
        for a in links_on_page:
            h = a.get_attribute("href") or ""
            if "act=5" in h and h not in seen_hrefs:
                seen_hrefs.add(h)
                full = "https://www.imot.bg" + h if not h.startswith("http") else h
                hrefs.append(full)

        for link in hrefs:
            try:
                detail = context.new_page()
                detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                rand_sleep(1, 2)

                detail_text = detail.inner_text("body")

                # Заглавие
                title_el = detail.query_selector("h1, h2, .title")
                title = title_el.inner_text().strip() if title_el else ""

                # Описание
                desc_el = detail.query_selector(".description, [class*='desc']")
                desc = desc_el.inner_text().strip()[:400] if desc_el else ""
                full_desc = (title + " | " + desc).strip(" |")

                # Частно лице — imot.bg вече го филтрира с f2=1, но проверяваме пак
                if AGENCY_WORDS.search(full_desc) and not PRIVATE_WORDS.search(full_desc):
                    detail.close()
                    continue

                # Цена
                price_el = detail.query_selector("[class*='price'], .price")
                price_raw = price_el.inner_text() if price_el else detail_text
                eur = to_eur(price_raw)

                # Площ
                sqm = None
                ma = re.search(r'([\d]+)\s*кв\.?м', detail_text)
                if ma:
                    sqm = int(ma.group(1))

                ppm = round(eur / sqm, 2) if eur and sqm else None

                # Локация
                loc_el = detail.query_selector("h2, [class*='location'], [class*='adress']")
                loc = loc_el.inner_text().strip().split("\n")[0] if loc_el else "Варна"

                # Дата
                date_el = detail.query_selector("[class*='date'], [class*='time']")
                date_raw = date_el.inner_text().strip() if date_el else ""

                # Телефон
                phone = extract_phones(detail_text)
                if not phone:
                    phone_el = detail.query_selector("a[href^='tel:']")
                    if phone_el:
                        phone = clean_phone(phone_el.get_attribute("href") or "")

                # Снимка
                img = ""
                img_el = detail.query_selector("img[src*='imot.bg'], img[src*='property']")
                if img_el:
                    img = img_el.get_attribute("src") or ""

                detail.close()

                rows.append({
                    "Цена":        eur,
                    "Площ_квм":   sqm,
                    "Цена_на_квм": ppm,
                    "Локация":    loc,
                    "Описание":   full_desc,
                    "Линк":       link,
                    "Снимка_URL": img,
                    "Дата_обява": date_raw,
                    "Източник":   "imot",
                    "ID_обява":   extract_id_from_url(link, "imot"),
                    "Спешност":   is_urgent(full_desc),
                    "Телефон":    phone,
                    "Тип_имот":   detect_type(full_desc),
                })
            except Exception as e:
                print(f"  imot.bg грешка: {e}")

        next_btn = page.query_selector('a[href*="f21="]')
        if not next_btn:
            break
        current_page += 1
        rand_sleep(2, 4)

    page.close()
    print(f"  imot.bg: {len(rows)} обяви")
    return rows


# ══════════════════════════════════════════════════════════════════
# ГЛАВНА ФУНКЦИЯ
# ══════════════════════════════════════════════════════════════════

def main():
    all_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1366, "height": 768},
            locale="bg-BG",
            extra_http_headers={"Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"},
        )

        print("\n=== OLX.bg ===")
        try:
            all_rows += scrape_olx(context)
        except Exception as e:
            print(f"OLX ГРЕШКА: {e}")

        print("\n=== ALO.bg ===")
        try:
            all_rows += scrape_alo(context)
        except Exception as e:
            print(f"ALO ГРЕШКА: {e}")

        print("\n=== imot.bg ===")
        try:
            all_rows += scrape_imot(context)
        except Exception as e:
            print(f"imot.bg ГРЕШКА: {e}")

        browser.close()

    # ── Обединяване и почистване ──
    COLS = ["Цена","Площ_квм","Цена_на_квм","Локация","Описание",
            "Линк","Снимка_URL","Дата_обява","Източник","ID_обява",
            "Спешност","Телефон","Тип_имот"]

    df = pd.DataFrame(all_rows, columns=COLS)

    # Дедупликация по линк
    df = df.drop_duplicates(subset=["Линк"])

    # Дедупликация между сайтове по цена + описание
    df["_desc_key"] = df["Описание"].str[:50].str.lower().str.strip()
    df = df.drop_duplicates(subset=["Цена", "_desc_key"])
    df = df.drop(columns=["_desc_key"])

    # Числови типове
    df["Цена"]        = pd.to_numeric(df["Цена"],        errors="coerce").astype("Int64")
    df["Площ_квм"]   = pd.to_numeric(df["Площ_квм"],    errors="coerce").astype("Int64")
    df["Цена_на_квм"] = pd.to_numeric(df["Цена_на_квм"], errors="coerce")

    # Изчисли Цена_на_квм където липсва
    mask = df["Цена_на_квм"].isna() & df["Цена"].notna() & df["Площ_квм"].notna()
    df.loc[mask, "Цена_на_квм"] = (df.loc[mask, "Цена"] / df.loc[mask, "Площ_квм"]).round(2)

    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

    print(f"\n✅ ГОТОВО — {len(df)} уникални обяви → {OUTPUT_FILE}")
    print(df["Източник"].value_counts().to_string())
    print(df["Тип_имот"].value_counts().to_string())


if __name__ == "__main__":
    main()
