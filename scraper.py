import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from bs4 import BeautifulSoup
import time
import re
import os
import random
import json
import subprocess
import tempfile
import shutil
from copy import copy


def kill_driver_tree(pid):
    """Best-effort kill of the chromedriver process tree (by PID).

    Kept as a backup. Chrome re-parents its real browser process away from
    chromedriver almost immediately, so /T usually finds an empty tree —
    that's why we ALSO kill by profile marker (see kill_chrome_by_profile).
    """
    if not pid:
        return
    if os.name != "nt":
        return  # taskkill is Windows-only; driver.quit() handles cleanup on Linux
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def kill_chrome_by_profile(profile_marker):
    """Kill every chrome.exe whose command line contains profile_marker.

    This is how we reliably target THIS batch's Chrome windows. We pass a
    unique --user-data-dir per batch, so chrome.exe processes for that
    batch all have the marker in their command line. Other scrapers'
    Chrome windows have different markers and are untouched.
    """
    if not profile_marker:
        return
    if os.name != "nt":
        return  # PowerShell is Windows-only; driver.quit() handles cleanup on Linux
    ps_script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile_marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except Exception:
        pass

# Configuration
BATCH_SIZE = 40  # Number of URLs to scrape before taking a break


# ----------------------------
# Utility functions
# ----------------------------
def parse_jpy(text):
    if not text:
        return None
    text = text.replace(",", "").strip()
    if "万" in text:
        num = float(re.findall(r"[\d.]+", text)[0])
        return int(num * 10000)
    digits = re.findall(r"\d+", text)
    return int(digits[0]) if digits else None


