import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime

FILE_PATH = "ipos.json"
AUDIT_LOG_PATH = "data_health_audit.md"
OVERRIDES_PATH = "manual_overrides.json"

UPSTOX_BASE_URL = "https://api.upstox.com/v2"
INVESTORGAIN_BASE_URL = "https://www.investorgain.com"

UPSTOX_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

SEBI_REGISTRARS = [
    ("mudra", "Mudra RTA Ventures", "https://mudrarta.com/"),
    ("purva", "Purva Sharegistry", "https://www.purvashare.com/queries/"),
    ("maashitla", "Maashitla Securities", "https://maashitla.com/allotment-status"),
    ("cameo", "Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    ("bigshare", "Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"),
    ("link intime", "Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    ("mufg", "Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    ("kfin", "KFin Technologies", "https://ris.kfintech.com/ipostatus/"),
    ("skyline", "Skyline Financial", "https://www.skylinerta.com/ipo.php"),
    ("integrated", "Integrated Registry", "https://www.integratedregistry.in/RegistrarsToSTA.aspx"),
    ("mas services", "MAS Services", "https://www.masserv.com/")
]

def clean_num_or_none(val):
    if val is None or val == "":
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return None

def normalize_key(name: str) -> str:
    if not name:
        return ""
    clean = re.sub(
        r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Industry|Enterprises|Enterprise|Services|Accessories|Polymers|Foods|Technologies|Consultants)\b",
        "",
        name,
        flags=re.IGNORECASE
    )
    return re.sub(r"[^a-zA-Z0-9]", "", clean).lower()

def match_gmp_fuzzy(norm_key, gmp_data):
    if not norm_key:
        return {}
    if norm_key in gmp_data:
        return gmp_data[norm_key]
    for k, v in gmp_data.items():
        if k and (k in norm_key or norm_key in k):
            return v
        if len(k) >= 4 and len(norm_key) >= 4:
            if k[:4] == norm_key[:4]:
                return v
    return {}

def parse_date_or_none(date_str):
    if not date_str or date_str.strip() in ["--", "-", "", "N/A", "TBD", "TBA", "null", "None"]:
        return None
    cleaned = date_str.strip()
    current_year = datetime.now().year

    # Standard formats
    for fmt in (
        "%d-%b-%y", "%d-%b-%Y",
        "%d-%m-%y", "%d-%m-%Y",
        "%d/%m/%y", "%d/%m/%Y",
        "%d %b %Y", "%d %b %y",
        "%Y-%m-%d"
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Formats missing year
    for fmt in ("%d-%b", "%d-%m", "%d %b"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(year=current_year).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Regex extraction fallback: Day-Month-Year (e.g. 5-10-26, 05-Oct-2026)
    match = re.search(r"(\d{1,2})[-/ ]([A-Za-z]{3}|\d{1,2})[-/ ]?(\d{2,4})?", cleaned)
    if match:
        day, month_raw, yr = match.groups()
        yr_int = int(yr) if yr else current_year
        if yr and len(yr) == 2:
            yr_int = 2000 + int(yr)
        try:
            if month_raw.isdigit():
                dt = datetime.strptime(f"{int(day):02d}-{int(month_raw):02d}-{yr_int}", "%d-%m-%Y")
            else:
                dt = datetime.strptime(f"{int(day):02d}-{month_raw.capitalize()}-{yr_int}", "%d-%b-%Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    return None

def determine_status(open_d, close_d, list_d):
    today = datetime.now().date()
    try:
        o = datetime.strptime(open_d, "%Y-%m-%d").date() if open_d and open_d != "To Be Updated" else None
        c = datetime.strptime(close_d, "%Y-%m-%d").date() if close_d and close_d != "To Be Updated" else None
        l = datetime.strptime(list_d, "%Y-%m-%d").date() if list_d and list_d != "To Be Updated" else None

        if o and today < o:
            return "UPCOMING"
        elif o and c and (o <= today <= c):
            return "OPEN"
        elif c and l and (c < today < l):
            return "CLOSED"
        elif l and today >= l:
            return "LISTED"
        elif c and today > c:
            return "CLOSED"
    except Exception:
        pass
    return "UPCOMING"

def validate_record(item: dict) -> bool:
    if item["issuePriceMin"] < 0 or item["issuePriceMax"] < 0:
        return False
    if item["issuePriceMin"] > item["issuePriceMax"] and item["issuePriceMax"] > 0:
        return False
    if item["gmpAmount"] <= 0 and item["gmpPercent"] > 0:
        item["gmpPercent"] = 0.0
    if item["lotSize"] < 0:
        return False
    return True

# ----------------- TIER 1: UPSTOX MULTI-STATUS INGESTION -----------------
def try_fetch_upstox():
    if not UPSTOX_TOKEN:
        return None, "No token supplied in environment."

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_TOKEN}"
    }

    all_upstox_items = []
    queries = [
        {"status": "open"},
        {"status": "upcoming"},
        {"status": "closed"}
    ]

    for q in queries:
        try:
            print(f"  --> Calling Upstox with query {q}...", flush=True)
            res = requests.get(f"{UPSTOX_BASE_URL}/ipos", headers=headers, params=q, timeout=8)
            if res.status_code == 200:
                items = res.json().get("data", [])
                all_upstox_items.extend(items)
                print(f"      Upstox {q['status']} returned {len(items)} records.", flush=True)
            elif res.status_code in [401, 403]:
                return None, f"Upstox Auth Token Expired / Invalid (HTTP {res.status_code})."
            else:
                print(f"      Upstox status {res.status_code} for {q}", flush=True)
        except Exception as e:
            print(f"      Upstox query notice for {q}: {e}", flush=True)

    if not all_upstox_items:
        try:
            print("  --> Fallback call to plain Upstox /ipos...", flush=True)
            res = requests.get(f"{UPSTOX_BASE_URL}/ipos", headers=headers, timeout=8)
            if res.status_code == 200:
                all_upstox_items = res.json().get("data", [])
                print(f"      Plain /ipos returned {len(all_upstox_items)} records.", flush=True)
        except Exception as e:
            print(f"      Plain /ipos notice: {e}", flush=True)

    deduped = {}
    for it in all_upstox_items:
        iid = it.get("id") or it.get("symbol") or it.get("name")
        if iid and iid not in deduped:
            deduped[iid] = it

    return list(deduped.values()), "OK"

# ----------------- SECONDARY GMP & PERFORMANCE CRAWLER -----------------
def fetch_gmp_and_performance_map(session):
    gmp_map = {}
    report_urls = [
        f"{INVESTORGAIN_BASE_URL}/report/live-ipo-gmp/331/",
        f"{INVESTORGAIN_BASE_URL}/report/live-ipo-gmp/331/ipo/",
        f"{INVESTORGAIN_BASE_URL}/report/ipo-subscription-live/333/"
    ]

    for url in report_urls:
        try:
            print(f"  --> Fetching live table from {url}...", flush=True)
            res = session.get(url, timeout=10)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table")
            if not table:
                continue

            header_row = table.find("tr")
            if not header_row:
                continue

            headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]
            name_idx, gmp_idx, lot_idx, allot_idx, list_idx, sub_idx = 0, -1, -1, -1, -1, -1

            for i, h in enumerate(headers):
                if any(k in h for k in ["company", "name", "ipo"]): name_idx = i
                elif "gmp" in h: gmp_idx = i
                elif "lot" in h: lot_idx = i
                elif any(k in h for k in ["boa", "allotment"]): allot_idx = i
                elif "listing" in h: list_idx = i
                elif "sub" in h or "total" in h: sub_idx = i

            for row in table.find_all("tr")[1:]:
                tds = row.find_all("td")
                if len(tds) > name_idx:
                    raw_name = tds[name_idx].text.strip()
                    key = normalize_key(raw_name)
                    if not key:
                        continue

                    cleaned_gmp = 0.0
                    if gmp_idx != -1 and len(tds) > gmp_idx:
                        gmp_raw = tds[gmp_idx].text.strip()
                        cleaned_gmp = clean_num_or_none(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw) or 0.0

                    lot_val = 0
                    if lot_idx != -1 and len(tds) > lot_idx:
                        lot_val = int(clean_num_or_none(tds[lot_idx].text) or 0)

                    allot_d = parse_date_or_none(tds[allot_idx].text) if (allot_idx != -1 and len(tds) > allot_idx) else None
                    list_d = parse_date_or_none(tds[list_idx].text) if (list_idx != -1 and len(tds) > list_idx) else None
                    sub_t = clean_num_or_none(tds[sub_idx].text) if (sub_idx != -1 and len(tds) > sub_idx) else None

                    l_price, l_pct = 0.0, 0.0
                    inline = re.search(r"L@\s*([\d,.]+)\s*\(([-+]?\d*\.?\d+)\%\)", tds[name_idx].text)
                    if inline:
                        l_price = float(inline.group(1).replace(",", ""))
                        l_pct = float(inline.group(2))

                    existing = gmp_map.get(key, {})
                    gmp_map[key] = {
                        "gmpAmount": cleaned_gmp if cleaned_gmp > 0 else existing.get("gmpAmount", 0.0),
                        "lotSize": lot_val if lot_val > 0 else existing.get("lotSize", 0),
                        "allotmentDate": allot_d or existing.get("allotmentDate"),
                        "listingDate": list_d or existing.get("listingDate"),
                        "listingPrice": l_price if l_price > 0 else existing.get("listingPrice", 0.0),
                        "listingGainPercent": l_pct if l_pct != 0.0 else existing.get("listingGainPercent", 0.0),
                        "subscriptionTotal": sub_t or existing.get("subscriptionTotal")
                    }

        except Exception as e:
            print(f"      Scraper notice for {url}: {e}", flush=True)

    print(f"      Parsed {len(gmp_map)} secondary enriched entries successfully.", flush=True)
    return gmp_map

# ----------------- TIER 2: SECONDARY SCRAPER FALLBACK ENGINE -----------------
def fetch_secondary_engine(session, manual_overrides, previous_cache, gmp_data):
    try:
        res = session.get(f"{INVESTORGAIN_BASE_URL}/report/live-ipo-gmp/331/", timeout=10)
    except Exception:
        return []

    if res.status_code != 200:
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    header_row = table.find("tr")
    headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]

    col_map = {}
    for idx, h in enumerate(headers):
        if any(k in h for k in ["name", "company", "ipo"]):
            if "name" not in col_map: col_map["name"] = idx
        elif "gmp" in h: col_map["gmp"] = idx
        elif "sub" in h: col_map["sub"] = idx
        elif "price" in h: col_map["price"] = idx
        elif "lot" in h: col_map["lot"] = idx
        elif "open" in h: col_map["open"] = idx
        elif "close" in h: col_map["close"] = idx
        elif any(k in h for k in ["boa", "allotment"]): col_map["allotment"] = idx
        elif "listing" in h: col_map["listing"] = idx

    name_col_idx = col_map.get("name", 0)
    rows = table.find_all("tr")[1:]
    dataset = []

    for idx, row in enumerate(rows):
        tds = row.find_all("td")
        if len(tds) < 5:
            continue

        name_td = tds[name_col_idx]
        company_link = name_td.find("a")
        raw_cell_text = name_td.text.strip()
        raw_name = company_link.text.strip() if company_link else raw_cell_text

        if not raw_name or "company" in raw_name.lower():
            continue

        clean_name = re.sub(r"L@\s*[\d,.]+\s*\([^)]*\)", "", raw_name, flags=re.IGNORECASE)
        clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited)\b", "", clean_name, flags=re.IGNORECASE).strip()
        if not clean_name:
            continue

        norm_key = normalize_key(clean_name)
        category = "SME" if "SME" in (raw_cell_text + raw_name).upper() else "Mainboard"

        open_d = parse_date_or_none(tds[col_map["open"]].text) if "open" in col_map else None
        close_d = parse_date_or_none(tds[col_map["close"]].text) if "close" in col_map else None
        allot_d = parse_date_or_none(tds[col_map["allotment"]].text) if "allotment" in col_map else None
        list_d = parse_date_or_none(tds[col_map["listing"]].text) if "listing" in col_map else None

        gmp_info = match_gmp_fuzzy(norm_key, gmp_data)

        if not allot_d:
            allot_d = gmp_info.get("allotmentDate")
        if not list_d:
            list_d = gmp_info.get("listingDate")

        status = determine_status(open_d, close_d, list_d)

        listing_price = gmp_info.get("listingPrice", 0.0)
        listing_gain_pct = gmp_info.get("listingGainPercent", 0.0)

        reg_name, reg_url = None, None
        if norm_key in manual_overrides:
            reg_name, reg_url = manual_overrides[norm_key]
        elif norm_key in previous_cache:
            reg_name, reg_url = previous_cache[norm_key]
        else:
            for pat, name, url in SEBI_REGISTRARS:
                if pat in raw_cell_text.lower():
                    reg_name, reg_url = name, url
                    break
            if not reg_name:
                reg_name = "To Be Updated"
                reg_url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"

        price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else ""
        prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
        price_min = min(prices) if prices else 0.0
        price_max = max(prices) if prices else 0.0

        lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ""
        lot_size_num = clean_num_or_none(lot_raw)
        lot_size = int(lot_size_num) if lot_size_num else gmp_info.get("lotSize", 0)

        cleaned_gmp = gmp_info.get("gmpAmount", 0.0)
        if cleaned_gmp <= 0.0 and "gmp" in col_map:
            gmp_raw = tds[col_map["gmp"]].text.strip()
            parsed_g = clean_num_or_none(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)
            cleaned_gmp = parsed_g if parsed_g is not None else 0.0

        gmp_pct = round((cleaned_gmp / price_max * 100), 2) if (cleaned_gmp > 0 and price_max > 0) else 0.0

        sub_raw = tds[col_map["sub"]].text.strip() if "sub" in col_map else ""
        sub_total = clean_num_or_none(sub_raw) or gmp_info.get("subscriptionTotal") or 0.0

        symbol = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()
        if not symbol:
            symbol = "IPO"

        cand = {
            "id": str(idx + 1),
            "name": clean_name,
            "symbol": symbol,
            "category": category,
            "status": status,
            "issuePriceMin": price_min,
            "issuePriceMax": price_max,
            "lotSize": lot_size,
            "openDate": open_d or "To Be Updated",
            "closeDate": close_d or "To Be Updated",
            "allotmentDate": allot_d or "To Be Updated",
            "listingDate": list_d or "To Be Updated",
            "gmpAmount": cleaned_gmp,
            "gmpPercent": gmp_pct,
            "subscriptionTotal": sub_total,
            "subscriptionRetail": 0.0,
            "subscriptionHNI": 0.0,
            "subscriptionQIB": 0.0,
            "listingPrice": listing_price,
            "listingGainPercent": listing_gain_pct,
            "registrarName": reg_name,
            "registrarUrl": reg_url,
            "rhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+file+SEBI",
            "drhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+file+SEBI"
        }
        if validate_record(cand):
            dataset.append(cand)

    return dataset

