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
import json
import base64
from datetime import date
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ── Настройки ───────────────────────────────────────────────────
OLX_EMAIL    = os.environ.get("OLX_EMAIL", "")
OLX_PASSWORD = os.environ.get("OLX_PASSWORD", "")
OLX_LOGIN_URL = os.environ.get("OLX_LOGIN_URL", "https://www.olx.bg/accounts/login/")
OLX_STORAGE_STATE_B64 = os.environ.get("OLX_STORAGE_STATE_B64", "")
OUTPUT_FILE  = "data/properties_varna.csv"
MAX_PAGES    = int(os.environ.get("MAX_PAGES", "25"))
MAX_OLX_PAGES = int(os.environ.get("MAX_OLX_PAGES", str(MAX_PAGES)))
MAX_ALO_PAGES = int(os.environ.get("MAX_ALO_PAGES", str(MAX_PAGES)))
MAX_IMOT_PAGES = int(os.environ.get("MAX_IMOT_PAGES", str(MAX_PAGES)))
SCRAPE_ALO = os.environ.get("SCRAPE_ALO", "1") == "1"
SCRAPE_IMOT = os.environ.get("SCRAPE_IMOT", "1") == "1"
SCRAPE_OLX = os.environ.get("SCRAPE_OLX", "1") == "1"
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


def clean_text(text: str, limit: int | None = None) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip()
    return text


def site_from_link(link: str) -> str:
    if "olx.bg" in link:
        return "olx.bg"
    if "alo.bg" in link:
        return "alo.bg"
    if "imot.bg" in link:
        return "imot.bg"
    return "unknown"


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


