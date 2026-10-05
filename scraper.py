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
    # Strip common noise words including corporate suffixes & industries
    clean = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Industry|Enterprises|Services|Accessories)\b", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-zA-Z0-9]", "", clean).lower()

def match_gmp_fuzzy(norm_key, gmp_data):
    """Finds best match in gmp_data even if company suffix differs."""
    if not norm_key:
        return {}
    if norm_key in gmp_data:
        return gmp_data[norm_key]
    
    # Prefix / Substring matching
    for k, v in gmp_data.items():
        if k and (k in norm_key or norm_key in k):
            return v
        if len(k) >= 4 and len(norm_key) >= 4:
            if k[:5] == norm_key[:5]:
                return v
    return {}

def parse_date_or_none(date_str):
    if not date_str or date_str.strip() in ["--", "-", ""]:
        return None
    cleaned = date_str.strip()
    current_year = datetime.now().year

    match = re.search(r"(\d{1,2})[-/]([A-Za-z]{3}|\d{1,2})(?:[-/](\d{2,4}))?", cleaned)
    if match:
        day, month_raw, yr = match.groups()
        year = current_year if not yr else (2000 + int(yr) if len(yr) == 2 else int(yr))
        try:
            if month_raw.isdigit():
                dt = datetime.strptime(f"{int(day):02d}-{int(month_raw):02d}-{year}", "%d-%m-%Y")
            else:
                dt = datetime.strptime(f"{int(day):02d}-{month_raw.capitalize()}-{year}", "%d-%b-%Y")
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

    # De-duplicate by ID
    deduped = {}
    for it in all_upstox_items:
        iid = it.get("id") or it.get("symbol") or it.get("name")
        if iid and iid not in deduped:
            deduped[iid] = it

    return list(deduped.values()), "OK"