# ----------------------------
# Characteristics extractor
# ----------------------------
def extract_characteristics(soup, property_overview=None):
    characteristics = {}

    # Method 1: dl/dt/dd format (older pages — chintai/room)
    main_container = soup.select_one(
        "div.bg-white.lg\\:rounded-sm.pt-4.lg\\:pt-6.lg\\:pb-10.lg\\:px-6"
    )
    if main_container:
        inner = main_container.find("div", class_="-mx-4 lg:mx-0")
        if inner:
            for dl in inner.find_all("dl"):
                for row in dl.find_all("div", recursive=False):
                    dt = row.find("dt")
                    dd = row.find("dd")
                    if not dt:
                        continue
                    key = dt.get_text(strip=True)
                    value = None
                    if dd:
                        raw = dd.get_text(" ", strip=True)
                        if raw and raw != "-":
                            value = raw
                    if key:
                        characteristics[key] = value

    # Method 2: SUMMARY TABLE — outside section#about
    tables = soup.select("table.w-full.table-fixed")
    for table in tables:
        if table.find_parent("section", id="about"):
            continue
        for row in table.find_all("tr"):
            if row.get("data-component") == "TableRow":
                continue
            th = row.find("th")
            td = row.find("td")
            if not (th and td):
                continue
            key = th.get_text(" ", strip=True)
            if not key:
                continue

            td_copy = copy(td)
            for tag in td_copy.find_all(["button", "img", "svg", "form"]):
                tag.decompose()
            for a in td_copy.find_all("a"):
                a.decompose()
            for div in td_copy.find_all("div"):
                txt = div.get_text(" ", strip=True).lower()
                if any(kw in txt for kw in [
                    "estimated payment", "calculation by", "loan details",
                    "mortgage loan assessment", "mogecheck", "discuss payment",
                    "*calculation", "i'd like to see it", "book a tour",
                    "please feel free", "概算", "目安",
                ]):
                    div.decompose()
            for hidden in td_copy.find_all(attrs={"data-disclosure-target": "panel"}):
                classes = hidden.get("class", [])
                hidden["class"] = [c for c in classes if c != "!hidden"]

            paragraphs = td_copy.find_all(["p", "li"])
            if paragraphs:
                parts = [p.get_text(" ", strip=True) for p in paragraphs]
                parts = [p for p in parts if p]
                raw_value = " | ".join(parts) if parts else td_copy.get_text(" ", strip=True)
            else:
                spans = td_copy.find_all("span", recursive=False)
                if spans:
                    parts = [s.get_text(" ", strip=True) for s in spans]
                    parts = [p for p in parts if p]
                    raw_value = " | ".join(parts) if parts else td_copy.get_text(" ", strip=True)
                else:
                    raw_value = td_copy.get_text(" ", strip=True)

            raw_value = re.sub(r"\s+", " ", raw_value).strip()
            value = raw_value if raw_value and raw_value != "-" else None

            if key in characteristics:
                if value is not None:
                    characteristics[key] = value
            else:
                characteristics[key] = value

    # Method 3: grid layout (tochi / kodate pages)
    grid_containers = soup.find_all(
        "div",
        class_=lambda c: c and "grid" in c and "grid-cols-max1fr" in c
    )
    for grid in grid_containers:
        children = [c for c in grid.find_all(recursive=False)
                    if c.name in ("span", "div")]
        i = 0
        while i < len(children) - 1:
            key_el = children[i]
            val_el = children[i + 1]
            if key_el.name == "span" and val_el.name == "div":
                key = key_el.get_text(" ", strip=True)
                val_copy = copy(val_el)
                for tag in val_copy.find_all(["button", "img", "svg", "form"]):
                    tag.decompose()
                paragraphs = val_copy.find_all(["p", "li"])
                if paragraphs:
                    parts = [p.get_text(" ", strip=True) for p in paragraphs]
                    parts = [p for p in parts if p]
                    raw_value = " | ".join(parts) if parts else val_copy.get_text(" ", strip=True)
                else:
                    raw_value = val_copy.get_text(" ", strip=True)
                raw_value = re.sub(r"\s+", " ", raw_value).strip()
                value = raw_value if raw_value and raw_value != "-" else None
                if key:
                    if key in characteristics:
                        if value is not None:
                            characteristics[key] = value
                    else:
                        characteristics[key] = value
                i += 2
            else:
                i += 1

    # FALLBACK: derive from property_overview if everything else empty
    if not characteristics and property_overview:
        SUMMARY_KEYS_MAP = {
            "Floor plan": "Floor plan",
            "area": ["Building area", "land area", "Land area/tsubo"],
            "parking": "parking",
            "Year built": "Year of construction",
            "traffic": "traffic",
            "location": "location",
            "Land rights": "Land rights",
        }
        for summary_key, source in SUMMARY_KEYS_MAP.items():
            if isinstance(source, list):
                parts = []
                for k in source:
                    v = property_overview.get(k)
                    if v:
                        parts.append(f"{k}: {v}")
                if parts:
                    characteristics[summary_key] = " ".join(parts)
            else:
                v = property_overview.get(source)
                if v:
                    characteristics[summary_key] = v

    return characteristics