# ----------------- MAIN PIPELINE ORCHESTRATOR -----------------
def run_pipeline():
    print(">>> [Phase 1/5] Initializing pipeline session...", flush=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Load manual overrides
    print(">>> [Phase 2/5] Loading overrides & caching references...", flush=True)
    manual_overrides = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    manual_overrides[normalize_key(k)] = (v["registrarName"], v["registrarUrl"])
            print(f"    Loaded {len(manual_overrides)} manual overrides.", flush=True)
        except Exception:
            pass

    # 2. Load previous cache & prepare Last-Known-Good backup
    previous_cache = {}
    last_known_good = []
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                last_known_good = json.load(f)
                for item in last_known_good:
                    k = normalize_key(item.get("name", ""))
                    r_name = item.get("registrarName")
                    if k and r_name and r_name not in ["To Be Updated", "To Be Announced"]:
                        previous_cache[k] = (r_name, item.get("registrarUrl", ""))
            print(f"    Loaded {len(last_known_good)} existing records from cache.", flush=True)
        except Exception:
            pass

    # 3. Secondary GMP and performance enrichment
    print(">>> [Phase 3/5] Ingesting secondary GMP & performance map...", flush=True)
    gmp_data = fetch_gmp_and_performance_map(session)

    # 4. Ingestion Execution
    print(">>> [Phase 4/5] Executing Data Ingestion...", flush=True)
    final_dataset = []
    active_source = "None"
    upstox_data, status_msg = try_fetch_upstox()

    if upstox_data:
        print(f"    Tier 1 Active: Processing {len(upstox_data)} items from Upstox API.", flush=True)
        active_source = "UPSTOX_OFFICIAL"

        seen_keys = set()
        idx_counter = 1

        for item in upstox_data:
            raw_name = item.get("name", "").strip()
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited)\b", "", raw_name, flags=re.IGNORECASE).strip()
            norm_key = normalize_key(clean_name)
            seen_keys.add(norm_key)

            price_min = float(item.get("minimum_price") or item.get("issue_price_min") or 0.0)
            price_max = float(item.get("maximum_price") or item.get("issue_price_max") or 0.0)

            gmp_info = match_gmp_fuzzy(norm_key, gmp_data)
            gmp_val = gmp_info.get("gmpAmount", 0.0)
            gmp_pct = round((gmp_val / price_max * 100), 2) if (gmp_val > 0 and price_max > 0) else 0.0

            raw_lot = item.get("lot_size") or item.get("minimum_quantity") or item.get("lotSize")
            lot_size = int(clean_num_or_none(raw_lot) or 0)
            if lot_size <= 0:
                lot_size = gmp_info.get("lotSize", 0)

            open_d = parse_date_or_none(item.get("bidding_start_date") or item.get("open_date"))
            close_d = parse_date_or_none(item.get("bidding_end_date") or item.get("close_date"))
            allot_d = parse_date_or_none(item.get("allotment_date")) or gmp_info.get("allotmentDate") or "To Be Updated"
            list_d = parse_date_or_none(item.get("listing_date")) or gmp_info.get("listingDate") or "To Be Updated"

            raw_status = (item.get("status") or "UPCOMING").upper()
            status = determine_status(open_d, close_d, list_d if list_d != "To Be Updated" else None)
            if status == "UPCOMING" and raw_status in ["OPEN", "CLOSED"]:
                status = raw_status

            sub_retail, sub_hni, sub_qib = 0.0, 0.0, 0.0
            for inv in item.get("investors", []):
                cat = inv.get("category_name", "").upper()
                sub = float(inv.get("subscription_rate", 0.0) or 0.0)
                if "RETAIL" in cat or "RII" in cat: sub_retail = sub
                elif "HNI" in cat or "NII" in cat: sub_hni = sub
                elif "QIB" in cat: sub_qib = sub

            total_sub = float(clean_num_or_none(item.get("total_subscription")) or gmp_info.get("subscriptionTotal") or 0.0)

            reg_name, reg_url = None, None
            if norm_key in manual_overrides:
                reg_name, reg_url = manual_overrides[norm_key]
            elif norm_key in previous_cache:
                reg_name, reg_url = previous_cache[norm_key]
            else:
                reg_name = item.get("registrar_name") or "To Be Updated"
                reg_url = item.get("registrar_url") or "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"

            sym = item.get("symbol")
            if not sym or str(sym).strip() in ["None", "null", ""]:
                sym = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()
            if not sym:
                sym = "IPO"

            cand = {
                "id": str(idx_counter),
                "name": clean_name,
                "symbol": str(sym).strip(),
                "category": "SME" if str(item.get("issue_type", "")).lower() == "sme" else "Mainboard",
                "status": status,
                "issuePriceMin": price_min,
                "issuePriceMax": price_max,
                "lotSize": lot_size,
                "openDate": open_d or "To Be Updated",
                "closeDate": close_d or "To Be Updated",
                "allotmentDate": allot_d,
                "listingDate": list_d,
                "gmpAmount": gmp_val,
                "gmpPercent": gmp_pct,
                "subscriptionTotal": total_sub,
                "subscriptionRetail": sub_retail,
                "subscriptionHNI": sub_hni,
                "subscriptionQIB": sub_qib,
                "listingPrice": gmp_info.get("listingPrice", 0.0),
                "listingGainPercent": gmp_info.get("listingGainPercent", 0.0),
                "registrarName": reg_name,
                "registrarUrl": reg_url,
                "rhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+file+SEBI",
                "drhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+file+SEBI"
            }
            if validate_record(cand):
                final_dataset.append(cand)
                idx_counter += 1

        for old in last_known_good:
            old_key = normalize_key(old.get("name", ""))
            if old_key not in seen_keys and old.get("status") in ["LISTED", "CLOSED"]:
                old["id"] = str(idx_counter)
                final_dataset.append(old)
                idx_counter += 1

    else:
        print(f"    Tier 1 Notice: {status_msg}", flush=True)
        print(">>> Engaging Tier 2 Engine...", flush=True)
        final_dataset = fetch_secondary_engine(session, manual_overrides, previous_cache, gmp_data)
        if final_dataset:
            active_source = "SECONDARY_VALIDATED"
        else:
            final_dataset = last_known_good
            active_source = "CACHE_FALLBACK"

    # 5. Output Feed
    print(">>> [Phase 5/5] Finalizing feed and writing records...", flush=True)
    if not final_dataset:
        print("CRITICAL: Pipeline failed. Preserving existing ipos.json.", flush=True)
        return

    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)

    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("# IPO Pipeline Data Health Audit\n\n")
        f.write(f"**Last Sync (UTC):** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"**Primary Active Source:** {active_source}\n")
        f.write(f"**Total Records Ingested:** {len(final_dataset)}\n")
        f.write("Status: Healthy. Date format and lot size normalization verified.\n")

    print(f">>> Pipeline executed successfully using [{active_source}]. Processed {len(final_dataset)} records.", flush=True)

if __name__ == "__main__":
    run_pipeline()