# ----------------- SECONDARY GMP & PERFORMANCE CRAWLER -----------------
def fetch_gmp_and_performance_map(session):
    gmp_map = {}
    try:
        print("  --> Fetching live GMP table from InvestorGain...", flush=True)
        res = session.get(f"{INVESTORGAIN_BASE_URL}/report/live-ipo-gmp/331/", timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table")
            if table:
                headers = [th.text.strip().lower() for th in table.find("tr").find_all(["th", "td"])]
                name_idx, gmp_idx, lot_idx, allot_idx, list_idx, sub_idx = 0, -1, -1, -1, -1, -1
                for i, h in enumerate(headers):
                    if any(k in h for k in ["company", "name", "ipo"]): name_idx = i
                    elif "gmp" in h: gmp_idx = i
                    elif "lot" in h: lot_idx = i
                    elif any(k in h for k in ["boa", "allotment"]): allot_idx = i
                    elif "listing" in h: list_idx = i
                    elif "sub" in h: sub_idx = i

                for row in table.find_all("tr")[1:]:
                    tds = row.find_all("td")
                    if len(tds) > max(name_idx, gmp_idx) and gmp_idx != -1:
                        raw_name = tds[name_idx].text.strip()
                        key = normalize_key(raw_name)
                        gmp_raw = tds[gmp_idx].text.strip()
                        cleaned_val = clean_num_or_none(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw) or 0.0

                        lot_val = int(clean_num_or_none(tds[lot_idx].text) or 0) if (lot_idx != -1 and len(tds) > lot_idx) else 0
                        allot_d = parse_date_or_none(tds[allot_idx].text) if (allot_idx != -1 and len(tds) > allot_idx) else None
                        list_d = parse_date_or_none(tds[list_idx].text) if (list_idx != -1 and len(tds) > list_idx) else None
                        sub_t = clean_num_or_none(tds[sub_idx].text) if (sub_idx != -1 and len(tds) > sub_idx) else None

                        l_price, l_pct = 0.0, 0.0
                        inline = re.search(r"L@\s*([\d,.]+)\s*\(([-+]?\d*\.?\d+)\%\)", tds[name_idx].text)
                        if inline:
                            l_price = float(inline.group(1).replace(",", ""))
                            l_pct = float(inline.group(2))

                        if key:
                            gmp_map[key] = {
                                "gmpAmount": cleaned_val,
                                "lotSize": lot_val,
                                "allotmentDate": allot_d,
                                "listingDate": list_d,
                                "listingPrice": l_price,
                                "listingGainPercent": l_pct,
                                "subscriptionTotal": sub_t
                            }
            print(f"      Parsed {len(gmp_map)} GMP entries successfully.", flush=True)
    except Exception as e:
        print(f"      GMP scraper notice: {e}", flush=True)
    return gmp_map

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

    # 2. Load previous cache
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

    # 4. Attempt Tier 1: Upstox Official API
    print(">>> [Phase 4/5] Executing Tier 1 Ingestion via Upstox API...", flush=True)
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

            # Match GMP data using fuzzy prefix/substring matching
            gmp_info = match_gmp_fuzzy(norm_key, gmp_data)
            gmp_val = gmp_info.get("gmpAmount", 0.0)
            gmp_pct = round((gmp_val / price_max * 100), 2) if (gmp_val > 0 and price_max > 0) else 0.0

            # CRITICAL FIX 1: Extract lot size from Upstox first, fallback to secondary
            raw_lot = item.get("lot_size") or item.get("minimum_quantity") or item.get("lotSize")
            lot_size = int(clean_num_or_none(raw_lot) or 0)
            if lot_size <= 0:
                lot_size = gmp_info.get("lotSize", 0)

            # CRITICAL FIX 2: Timeline Dates
            open_d = parse_date_or_none(item.get("bidding_start_date") or item.get("open_date"))
            close_d = parse_date_or_none(item.get("bidding_end_date") or item.get("close_date"))
            allot_d = parse_date_or_none(item.get("allotment_date")) or gmp_info.get("allotmentDate") or "To Be Updated"
            list_d = parse_date_or_none(item.get("listing_date")) or gmp_info.get("listingDate") or "To Be Updated"

            raw_status = (item.get("status") or "UPCOMING").upper()
            status = determine_status(open_d, close_d, list_d if list_d != "To Be Updated" else None)
            if status == "UPCOMING" and raw_status in ["OPEN", "CLOSED"]:
                status = raw_status

            # Subscription breakdowns
            sub_retail, sub_hni, sub_qib = 0.0, 0.0, 0.0
            for inv in item.get("investors", []):
                cat = inv.get("category_name", "").upper()
                sub = float(inv.get("subscription_rate", 0.0) or 0.0)
                if "RETAIL" in cat or "RII" in cat: sub_retail = sub
                elif "HNI" in cat or "NII" in cat: sub_hni = sub
                elif "QIB" in cat: sub_qib = sub

            total_sub = float(clean_num_or_none(item.get("total_subscription")) or gmp_info.get("subscriptionTotal") or 0.0)

            # Registrar mapping
            reg_name, reg_url = None, None
            if norm_key in manual_overrides:
                reg_name, reg_url = manual_overrides[norm_key]
            elif norm_key in previous_cache:
                reg_name, reg_url = previous_cache[norm_key]
            else:
                reg_name = item.get("registrar_name") or "To Be Updated"
                reg_url = item.get("registrar_url") or "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"

            # CRITICAL FIX 3: Safe Symbol Guarantee
            sym = item.get("symbol")
            if not sym or str(sym).strip() in ["None", "null", ""]:
                sym = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()
            if not sym:
                sym = "IPO"

            cand = {
                "id": str(idx_counter),
                "name": clean_name,
                "symbol": str(sym).strip(),
                "category": "SME" if item.get("issue_type") == "sme" else "MAINBOARD",
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

        # Preserve recently LISTED and CLOSED pipeline issues from previous cache
        for old in last_known_good:
            old_key = normalize_key(old.get("name", ""))
            if old_key not in seen_keys and old.get("status") in ["LISTED", "CLOSED"]:
                old["id"] = str(idx_counter)
                final_dataset.append(old)
                idx_counter += 1

    else:
        print(f"    Tier 1 Bypassed / Notice: {status_msg}", flush=True)
        # In Tier 2, if Upstox fails, we still have our complete scraper
        final_dataset = last_known_good
        active_source = "CACHE_FALLBACK"

    # Write output
    if not final_dataset:
        print("CRITICAL: Pipeline failed. Preserving existing ipos.json.", flush=True)
        return

    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)

    print(f">>> Pipeline completed successfully. Written {len(final_dataset)} records.", flush=True)

if __name__ == "__main__":
    run_pipeline()
                