# ----------------------------
# Property overview extractor
# ----------------------------
def extract_property_overview(soup):
    """Extract Property Overview from section#about (tochi/kodate/sale)
    and from older dl/dt/dd layouts. Hardened against mortgage simulator
    and footer region junk."""
    property_overview = {}

    # Junk keys we never want, even if they bleed in from other widgets
    JUNK_KEY_PATTERNS = [
        "mortgage", "principal", "interest rate", "loan",
        "repair reserve", "terms of use", "business hours",
        "closed on", "ローン", "返済", "金利",
        "hokkaido", "tohoku", "kanto", "kinki", "kyushu",
        "chugoku", "shikoku", "tokai", "hokuriku", "okinawa",
        "買い物", "グルメ", "娯楽施設", "文化施設", "役所",
        "医療", "保育施設", "学校", "周辺施設", "避難所", "学区",
    ]

    def is_junk_key(key):
        if not key:
            return True
        k = key.lower()
        return any(p in k for p in JUNK_KEY_PATTERNS)

    # ============================
    # Method 1: #about > TABLE  (tochi / kodate / sale pages)
    # The container can be <section id="about"> OR <div id="about">.
    # Find ANY table inside that container, not just one with strict classes.
    # ============================
    about_section = soup.find("section", id="about")
    if not about_section:
        # Fallback for pages that use <div id="about"> instead
        about_section = soup.find(id="about")
    if about_section:
        # Try strict class first, then fall back to ANY table inside #about
        table = about_section.find(
            "table",
            class_=lambda c: c and "w-full" in c and "table-fixed" in c
        )
        if not table:
            table = about_section.find("table")

        if table:
            for row in table.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if not (th and td):
                    continue
                key = th.get_text(" ", strip=True)
                if not key or is_junk_key(key):
                    continue

                td_copy = copy(td)

                # Drop pure-junk tags entirely
                for tag in td_copy.find_all(
                    ["button", "img", "svg", "form", "template"]
                ):
                    tag.decompose()
                for a in td_copy.find_all("a"):
                    a.decompose()

                # modal-kit wraps the value AND a help-icon modal — unwrap it
                # so the visible text ("Building restrictions apply") survives
                for mk in td_copy.find_all("modal-kit"):
                    mk.unwrap()

                # Remove ONLY tour CTA blocks; be precise so we don't eat the
                # parent that holds the real value ("Vacant land", etc.)
                for div in td_copy.find_all("div"):
                    txt = div.get_text(" ", strip=True).lower()
                    # only strip blocks that are ENTIRELY a CTA, not blocks
                    # that happen to contain one
                    cta_keywords = ["book a tour", "please feel free",
                                    "概算", "目安", "想定"]
                    if any(kw in txt for kw in cta_keywords):
                        # length check + must NOT also contain real content
                        # like "vacant", "available", etc.
                        has_real_content = any(rc in txt for rc in [
                            "vacant", "available", "occupied", "delivered",
                            "consultation", "空き", "居住中",
                        ])
                        if len(txt) < 200 and not has_real_content:
                            div.decompose()

                # Prefer <p>/<li> structure; fall back to plain text
                paragraphs = td_copy.find_all(["p", "li"])
                if paragraphs:
                    parts = [p.get_text(" ", strip=True) for p in paragraphs]
                    parts = [p for p in parts if p]
                    raw_value = " | ".join(parts) if parts else td_copy.get_text(" ", strip=True)
                else:
                    raw_value = td_copy.get_text(" ", strip=True)

                raw_value = re.sub(r"\s+", " ", raw_value).strip()
                value = raw_value if raw_value and raw_value != "-" else None
                property_overview[key] = value

    # ============================
    # Method 2: older "Property overview" h2 + dl  (chintai/room legacy pages)
    # ============================
    if not property_overview:
        overview_h2 = soup.find(
            ["h2", "h3"],
            string=lambda x: x and (
                "Property overview" in x or "物件概要" in x or "物件情報" in x
            )
        )
        dl_container = None
        if overview_h2:
            parent = overview_h2.find_parent(["section", "div"])
            if parent:
                dl_container = parent.find("dl")

        if dl_container:
            for row in dl_container.find_all("div", recursive=False):
                dt = row.find("dt")
                dd = row.find("dd")
                if not dt or not dd:
                    continue
                key = dt.get_text(" ", strip=True)
                if not key or is_junk_key(key):
                    continue

                dd_copy = copy(dd)
                for tag in dd_copy.find_all(["button", "img", "svg", "form", "a"]):
                    tag.decompose()
                parts = []
                for txt in dd_copy.stripped_strings:
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt and txt != "-":
                        parts.append(txt)
                value = " | ".join(parts) if parts else None
                property_overview[key] = value

    # ============================
    # Method 3: SCOPED fallback — only dt/dd inside a labeled overview container.
    # NEVER iterate the whole page (that's what was grabbing the mortgage
    # simulator, nearby facilities, and the region selector at the footer).
    # ============================
    if not property_overview:
        overview_container = None
        for heading in soup.find_all(["h2", "h3"]):
            heading_text = heading.get_text(" ", strip=True)
            if any(kw in heading_text for kw in [
                "Property overview", "物件概要", "物件情報"
            ]):
                overview_container = heading.find_parent(["section", "div"])
                if overview_container:
                    break

        if overview_container:
            for dt in overview_container.find_all("dt"):
                dd = dt.find_next("dd")
                if not dd:
                    continue
                key = dt.get_text(" ", strip=True)
                if not key or is_junk_key(key):
                    continue

                dd_copy = copy(dd)
                for tag in dd_copy.find_all(["button", "img", "svg", "form", "a"]):
                    tag.decompose()
                parts = []
                for txt in dd_copy.stripped_strings:
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt and txt != "-":
                        parts.append(txt)
                value = " | ".join(parts) if parts else None
                property_overview[key] = value

    return property_overview


