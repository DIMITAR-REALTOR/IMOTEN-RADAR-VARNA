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
OLX_LOGIN_URL = os.environ.get("OLX_LOGIN_URL", "https://www.olx.bg/")
OLX_STORAGE_STATE_B64 = os.environ.get("OLX_STORAGE_STATE_B64", "")
ALO_STORAGE_STATE_B64 = os.environ.get("ALO_STORAGE_STATE_B64", "")
BROWSER_STORAGE_STATE_B64 = os.environ.get("BROWSER_STORAGE_STATE_B64", "")
OUTPUT_FILE  = "data/properties_varna.csv"
MAX_PAGES    = int(os.environ.get("MAX_PAGES", "25"))
MAX_OLX_PAGES = int(os.environ.get("MAX_OLX_PAGES", str(MAX_PAGES)))
MAX_ALO_PAGES = int(os.environ.get("MAX_ALO_PAGES", str(MAX_PAGES)))
MAX_IMOT_PAGES = int(os.environ.get("MAX_IMOT_PAGES", str(MAX_PAGES)))
SCRAPE_ALO = os.environ.get("SCRAPE_ALO", "1") == "1"
SCRAPE_IMOT = os.environ.get("SCRAPE_IMOT", "1") == "1"
SCRAPE_OLX = os.environ.get("SCRAPE_OLX", "1") == "1"
OLX_FETCH_PHONES = os.environ.get("OLX_FETCH_PHONES", "0") == "1"
# imot.bg по подразбиране взима само краткото резюме от страницата със списъка
# (по-бързо, по-малко риск от блокиране). Ако се включи, за всяка обява се
# отваря и детайлната страница за по-пълно описание — по-бавно и със селектори,
# които не са тествани "на живо" (виж README, "Известни ограничения").
IMOT_FETCH_FULL_DESC = os.environ.get("IMOT_FETCH_FULL_DESC", "0") == "1"
HISTORY_DIR  = "data/history"
BGN_TO_EUR   = 1.95583

PHONE_RE  = re.compile(r'(?<!\d)(?:\+359[\s\-]?|0)\d(?:[\s\-]?\d){8}(?!\d)')
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
    r'агенция|брокер|имоти\b|недвижими\s+имоти|real\s+estate|estate|properties|properti|инвест|invest|agency', re.I
)
PRIVATE_WORDS = re.compile(
    r'собственик|частно\s+лице|без\s+посредник|лично', re.I
)

# ── Наеми, промъкнали се сред обявите за продажба ──────────────────
# Търсим само в "продажби" раздели на сайтовете, но понякога наемна
# обява се промъква (грешна категория на подателя, или сайтът я показва
# и в двата раздела). Разпознаваме я по текст и/или по неправдоподобно
# ниска цена за съответния тип имот.
RENT_RE = re.compile(
    r'под\s*наем|pod[\s\-]?naem|дава(?:м|ме)?\s*под\s*наем|лв\.?\s*/\s*мес|€\s*/\s*мес|'
    r'eur\s*/\s*мес|на\s*месец|наем(?:а)?\s*(?:на|за)|самостоятелна\s+стая|за\s+квартирант',
    re.I,
)
PRICE_FLOOR_SALE = {
    "апартамент": 5000, "къща": 5000, "гараж": 1000, "парцел": 500,
    "земеделска": 500, "офис": 1000, "търговски": 1000,
}

# ── "Боклук" текст, който понякога се прихваща заедно с описанието ──
# (навигация/категории на сайта — напр. "Бизнеси, Стаи, Услуги"), когато
# по-широк CSS селектор захване част от менюто на страницата вместо
# самото описание на обявата.
JUNK_NAV_WORDS = {
    "бизнеси", "стаи", "услуги", "имоти", "превозни средства", "работа",
    "електроника", "дом и градина", "мода и красота", "хоби, спорт и развлечения",
    "домашни любимци", "за децата", "обучение", "земеделие", "всички категории",
    "апартаменти", "къщи", "гаражи", "парцели", "офиси", "магазини", "недвижими имоти",
}

os.makedirs("data", exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)
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