def extract_sqm(text: str) -> int | None:
    patterns = [
        r'(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:кв\.?\s*м\.?|квм|m2|m²|sq\.?\s*m)',
        r'(?:площ|застроена\s+площ|квадратура)\D{0,20}(\d{1,4}(?:[.,]\d{1,2})?)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I)
        if m:
            try:
                value = float(m.group(1).replace(",", "."))
                if 5 <= value <= 50000:
                    return int(round(value))
            except Exception:
                pass
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
            locator.wait_for(state="visible", timeout=timeout)
            locator.click(timeout=timeout)
            return True
        except Exception:
            pass
    return False


def fill_first(page, selectors: list[str], value: str, timeout=7000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
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


def reveal_phone(page) -> str:
    before = page.inner_text("body")
    phone = extract_phones(before)
    if phone:
        return phone

    click_first(page, [
        'button:has-text("Виж номера")',
        'a:has-text("Виж номера")',
        '[role="button"]:has-text("Виж номера")',
        'button:has-text("Виж номер")',
        'a:has-text("Виж номер")',
        '[role="button"]:has-text("Виж номер")',
        'button:has-text("Покажи телефона")',
        'button:has-text("Покажи телефон")',
        'button:has-text("Покажи")',
        'a:has-text("Покажи телефона")',
        'a:has-text("Покажи телефон")',
        'a:has-text("Покажи")',
        '[role="button"]:has-text("Покажи")',
        'button:has-text("Обади се")',
        'a:has-text("Обади се")',
        '[role="button"]:has-text("Обади се")',
        'button:has-text("Виж")',
        'a:has-text("Виж")',
        '[role="button"]:has-text("Виж")',
        'input[value="Виж"]',
        '.phone:has-text("Виж")',
        '[class*="phone"]:has-text("Виж")',
        '[data-testid="show-phone"]',
        '[data-testid*="phone"]',
        '[class*="show-phone"]',
        '[class*="showPhone"]',
        '[data-action*="phone"]',
        'a[href^="tel:"]',
    ], timeout=6000)
    rand_sleep(1.5, 2.5)

    phone_el = page.query_selector('[data-testid="seller-phone-number"], a[href^="tel:"]')
    if phone_el:
        href = phone_el.get_attribute("href") or ""
        text = phone_el.inner_text() or ""
        phone = extract_phones(f"{href} {text}")
        if phone:
            return phone

    return extract_phones(page.inner_text("body"))


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
    if OLX_STORAGE_STATE_B64:
        olx_logged_in = True
        print("  OLX: използвам запазена login сесия от OLX_STORAGE_STATE_B64")
    elif OLX_EMAIL and OLX_PASSWORD:
        print("  OLX: влизане в акаунт...")
        try:
            login_urls = [OLX_LOGIN_URL, "https://login.olx.bg/", "https://www.olx.bg/accounts/login/"]
            last_error = None
            for login_url in dict.fromkeys(login_urls):
                try:
                    page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                    rand_sleep(2, 4)
                    if page.locator('input[type="email"], input[name="username"], input[name="email"], input[autocomplete="username"]').count():
                        break
                except Exception as e:
                    last_error = e
            if last_error and not page.url:
                raise last_error
            rand_sleep(1, 2)

            click_first(page, [
                "#onetrust-accept-btn-handler",
                "button[id*='accept']",
                "button:has-text('Accept')",
                "button:has-text('Accept all')",
                "button:has-text('Приемам')",
                "button:has-text('Съгласен')",
                "button:has-text('Разбрах')",
            ], timeout=3000)

            email_filled = fill_first(page, [
                'input[name="username"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[type="text"]',
                'input[placeholder*="mail"]',
                'input[placeholder*="имейл"]',
                'input[placeholder*="телефон"]',
                'input[autocomplete="username"]',
                'input[data-testid*="email"]',
                'input[data-testid*="username"]',
                '#username',
                '#email',
            ], OLX_EMAIL)
            if not email_filled:
                save_debug_page(page, "olx_login_debug")
                raise RuntimeError(f"Не намерих поле за email/username. URL: {page.url}")

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
    while current_page <= MAX_OLX_PAGES:
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

                phone = ""
                sqm = None
                desc_full = title
                try:
                    detail = context.new_page()
                    detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                    rand_sleep(1, 2)

                    detail_text = detail.inner_text("body")
                    sqm = extract_sqm(f"{title} {detail_text}")

                    desc_el = detail.query_selector('[data-cy="ad_description"], [data-testid="ad-description"], div:has-text("Описание")')
                    if desc_el:
                        desc_full = clean_text(f"{title} | {desc_el.inner_text()}", 600)

                    # Телефон — само ако сме влезли в акаунт
                    if olx_logged_in:
                        phone = reveal_phone(detail)

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
                    "Източник":   link,
                    "ID_обява":   extract_id_from_url(link, "olx"),
                    "Спешност":   is_urgent(desc_full),
                    "Телефон":    phone,
                    "Тип_имот":   detect_type(desc_full),
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
    while current_page <= MAX_ALO_PAGES:
        url = base_url if current_page == 1 else f"{base_url}&page={current_page}"
        print(f"  ALO стр.{current_page}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            rand_sleep(1, 2)
        except PwTimeout:
            print("  ALO: timeout")
            break

        cards = page.query_selector_all(".listvip, .listvip-item, .listing-item, [class*='ad-item'], article, div[class*='obiava'], div[class*='listing']")
        if not cards:
            cards = page.query_selector_all("a[href*='/obiava/'], a[href*='/ad/'], a[href*='alo.bg/']")
        if not cards:
            print("  ALO: няма карти")
            save_debug_page(page, f"alo_page_{current_page}_debug")
            break

        for card in cards:
            try:
                link_el = card.query_selector("a.listvip-image[href], a[href*='alo.bg/'][href], a[href*='/obiava/'], a[href*='/ad/']")
                if not link_el:
                    link_el = card.query_selector("a[href]")
                if not link_el and (card.get_attribute("href") or ""):
                    link_el = card
                link = link_el.get_attribute("href") if link_el else ""
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.alo.bg" + link
                if "alo.bg" not in link or any(x in link for x in ["/login", "/users", "/search", "/obiavi/nedvizhimi-imoti/varna/"]):
                    continue

                title_el = card.query_selector(".listvip-item-title, [class*='title'], h2, h3, a[href]")
                title = title_el.inner_text().strip() if title_el else card.inner_text().strip()

                price_el = card.query_selector(".ads-params-multi:has-text('€'), [class*='price']")
                price_raw = price_el.inner_text().strip() if price_el else ""
                eur = to_eur(price_raw)

                loc_el = card.query_selector(".listvip-item-address, [class*='address'], [class*='location']")
                loc = loc_el.inner_text().strip() if loc_el else "Варна"

                img_el = card.query_selector("img[src]")
                img = img_el.get_attribute("src") if img_el else ""

                date_el = card.query_selector(".hidden-xs:last-child, [class*='date'], [class*='time']")
                date_raw = date_el.inner_text().strip() if date_el else ""

                card_text = card.inner_text()
                sqm = extract_sqm(card_text)
                ppm_match = re.search(r'(\d+(?:[.,]\d+)?)\s*€/кв\.?м', card_text, re.I)
                ppm = float(ppm_match.group(1).replace(",", ".")) if ppm_match else None
                desc_el_card = card.query_selector(".listvip-desc")
                desc_full = clean_text(f"{title} | {desc_el_card.inner_text() if desc_el_card else ''}", 600)

                # Отваряме детайл страница за телефон и площ
                phone = ""
                try:
                    detail = context.new_page()
                    detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                    rand_sleep(1.5, 2.5)

                    detail_text = detail.inner_text("body")
                    if not title:
                        title_el2 = detail.query_selector("h1, h2, h3, title")
                        title = clean_text(title_el2.inner_text() if title_el2 else "", 200)

                    # Частно лице проверка
                    if AGENCY_WORDS.search(detail_text) and not PRIVATE_WORDS.search(detail_text):
                        detail.close()
                        continue

                    # Площ
                    sqm = sqm or extract_sqm(f"{title} {detail_text}")

                    # Описание
                    desc_el = detail.query_selector("[class*='desc'], [class*='description']")
                    if desc_el:
                        desc_full = clean_text(title + " | " + desc_el.inner_text(), 600)

                    # Телефон
                    phone = reveal_phone(detail)

                    # Снимка
                    if not img:
                        img_el2 = detail.query_selector("img[src*='alo']")
                        if img_el2:
                            img = img_el2.get_attribute("src") or ""

                    detail.close()
                except Exception:
                    pass

                ppm = ppm or (round(eur / sqm, 2) if eur and sqm else None)

                rows.append({
                    "Цена":        eur,
                    "Площ_квм":   sqm,
                    "Цена_на_квм": ppm,
                    "Локация":    loc,
                    "Описание":   desc_full,
                    "Линк":       link,
                    "Снимка_URL": img,
                    "Дата_обява": date_raw,
                    "Източник":   link,
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
    while current_page <= MAX_IMOT_PAGES:
        url = base_url if current_page == 1 else f"{base_url}&f21={current_page}"
        print(f"  imot.bg стр.{current_page}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            rand_sleep(1, 2)
        except PwTimeout:
            print("  imot.bg: timeout")
            break

        links_on_page = page.query_selector_all('a[href*="/obiava-"], a[href*="act=5"], a[href*="adv="], a[href*="imot.cgi"]')
        if not links_on_page:
            print("  imot.bg: няма повече")
            save_debug_page(page, f"imot_page_{current_page}_debug")
            break

        # Вземаме уникалните линкове
        hrefs = []
        seen_hrefs = set()
        for a in links_on_page:
            h = a.get_attribute("href") or ""
            if ("/obiava-" in h or "act=5" in h or "adv=" in h) and h not in seen_hrefs and "act=11" not in h:
                seen_hrefs.add(h)
                full = "https://www.imot.bg" + h if h.startswith("/") else h
                hrefs.append(full)
        if not hrefs:
            print("  imot.bg: няма валидни линкове към обяви")
            save_debug_page(page, f"imot_page_{current_page}_debug")
            break

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
                sqm = extract_sqm(f"{title} {detail_text}")

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
                    "Източник":   link,
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
        context_options = {
            "user_agent": random.choice(USER_AGENTS),
            "viewport": {"width": 1366, "height": 768},
            "locale": "bg-BG",
            "extra_http_headers": {"Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"},
        }
        if OLX_STORAGE_STATE_B64:
            context_options["storage_state"] = json.loads(base64.b64decode(OLX_STORAGE_STATE_B64).decode("utf-8"))
        context = browser.new_context(**context_options)

        if SCRAPE_ALO:
            print("\n=== ALO.bg ===")
            try:
                all_rows += scrape_alo(context)
            except Exception as e:
                print(f"ALO ГРЕШКА: {e}")
        else:
            print("\n=== ALO.bg ===")
            print("  ALO: пропуснато чрез SCRAPE_ALO=0")

        if SCRAPE_IMOT:
            print("\n=== imot.bg ===")
            try:
                all_rows += scrape_imot(context)
            except Exception as e:
                print(f"imot.bg ГРЕШКА: {e}")
        else:
            print("\n=== imot.bg ===")
            print("  imot.bg: пропуснато чрез SCRAPE_IMOT=0")

        if SCRAPE_OLX:
            print("\n=== OLX.bg ===")
            try:
                all_rows += scrape_olx(context)
            except Exception as e:
                print(f"OLX ГРЕШКА: {e}")
        else:
            print("\n=== OLX.bg ===")
            print("  OLX: пропуснато чрез SCRAPE_OLX=0")

        browser.close()

    # ── Обединяване и почистване ──
    COLS = ["Цена","Площ_квм","Цена_на_квм","Локация","Описание",
            "Линк","Снимка_URL","Дата_обява","Източник","ID_обява",
            "Спешност","Телефон","Тип_имот"]

    df = pd.DataFrame(all_rows, columns=COLS)

    if df.empty:
        final_cols = ["Тип_имот","Локация-район","Площ_квм","Цена","Цена_кв.м","Телефон",
                      "Описание","Източник","Снимка_URL","Дата_обява","Спешност"]
        pd.DataFrame(columns=final_cols).to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
        print(f"\n⚠️ Няма намерени обяви → {OUTPUT_FILE}")
        return

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

    # Финален ред на колоните за CSV
    df["Локация-район"] = df["Локация"]
    df["Цена_кв.м"] = df["Цена_на_квм"]
    df["Описание"] = df["Описание"].fillna("").map(lambda x: clean_text(str(x), 800))
    final_cols = ["Тип_имот","Локация-район","Площ_квм","Цена","Цена_кв.м","Телефон",
                  "Описание","Източник","Снимка_URL","Дата_обява","Спешност"]
    df = df[final_cols]

    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

    print(f"\n✅ ГОТОВО — {len(df)} уникални обяви → {OUTPUT_FILE}")
    print(df["Източник"].map(site_from_link).value_counts().to_string())
    print(df["Тип_имот"].value_counts().to_string())


if __name__ == "__main__":
    main()