# ----------------------------
# Main property parser
# ----------------------------
def parse_property(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    time.sleep(3)

    outdated_tag = soup.select_one('p.text-lg.lg\\:text-xl.font-bold.text-orange')
    if outdated_tag:
        text = outdated_tag.get_text(strip=True)
        if ("Currently, this property information is no longer posted" in text
            or "This property is currently closed" in text
            or "この物件情報は掲載を終了しています" in text
            or "This property listing is no longer available" in text):
            return "OUTDATED"

    # ============================
    # :one: STRICT PROPERTY NAME CHECK
    # ============================
    title_tag = soup.select_one(
        "h1.mt-1.mb-1.text-sm.detail-main-screen\\:text-base.font-bold"
    )
    if not title_tag:
        title_tag = soup.select_one(
            "span.break-words.lg\\:text-2xl.lg\\:font-bold.lg\\:leading-7"
        )
    if not title_tag:
        h1 = soup.select_one("h1.font-bold.lg\\:text-2xl")
        if h1:
            name_span = h1.select_one("span.break-words")
            if name_span:
                title_tag = name_span
            else:
                title_tag = h1

    if not title_tag:
        return None

    data["name"] = title_tag.get_text(strip=True)

    # ============================
    # :two: Property & Transaction Type
    # ============================
    type_tag = soup.select_one('span[class*="bg-mono-50"]')

    if type_tag:
        full_text = type_tag.get_text(" ", strip=True)
        words = full_text.split()
        if len(words) >= 2:
            data["transaction_type"] = words[0]
            data["property_type"] = words[1]
        else:
            data["transaction_type"] = full_text[:2] if len(full_text) >= 2 else full_text
            data["property_type"] = full_text[2:] if len(full_text) > 2 else None
    else:
        fallback_type_tag = soup.select_one(
            "span.rounded-full.bg-mono-100.px-2.py-0\\.5"
        )
        if fallback_type_tag:
            data["property_type"] = fallback_type_tag.get_text(strip=True)
        else:
            data["property_type"] = None

        data["transaction_type"] = None

        if not data["property_type"]:
            h1 = soup.find("h1")
            if h1:
                for span in h1.find_all("span", class_="rounded-full"):
                    txt = span.get_text(strip=True).lower()
                    if txt in ["land", "土地"]:
                        data["property_type"] = "land"
                        break
                    elif txt in ["mansion", "マンション"]:
                        data["property_type"] = "mansion"
                        break
                    elif txt in ["apartment", "アパート"]:
                        data["property_type"] = "apartment"
                        break
                    elif txt in ["house", "一戸建て"]:
                        data["property_type"] = "house"
                        break

    if not data.get("transaction_type"):
        url_lower = url.lower()
        if "/chintai/" in url_lower:
            data["transaction_type"] = "Rental"
        elif "/tochi/" in url_lower or "/kodate/" in url_lower or "/mansion/chuko/" in url_lower or "/mansion/shinchiku/" in url_lower:
            data["transaction_type"] = "Sale"
        elif data.get("name"):
            name_text = data["name"]
            if "売" in name_text or "sale" in name_text.lower():
                data["transaction_type"] = "Sale"
            elif "賃貸" in name_text or "rent" in name_text.lower():
                data["transaction_type"] = "Rental"

    if not data.get("transaction_type"):
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not (th and td):
                continue
            th_text = th.get_text(" ", strip=True)
            if th_text in ("Transaction type", "取引態様", "取引形態"):
                td_text = td.get_text(" ", strip=True)
                if td_text and td_text != "-":
                    data["transaction_type"] = td_text
                    break

    if not type_tag and not data.get("property_type"):
        return None

    # ============================
    # :three: Amenities
    # ============================
    amenities = [
        li.get_text(strip=True)
        for li in soup.select(
            'li.shrink-0.border.border-mono-200.rounded-sm.font-semibold.text-xs.text-mono-600'
        )
    ]

    if not amenities:
        equip_section = soup.select_one("section#equip")
        if equip_section:
            amenities = [
                li.get_text(strip=True)
                for li in equip_section.select("ul li.list-dot-brand")
            ]

    if not amenities:
        equip_parent = None
        for sec in soup.find_all("section"):
            h3 = sec.find("h3", recursive=False)
            if h3 and ("Equipment information" in h3.get_text() or "設備" in h3.get_text()):
                equip_parent = sec
                break

        if equip_parent:
            for inner_sec in equip_parent.find_all("section"):
                h4 = inner_sec.find("h4")
                if not h4:
                    continue
                category = h4.get_text(" ", strip=True)
                for li in inner_sec.select("ul li.list-dot-brand"):
                    text = li.get_text(" ", strip=True)
                    if text:
                        amenities.append(f"{category}: {text}")

    if not amenities:
        for sec in soup.find_all("section"):
            h4 = sec.find("h4")
            if h4:
                category = h4.get_text(strip=True)
                ul = sec.find("ul")
                if ul:
                    for li in ul.find_all("li"):
                        text = li.get_text(strip=True)
                        if text:
                            amenities.append(f"{category}: {text}")

    data["amenities"] = amenities

    # ============================
    # :six: Conditions
    # ============================
    conditions = []
    condition_tags = soup.select(
        "p.font-bold.sm\\:text-sm.text-center.leading-\\[1\\.4\\].text-xxs"
    )
    for tag in condition_tags:
        text = tag.get_text(strip=True)
        if text:
            conditions.append(text)
    data["conditions"] = conditions

    # ============================
    # :seven: Additional Information
    # ============================
    additional_info = {}
    ul_tag = soup.select_one(
        "ul.mt-3.lg\\:mt-5.w-full.flex.flex-wrap.border-b.border-mono-200.text-sm"
    )
    if ul_tag:
        li_tags = ul_tag.find_all("li", recursive=False)
        for li in li_tags:
            key_tag = li.find("p")
            value_tag = li.find("div")
            if key_tag and value_tag:
                key = key_tag.get_text(strip=True)
                value = value_tag.get_text(" ", strip=True)
                if key:
                    additional_info[key] = value
    data["additional_information"] = additional_info

    # ============================
    # PROPERTY OVERVIEW
    # ============================
    property_overview = extract_property_overview(soup)
    data["property_overview"] = property_overview

    # ============================
    # :four: Characteristics (with Property_Overview fallback)
    # ============================
    characteristics = extract_characteristics(soup, property_overview)
    data["characteristics"] = characteristics

    # ============================
    # :five: Prices
    # ============================
    data["price"] = parse_jpy(characteristics.get("賃料"))

    if not data["price"]:
        for key in ["Rent", "rent", "賃料"]:
            if characteristics.get(key):
                data["price"] = characteristics.get(key)
                break

    if not data["price"]:
        price_tag = soup.select_one("span.text-\\[1\\.625rem\\]\\/\\[1\\.25rem\\].lg\\:text-2xl")
        if price_tag:
            parent_b = price_tag.find_parent("b")
            if parent_b:
                data["price"] = parent_b.get_text(" ", strip=True)
            else:
                data["price"] = price_tag.get_text(strip=True)

    if not data["price"]:
        price_tag = soup.select_one('p[data-component="price"]')
        if price_tag:
            data["price"] = price_tag.get_text(" ", strip=True)

    if not data["price"]:
        price_tag = soup.select_one("p.text-orange b.font-bold")
        if price_tag:
            data["price"] = price_tag.get_text(" ", strip=True)

    # Fallback: price from property overview
    if not data["price"] and isinstance(property_overview, dict):
        data["price"] = property_overview.get("price") or property_overview.get("Price")

    if not data["price"]:
        for font_tag in soup.find_all("font"):
            text = font_tag.get_text(" ", strip=True)
            if text and re.search(r"[\d,]+\s*(yen|円|万)", text, re.IGNORECASE):
                parent_text = font_tag.find_parent().get_text(" ", strip=True) if font_tag.find_parent() else ""
                if any(kw in parent_text.lower() for kw in ["estimated payment", "管理費", "management fee", "敷金", "deposit", "礼金", "key money"]):
                    continue
                data["price"] = text
                break

    # Management fee
    data["management_fee"] = extract_management_fee(soup, characteristics)

    # ============================
    # :nine: Coordinates
    # ============================
    data["latitude"] = None
    data["longitude"] = None

    map_window = soup.find("map-viewer-window", attrs={"data-map-info": True})
    if map_window:
        try:
            info = json.loads(map_window["data-map-info"])
            loc = info.get("buildingLocation", {})
            data["latitude"] = loc.get("latitude")
            data["longitude"] = loc.get("longitude")
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    if not data["latitude"]:
        map_viewer = soup.find("map-viewer-google-map", attrs={"data-lat": True, "data-lon": True})
        if map_viewer:
            try:
                data["latitude"] = float(map_viewer.get("data-lat"))
                data["longitude"] = float(map_viewer.get("data-lon"))
            except (ValueError, TypeError):
                pass

    if not data["latitude"]:
        for lat_attr, lon_attr in [("data-lat", "data-lon"), ("data-lat", "data-lng"),
                                    ("data-latitude", "data-longitude")]:
            el = soup.find(attrs={lat_attr: True, lon_attr: True})
            if el:
                try:
                    data["latitude"] = float(el[lat_attr])
                    data["longitude"] = float(el[lon_attr])
                    break
                except (ValueError, TypeError):
                    continue

    if not data["latitude"]:
        map_div = soup.find(attrs={"data-detail--map-surround-article-position-value": True})
        if map_div:
            try:
                coords = json.loads(map_div["data-detail--map-surround-article-position-value"])
                data["latitude"] = coords.get("lat")
                data["longitude"] = coords.get("lng")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    return data


# ----------------------------
# Helper: management fee extraction
# ----------------------------
def extract_management_fee(soup, characteristics):
    """Extract management fee with multiple fallback strategies."""

    DISCLAIMER_PHRASES = [
        "rounded up", "rounded down", "shown in units",
        "unit of 10,000", "units of 10,000", "after adding",
        "loan details", "loan simulator",
        "1万円単位", "切り上げ", "切り下げ",
        "概算", "目安", "想定",
        "repair reserve fund", "修繕積立金",
    ]

    def is_disclaimer(text):
        if not text:
            return False
        text_lower = text.lower()
        return any(phrase in text_lower for phrase in DISCLAIMER_PHRASES)

    fee = characteristics.get("管理費等") or characteristics.get("Management fees etc.")
    if fee:
        return fee

    for key in ["管理費", "Management fee", "Management fees", "管理費・共益費"]:
        if characteristics.get(key):
            return characteristics.get(key)

    for span in soup.find_all("span"):
        mr1 = span.find("span", class_="mr-1")
        if mr1 and ("Management fees" in mr1.get_text() or "管理費" in mr1.get_text()):
            fee_b = span.find("b")
            if fee_b:
                return fee_b.get_text(" ", strip=True)

    for label in soup.find_all(string=re.compile(r"(Management fee|管理費)", re.IGNORECASE)):
        parent = label.parent
        if not parent:
            continue
        if is_disclaimer(parent.get_text(" ", strip=True)):
            continue
        sibling = parent.find_next_sibling()
        if sibling:
            text = sibling.get_text(" ", strip=True)
            if re.search(r"[\d,]+\s*(yen|円)", text, re.IGNORECASE) and not is_disclaimer(text):
                return text
        b_tag = parent.find("b")
        if b_tag:
            text = b_tag.get_text(" ", strip=True)
            if text and re.search(r"[\d,]", text) and not is_disclaimer(text):
                return text

    for font_tag in soup.find_all("font"):
        text = font_tag.get_text(" ", strip=True)
        if not text or not re.search(r"[\d,]+\s*(yen|円)", text, re.IGNORECASE):
            continue
        parent = font_tag.find_parent()
        for _ in range(3):
            if not parent:
                break
            parent_text = parent.get_text(" ", strip=True).lower()
            if is_disclaimer(parent_text):
                parent = parent.find_parent()
                continue
            if "management fee" in parent_text or "管理費" in parent_text:
                if "rent" not in parent_text[:50].lower() and "賃料" not in parent_text[:50]:
                    return text
            parent = parent.find_parent()

    for span in soup.find_all("span", class_="font-bold"):
        text = span.get_text(" ", strip=True)
        if not text or not re.search(r"[\d,]+\s*(yen|円)", text, re.IGNORECASE):
            continue
        parent = span.find_parent()
        for _ in range(5):
            if not parent:
                break
            parent_text = parent.get_text(" ", strip=True).lower()
            if is_disclaimer(parent_text):
                parent = parent.find_parent()
                continue
            if ("management fee" in parent_text or "管理費" in parent_text
                or "common service fee" in parent_text or "共益費" in parent_text):
                snippet = parent_text[:100]
                if (("rent" in snippet or "賃料" in snippet)
                    and "management" not in snippet and "管理" not in snippet):
                    break
                return text
            parent = parent.find_parent()

    page_text = soup.get_text(" ", strip=True)

    match = re.search(
        r"Estimated payment[:\s]*([\d,]+\s*(?:yen|円))",
        page_text,
        re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    match = re.search(
        r"(?:想定支払額|月額目安|月々のお支払い|月々支払い)[:：\s]*([\d,]+\s*(?:yen|円))",
        page_text,
        re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    return None


# ----------------------------
# Selenium setup function
# ----------------------------
def init_driver(profile_dir):
    """Initialize a new Chrome driver instance with an isolated profile dir.

    The profile_dir is what lets us later kill ONLY this batch's chrome.exe
    processes (see kill_chrome_by_profile).
    """
    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    # Required to launch Chrome as root inside a Docker container; harmless on
    # Windows. --disable-dev-shm-usage avoids crashes from a small /dev/shm.
    if os.name != "nt":
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    options.add_experimental_option('prefs', {
        'translate_whitelists': {'ja': 'en'},
        'translate': {'enabled': True}
    })

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    return driver


# ----------------------------
# Batch scraping function
# ----------------------------
def scrape_batch(driver, urls_to_scrape, df_existing, output_csv, failed_txt, outdated_txt):
    """Scrape a batch of URLs with the current driver."""
    results = []

    for idx, url in enumerate(urls_to_scrape, start=1):
        print(f"[OPEN] ({idx}/{len(urls_to_scrape)}) {url}")

        try:
            driver.get(url)
            time.sleep(3)

            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "section#about"))
                )
            except Exception:
                pass

            driver.execute_script("window.scrollTo(0, 300);")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, 600);")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, 900);")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(2)

            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("""
                        const tables = document.querySelectorAll('table.w-full.table-fixed');
                        for (const t of tables) {
                            if (!t.closest('section#about')) {
                                return t.querySelectorAll('tr').length >= 5;
                            }
                        }
                        return false;
                    """)
                )
            except Exception:
                pass

            html = driver.page_source
            data = parse_property(html, url)
            print(data)

            if data == "OUTDATED":
                print(f"[OUTDATED] {url}")
                with open(outdated_txt, "a", encoding="utf-8") as f:
                    f.write(url + "\n")
                continue

            if data is None:
                print(f"[FAILED - CLOSED] {url}")
                with open(failed_txt, "a", encoding="utf-8") as f:
                    f.write(url + "\n")
                continue

            data["url"] = url
            results.append(data)

            df_new = pd.DataFrame(results)
            df_all = pd.concat([df_existing, df_new], ignore_index=True)

            for col in df_all.columns:
                df_all[col] = df_all[col].apply(
                    lambda x: " | ".join(x) if isinstance(x, list)
                    else " | ".join(f"{k}: {v}" for k, v in x.items() if v) if isinstance(x, dict)
                    else x
                )
            df_all.to_excel(output_csv, index=False, engine="openpyxl")
            print(f"added details: {url}")

            df_existing = df_all
            results = []

        except Exception as e:
            print(f"[ERROR] {url} → {e}")
            with open(failed_txt, "a", encoding="utf-8") as f:
                f.write(url + "\n")

    return df_existing