def strip_nav_junk(text: str) -> str:
    """Маха навигационни/категорийни етикети на сайта (напр. 'Бизнеси, Стаи,
    Услуги'), ако са се промъкнали в текста на описанието — вижда се обикновено
    като къс списък от такива думи, разделени с запетая или '|'. Не пипа текст,
    който не съвпада с известните junk-думи (за да не изяде истинско описание)."""
    if not text:
        return text
    cleaned = text
    for word in JUNK_NAV_WORDS:
        pattern = r'(^|[,|]\s*)' + re.escape(word) + r'(\s*(?=[,|]|$))'
        cleaned = re.sub(pattern, r'\1', cleaned, flags=re.I)
    cleaned = re.sub(r'[,|]\s*[,|]+', ',', cleaned)
    cleaned = re.sub(r'^[\s,|]+|[\s,|]+$', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else text


_CITY_VARIANTS_RE = re.compile(r'^\s*(?:гр\.?\s*|град\s+)?варна\s*$', re.I)
# Хващаме "варна" в началото независимо дали има "гр."/"град" пред нея —
# "Варна, Левски 2" трябва да се сведе до "Левски", както и "гр. Варна, Левски".
_CITY_PREFIX_RE = re.compile(r'^(?:гр\.?\s*|град\s+)?варна\s*[,\-]?\s*', re.I)


def normalize_location(loc: str) -> str:
    """Обединява различни изписвания на едно и също място:
    'град Варна' / 'гр. Варна' / 'гр.Варна' / 'Варна' → 'Варна';
    'Левски 1' / 'Левски 2' / 'Левски' → 'Левски' — номерът на подрайона
    се маха по подразбиране, за консистентност в статистиките. Ако за
    определени квартали номерът трябва да се пази отделно, редакторът на
    тази функция е точното място за изключение."""
    if not loc:
        return loc
    loc = clean_text(loc)
    if _CITY_VARIANTS_RE.match(loc):
        return "Варна"
    loc = _CITY_PREFIX_RE.sub('', loc).strip()
    if not loc:
        return "Варна"
    loc = re.sub(r'\s+\d{1,2}$', '', loc).strip()
    return loc or "Варна"


def is_rent_listing(prop_type: str, price: "int | None", text: str) -> bool:
    """Обявата изглежда за под наем, не за продажба — по текст или по
    неправдоподобно ниска цена спрямо типа имот."""
    if RENT_RE.search(text or ""):
        return True
    floor = PRICE_FLOOR_SALE.get(prop_type, 0)
    if floor and price is not None and price < floor:
        return True
    return False


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
        m = re.search(r'/obiava-([a-z0-9]+)-', url) or re.search(r'adv=(\d+)', url)
    elif source == "alo":
        m = re.search(r'/(\d+)/?$', url) or re.search(r'-(\d+)\.html', url)
    else:
        m = None
    if m:
        return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:10]