# ----------------------------
# Files
# ----------------------------
input_csv = r"failed_03_part3.csv"
output_csv = "output_3.xlsx"
failed_txt = "failed_urls_3.txt"
outdated_txt = "outdated_urls_3.txt"


df_urls = pd.read_csv(input_csv)
urls = df_urls["url"].dropna().tolist()

# ----------------------------
# Resume mechanism (URL-based)
# ----------------------------
if os.path.exists(output_csv):
    df_existing = pd.read_excel(output_csv, engine="openpyxl")
    processed_urls = set(df_existing["url"].dropna())
else:
    df_existing = pd.DataFrame()
    processed_urls = set()

failed_urls_set = set()
if os.path.exists(failed_txt):
    with open(failed_txt, "r", encoding="utf-8") as f:
        failed_urls_set = set(line.strip() for line in f if line.strip())

outdated_urls_set = set()
if os.path.exists(outdated_txt):
    with open(outdated_txt, "r", encoding="utf-8") as f:
        outdated_urls_set = set(line.strip() for line in f if line.strip())

processed_urls = processed_urls.union(failed_urls_set).union(outdated_urls_set)
urls_to_process = [url for url in urls if url not in processed_urls]

print(f"\n{'='*60}")
print(f"Japan Property Scraper - Batch Mode")
print(f"{'='*60}")
print(f"Total URLs: {len(urls)}")
print(f"Already processed: {len(processed_urls)}")
print(f"Remaining to process: {len(urls_to_process)}")
print(f"Batch size: {BATCH_SIZE}")
print(f"{'='*60}\n")