def click_first(page, selectors: list[str], timeout=4000) -> bool:
    """Пробва списък от селектори за клик. Първо бърза (без чакане) проверка
    дали селекторът изобщо съществува в DOM-а — за да не се чака целия
    `timeout` за всеки от много "опитни" селектори, които не съществуват
    на конкретния сайт (иначе N селектора × timeout се сумират)."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
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
            if locator.count() == 0:
                continue
            locator.wait_for(state="visible", timeout=timeout)
            locator.fill(value, timeout=timeout)
            return True
        except Exception:
            pass
    return False


def has_login_form(page) -> bool:
    selectors = [
        'input[name="username"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="username"]',
        'input[name="password"]',
        'input[type="password"]',
    ]
    for selector in selectors:
        try:
            if page.locator(selector).count():
                return True
        except Exception:
            pass
    return False


def open_olx_login(page):
    entry_urls = [
        OLX_LOGIN_URL,
        "https://www.olx.bg/",
        "https://www.olx.bg/nedvizhimi-imoti/varna/",
    ]
    profile_selectors = [
        'a:has-text("Твоя профил")',
        'button:has-text("Твоя профил")',
        '[role="button"]:has-text("Твоя профил")',
        'a:has-text("Твоят профил")',
        'button:has-text("Твоят профил")',
        '[role="button"]:has-text("Твоят профил")',
        'a:has-text("Моят профил")',
        'button:has-text("Моят профил")',
        'a[href*="login.olx.bg"]',
        'a[href*="/myaccount"]',
        'a[href*="/account"]',
        '[data-testid*="login"]',
        '[data-testid*="account"]',
        '[data-cy*="login"]',
        '[data-cy*="account"]',
    ]

    last_error = None
    for entry_url in dict.fromkeys(entry_urls):
        try:
            page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)
            rand_sleep(2, 4)
            click_first(page, [
                "#onetrust-accept-btn-handler",
                "button[id*='accept']",
                "button:has-text('Accept')",
                "button:has-text('Accept all')",
                "button:has-text('Приемам')",
                "button:has-text('Съгласен')",
                "button:has-text('Разбрах')",
            ], timeout=2500)
            if has_login_form(page):
                return
            if click_first(page, profile_selectors, timeout=6000):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                rand_sleep(2, 4)
                if has_login_form(page) or "login.olx.bg" in page.url:
                    return
        except Exception as e:
            last_error = e

    try:
        page.goto("https://login.olx.bg/", wait_until="domcontentloaded", timeout=30000)
        rand_sleep(2, 4)
    except Exception as e:
        last_error = e

    if last_error and not has_login_form(page):
        raise last_error


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
    ], timeout=2500)
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

    # Вход с акаунт - по подразбиране е изключен, защото OLX блокира automation login.
    if not OLX_FETCH_PHONES:
        print("  OLX: телефоните са изключени (OLX_FETCH_PHONES=0)")
    elif OLX_STORAGE_STATE_B64 or BROWSER_STORAGE_STATE_B64:
        olx_logged_in = True
        print("  OLX: използвам запазена login сесия")
    elif OLX_FETCH_PHONES and OLX_EMAIL and OLX_PASSWORD:
        print("  OLX: влизане в акаунт...")
        try:
            open_olx_login(page)
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

    # Обхождаме и двата вида продавачи — частни лица И агенции — за да могат
    # да се анализират/сравняват после (преди агенционните обяви изобщо не се
    # сваляха, филтърът беше на ниво URL). Всеки ред пази "Тип_продавач".
    SELLER_PASSES = [
        ("private", "частно лице"),
        ("business", "агенция"),
    ]

    for private_business_qs, seller_label in SELLER_PASSES:
        base_url = (
            "https://www.olx.bg/nedvizhimi-imoti/varna/"
            f"?search%5Bprivate_business%5D={private_business_qs}"
            "&search%5Border%5D=created_at:desc"
        )

        current_page = 1
        while current_page <= MAX_OLX_PAGES:
            url = base_url if current_page == 1 else f"{base_url}&page={current_page}"
            print(f"  OLX/{seller_label} стр.{current_page}")
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

                        # Забележка: тук НЕ ползваме широк ':has-text("Описание")' селектор —
                        # той понякога захваща цялото меню/навигацията на страницата вместо
                        # само описанието на обявата (оттам "Бизнеси, Стаи, Услуги" боклук в текста).
                        desc_el = detail.query_selector('[data-cy="ad_description"], [data-testid="ad-description"]')
                        if desc_el:
                            desc_full = clean_text(strip_nav_junk(f"{title} | {desc_el.inner_text()}"), 600)

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
                        "Тип_продавач": seller_label,
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

ALO_CATEGORIES = [
    "apartamenti-stai",
    "kashti-vili",
    "parceli-za-zastroiavane-investicionni-proekti",
    "garaji-parkomesta",
    "magazini-ofisi",
    "zemedelska-zemia-gradini-lozia-gora",
    "promishleni-pomeshtenia-skladove",
]
ALO_VARNA_QS = "region_id=3&location_ids=554"


def scrape_alo(context) -> list[dict]:
    """Сайтът смени изцяло структурата си (стар URL /obiavi/nedvizhimi-imoti/varna/
    вече връща 404). Ново: отделна категория на тип имот + филтър по регион/град
    чрез query параметри region_id/location_ids (Варна = 3/554). Няма вече вграден
    филтър "само частни лица" — филтрираме client-side по AGENCY_WORDS."""
    rows = []
    page = context.new_page()

    for category in ALO_CATEGORIES:
        base_url = f"https://www.alo.bg/obiavi/imoti-prodajbi/{category}/?{ALO_VARNA_QS}"
        current_page = 1
        while current_page <= MAX_ALO_PAGES:
            url = base_url if current_page == 1 else f"{base_url}&page={current_page}"
            print(f"  ALO/{category} стр.{current_page}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                rand_sleep(1, 2)
            except PwTimeout:
                print("  ALO: timeout")
                break

            cards = page.query_selector_all(".listtop-item")
            if not cards:
                if current_page == 1:
                    print(f"  ALO/{category}: няма обяви за Варна")
                break

            for card in cards:
                try:
                    link_el = card.query_selector("a[href]")
                    link = link_el.get_attribute("href") if link_el else ""
                    if not link:
                        continue
                    if not link.startswith("http"):
                        link = "https://www.alo.bg" + link

                    title_el = card.query_selector(".listtop-item-title")
                    title = title_el.inner_text().strip() if title_el else ""

                    pub_el = card.query_selector(".listtop-publisher span")
                    publisher = pub_el.inner_text().strip() if pub_el else ""

                    # По-рано тук прескачахме обявите от агенции. Сега ги пазим,
                    # само таг-нати — за да могат агенция vs. частно лице да се
                    # анализират/сравняват после (виж "Тип_продавач" по-долу).
                    seller_label = "агенция" if AGENCY_WORDS.search(f"{publisher} {title}") else "частно лице"

                    loc_el = card.query_selector(".listtop-item-address")
                    loc = loc_el.inner_text().strip() if loc_el else "Варна"

                    price_raw = ""
                    ppm_raw = ""
                    type_raw = ""
                    sqm_raw = ""
                    for row_el in card.query_selector_all(".ads-params-row"):
                        label_el = row_el.query_selector(".ads-param-title")
                        val_el = row_el.query_selector(".ads-params-cell")
                        if not label_el or not val_el:
                            continue
                        label = label_el.inner_text().strip()
                        val = val_el.inner_text().strip()
                        if label.startswith("Цена"):
                            price_raw = val
                        elif label.startswith("за кв.м"):
                            ppm_raw = val
                        elif label.startswith("Вид на имота"):
                            type_raw = val
                        elif label.startswith("Квадратура"):
                            sqm_raw = val

                    eur = to_eur(price_raw)
                    sqm = extract_sqm(sqm_raw) or extract_sqm(title)
                    ppm_match = re.search(r'([\d.,]+)\s*€/кв\.?м', ppm_raw, re.I)
                    ppm = float(ppm_match.group(1).replace(",", ".")) if ppm_match else None

                    desc_el = card.query_selector(".listtop-desc")
                    desc_full = clean_text(strip_nav_junk(f"{title} | {desc_el.inner_text() if desc_el else ''}"), 600)

                    img_el = card.query_selector("img.listtop-image-img")
                    img = img_el.get_attribute("src") if img_el else ""
                    if img and not img.startswith("http"):
                        img = "https://www.alo.bg/" + img.lstrip("/")

                    # Отваряме детайл страница за телефон (и площ ако липсва)
                    phone = ""
                    try:
                        detail = context.new_page()
                        detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                        rand_sleep(1.5, 2.5)

                        detail_text = detail.inner_text("body")
                        if not sqm:
                            sqm = extract_sqm(detail_text)

                        phone = reveal_phone(detail)
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
                        "Дата_обява": "",
                        "Източник":   link,
                        "ID_обява":   extract_id_from_url(link, "alo"),
                        "Спешност":   is_urgent(desc_full),
                        "Телефон":    phone,
                        "Тип_имот":   detect_type(f"{type_raw} {desc_full}"),
                        "Тип_продавач": seller_label,
                    })
                except Exception as e:
                    print(f"  ALO card грешка: {e}")

            current_page += 1
            rand_sleep(2, 3)

    page.close()
    print(f"  ALO: {len(rows)} обяви")
    return rows


# ══════════════════════════════════════════════════════════════════
# IMOT.BG
# ══════════════════════════════════════════════════════════════════

def scrape_imot(context) -> list[dict]:
    """Обхожда списъка с обяви (без да отваря всяка обява поотделно)."""
    rows = []
    page = context.new_page()

    base_url = "https://www.imot.bg/obiavi/prodazhbi/grad-varna"

    current_page = 1
    while current_page <= MAX_IMOT_PAGES:
        url = base_url if current_page == 1 else f"{base_url}/p-{current_page}?sort=2"
        print(f"  imot.bg стр.{current_page}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            rand_sleep(1, 2)
        except PwTimeout:
            print("  imot.bg: timeout")
            break

        cards = page.query_selector_all(".ads2023 > div.item")
        if not cards:
            print("  imot.bg: няма повече")
            save_debug_page(page, f"imot_page_{current_page}_debug")
            break

        page_count = 0
        for card in cards:
            try:
                # По-рано тук прескачахме обявите от агенции (маркер ".seller").
                # Сега ги пазим, само таг-нати — виж "Тип_продавач" по-долу.
                seller_label = "агенция" if card.query_selector(".seller") else "частно лице"

                title_el = card.query_selector(".zaglavie a.title")
                if not title_el:
                    continue

                link = title_el.get_attribute("href") or ""
                if link.startswith("//"):
                    link = "https:" + link
                elif link.startswith("/"):
                    link = "https://www.imot.bg" + link

                type_title = title_el.evaluate(
                    "el => Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim()"
                )

                loc_el = card.query_selector(".zaglavie a.title location")
                loc = loc_el.inner_text().strip() if loc_el else "Варна"

                price_el = card.query_selector(".zaglavie .price div")
                price_raw = price_el.inner_text().strip() if price_el else ""
                eur = to_eur(price_raw)

                info_el = card.query_selector(".info")
                info_text = info_el.inner_text().strip() if info_el else ""

                phone = extract_phones(info_text)
                sqm = extract_sqm(f"{type_title} {info_text}")
                ppm = round(eur / sqm, 2) if eur and sqm else None

                # Описание без номера накрая (телефонът вече е отделна колона)
                desc_clean = PHONE_RE.sub("", info_text)
                desc_clean = re.sub(r',?\s*тел\.:\s*$', "", desc_clean).strip(" ,")
                full_desc = clean_text(strip_nav_junk(f"{type_title}, {loc} | {desc_clean}"), 800)

                # По подразбиране описанието на imot.bg е само краткото резюме
                # от списъка с обяви (сайтът не показва пълния текст там).
                # При IMOT_FETCH_FULL_DESC=1 пробваме да вземем и пълния текст
                # от детайлната страница — селекторите по-долу не са потвърдени
                # "на живо" (виж README), затова е best-effort с fallback към
                # горното резюме, ако нищо не съвпадне.
                if IMOT_FETCH_FULL_DESC and link:
                    try:
                        detail = context.new_page()
                        detail.goto(link, wait_until="domcontentloaded", timeout=15000)
                        rand_sleep(0.8, 1.5)
                        for sel in ['.description', '#description_div', '[itemprop="description"]',
                                    '.obiava-description', '.text-content']:
                            desc_el = detail.query_selector(sel)
                            if desc_el:
                                txt = clean_text(strip_nav_junk(desc_el.inner_text()))
                                if len(txt) > 40:
                                    full_desc = clean_text(f"{type_title}, {loc} | {txt}", 800)
                                    break
                        detail.close()
                    except Exception:
                        pass

                img_el = card.query_selector(".photo img.pic")
                img = img_el.get_attribute("src") or "" if img_el else ""
                if img.startswith("//"):
                    img = "https:" + img

                is_new = card.query_selector("new strong") is not None

                # Тип имот — първо по заглавието на imot.bg (по-точно),
                # после fallback към общата детекция по текст
                title_up = type_title.upper()
                if "ГАРАЖ" in title_up or "ПАРКОМЯСТО" in title_up:
                    prop_type = "гараж"
                elif "ПАРЦЕЛ" in title_up:
                    prop_type = "парцел"
                elif "КЪЩА" in title_up or "ВИЛА" in title_up:
                    prop_type = "къща"
                elif "ОФИС" in title_up:
                    prop_type = "офис"
                elif any(w in title_up for w in ("МАГАЗИН", "СКЛАД", "ЗАВЕДЕНИЕ", "ХОТЕЛ", "ПРОМ.")):
                    prop_type = "търговски"
                elif "СТАЕН" in title_up or "МЕЗОНЕТ" in title_up or "АТЕЛИЕ" in title_up:
                    prop_type = "апартамент"
                else:
                    prop_type = detect_type(f"{type_title} {info_text}")

                rows.append({
                    "Цена":        eur,
                    "Площ_квм":   sqm,
                    "Цена_на_квм": ppm,
                    "Локация":    loc,
                    "Описание":   full_desc,
                    "Линк":       link,
                    "Снимка_URL": img,
                    "Дата_обява": "Нова обява" if is_new else "",
                    "Източник":   link,
                    "ID_обява":   extract_id_from_url(link, "imot"),
                    "Спешност":   is_urgent(full_desc),
                    "Телефон":    phone,
                    "Тип_имот":   prop_type,
                    "Тип_продавач": seller_label,
                })
                page_count += 1
            except Exception as e:
                print(f"  imot.bg card грешка: {e}")

        print(f"  imot.bg: {page_count} обяви на стр.{current_page}")

        # ЗАБЕЛЕЖКА: спираме САМО когато страницата няма карти (виж по-горе).
        # Преди тук проверявахме и линка "Напред" (a.next) и спирахме ако липсва,
        # но той понякога не се появява в отговора към бота дори когато следващата
        # страница реално има резултати — затова вече не разчитаме на него.
        current_page += 1
        rand_sleep(2, 4)

    page.close()
    print(f"  imot.bg: {len(rows)} обяви общо")
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
        storage_state_b64 = BROWSER_STORAGE_STATE_B64 or OLX_STORAGE_STATE_B64 or ALO_STORAGE_STATE_B64
        if storage_state_b64:
            context_options["storage_state"] = json.loads(base64.b64decode(storage_state_b64).decode("utf-8"))
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
            "Спешност","Телефон","Тип_имот","Тип_продавач"]

    df = pd.DataFrame(all_rows, columns=COLS)

    if df.empty:
        final_cols = ["Тип_имот","Тип_продавач","Локация-район","Площ_квм","Цена","Цена_кв.м","Телефон",
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

    # Маха обяви под наем, промъкнали се сред резултатите за продажба
    def _row_is_rent(row) -> bool:
        price = row["Цена"]
        price = None if pd.isna(price) else int(price)
        return is_rent_listing(row["Тип_имот"], price, str(row["Описание"]))

    rent_mask = df.apply(_row_is_rent, axis=1)
    n_rent = int(rent_mask.sum())
    if n_rent:
        print(f"\n🏠 Премахнати {n_rent} обяви под наем, промъкнали се сред резултатите за продажба")
    df = df[~rent_mask].reset_index(drop=True)

    # Нормализация на локацията (град Варна / гр. Варна / Варна → Варна;
    # Левски 1 / Левски 2 / Левски → Левски)
    df["Локация"] = df["Локация"].map(normalize_location)

    # Финален ред на колоните за CSV
    df["Локация-район"] = df["Локация"]
    df["Цена_кв.м"] = df["Цена_на_квм"]
    df["Описание"] = df["Описание"].fillna("").map(lambda x: clean_text(strip_nav_junk(str(x)), 800))
    final_cols = ["Тип_имот","Тип_продавач","Локация-район","Площ_квм","Цена","Цена_кв.м","Телефон",
                  "Описание","Източник","Снимка_URL","Дата_обява","Спешност"]
    df = df[final_cols]

    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

    # Отделен датиран файл в data/history/ — пази история от run до run,
    # вместо всеки следващ scrape да трие предишния (OUTPUT_FILE се презаписва всеки път).
    history_file = f"{HISTORY_DIR}/properties_varna_{date.today().isoformat()}.csv"
    df.to_csv(history_file, index=False, encoding="utf-8-sig")

    print(f"\n✅ ГОТОВО — {len(df)} уникални обяви → {OUTPUT_FILE} и {history_file}")
    print(df["Източник"].map(site_from_link).value_counts().to_string())
    print(df["Тип_имот"].value_counts().to_string())
    print(df["Тип_продавач"].value_counts().to_string())


if __name__ == "__main__":
    main()