# ----------------------------
# Scraping loop with batching
# ----------------------------
try:
    current_idx = 0
    while current_idx < len(urls_to_process):
        batch_end = min(current_idx + BATCH_SIZE, len(urls_to_process))
        batch_urls = urls_to_process[current_idx:batch_end]
        print(f"\n:rocket: Starting batch: {current_idx + 1} to {batch_end} of {len(urls_to_process)}")
        driver = None
        driver_pid = None
        profile_dir = tempfile.mkdtemp(prefix="chrome_batch_")
        profile_marker = os.path.basename(profile_dir)
        try:
            driver = init_driver(profile_dir)
            try:
                driver_pid = driver.service.process.pid
            except Exception:
                driver_pid = None
            df_existing = scrape_batch(driver, batch_urls, df_existing, output_csv, failed_txt, outdated_txt)
        except Exception as e:
            print(f"\n:x: Error during batch: {e}")
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception as e:
                    print(f":warning: driver.quit() failed: {e}")
                driver = None
            kill_chrome_by_profile(profile_marker)
            kill_driver_tree(driver_pid)
            shutil.rmtree(profile_dir, ignore_errors=True)
            print(":lock: Browser closed for this batch")
        current_idx = batch_end
        if current_idx < len(urls_to_process):
            break_duration = random.randint(240, 420)

            print(f"\n:zzz: Batch complete! Taking a {break_duration // 60} minute {(break_duration % 60)} second break...")
            print(f":round_pushpin: Will resume at index {current_idx} after break.")
            time.sleep(break_duration)
            print(":alarm_clock: Break complete! Starting new session...")

    print("\n:white_check_mark: Scraping completed.")
    print(f":x: Failed URLs saved in: {failed_txt}")
    print(f":clock3: Outdated URLs saved in: {outdated_txt}")
except KeyboardInterrupt:
    print("\n\n:warning: Interrupted by user. Progress saved.")
except Exception as e:
    print(f"\n\n:x: Fatal error: {e}")
finally:
    print("\n" + "="*60)
    print("✓ Scraping session ended")
    print("="*60 + "\n")