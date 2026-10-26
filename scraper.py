import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, messaging

FILE_PATH = "ipos.json"
AUDIT_LOG_PATH = "data_health_audit.md"
OVERRIDES_PATH = "manual_overrides.json"
GMP_DATA_PATH = "gmp_data.json"

UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/"
}

SEBI_REGISTRARS = [
    ("kfin", "KFin Technologies", "https://ipostatus.kfintech.com/"),
    ("link intime", "Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    ("mufg", "Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    ("bigshare", "Bigshare Services", "https://www.bigshareonline.com/ipo_allotment.html"),
    ("cameo", "Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    ("maashitla", "Maashitla Securities", "https://maashitla.com/allotment-status/public-issues"),
    ("purva", "Purva Sharegistry", "https://www.purvashare.com/investor-service/ipo-query"),
    ("skyline", "Skyline Financial", "https://www.skylinerta.com/ipo.php"),
    ("integrated", "Integrated Registry", "https://ipostatus.integratedregistry.in/RegistrarsToSTAnew.aspx?OD=2"),
    ("mudra", "Mudra RTA Ventures", "https://www.mudrarta.com/ipo.php"),
    ("mas services", "MAS Services", "https://www.masserv.com/opt.asp"),
    ("accurate", "Accurate Securities", "https://accuratesecurities.com/ipo-details.html"),
    ("ankit", "Ankit Consultancy", "https://www.ankitonline.com/"),
    ("alankit", "Alankit Assignments", "https://www.alankit.com/"),
    ("abhipra", "Abhipra Capital", "https://www.abhipra.com/")
]

# ----------------- FIREBASE ADMIN INITIALIZATION -----------------
FIREBASE_CREDS_RAW = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
FIREBASE_APP_INITIALIZED = False

if FIREBASE_CREDS_RAW:
    try:
        cred_dict = json.loads(FIREBASE_CREDS_RAW)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        FIREBASE_APP_INITIALIZED = True
        print(">>> [Firebase] Admin SDK initialized successfully.", flush=True)
    except Exception as e:
        print(f">>> [Firebase] Initialization notice: {e}", flush=True)

def send_push_notification(title: str, body: str, ipo_id: str, channel_type: str = "market_updates"):
    if not FIREBASE_APP_INITIALIZED:
        return
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                "ipo_id": str(ipo_id),
                "channel_type": channel_type
            },
            topic="ipo_alerts"
        )
        response = messaging.send(message)
        print(f"      [FCM Push Sent] {title} -> Response: {response}", flush=True)
    except Exception as e:
        print(f"      [FCM Push Error] Failed to send '{title}': {e}", flush=True)

# ----------------- STRING & DATE UTILITIES -----------------
def clean_num_or_none(val):
    if val is None or val == "":
        return None
    val_str = str(val).strip().replace(",", "")
    m = re.search(r"[-+]?\d*\.?\d+", val_str)
    return float(m.group(0)) if m else None

def clean_company_name(name: str) -> str:
    if not name:
        return ""
    s = name.replace("’", "").replace("'", "")
    s = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Industry|Enterprises|Services|Home)\b", "", s, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", s.lower())

def get_tokens(name: str) -> set:
    if not name:
        return set()
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    noise = {"limited", "ltd", "pvt", "private", "ipo", "india", "sme", "technologies", "tech", "solutions", "industries", "services", "bse", "nse"}
    return {t for t in cleaned.split() if t not in noise and len(t) > 2}

def parse_date_or_none(date_str):
    if not date_str or str(date_str).strip() in ["--", "-", "", "N/A", "TBD", "TBA", "null", "None"]:
        return None
    cleaned = str(date_str).strip()
    current_year = datetime.now(IST).year

    for fmt in (
        "%d-%b-%y", "%d-%b-%Y",
        "%d-%m-%y", "%d-%m-%Y",
        "%d/%m/%y", "%d/%m/%Y",
        "%d %b %Y", "%d %b %y",
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"
    ):
        try:
            return datetime.strptime(cleaned.split("T")[0] if "T" in cleaned else cleaned, fmt.split("T")[0]).strftime("%Y-%m-%d")
        except ValueError:
            pass

    for fmt in ("%d-%b", "%d-%m", "%d %b"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(year=current_year).strftime("%Y-%m-%d")
        except ValueError:
            pass

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

def determine_status(open_d, close_d, list_d, raw_status=None, listing_price=0.0):
    now_ist = datetime.now(IST)
    today = now_ist.date()

    try:
        o = datetime.strptime(open_d, "%Y-%m-%d").date() if open_d and open_d != "To Be Updated" else None
        c = datetime.strptime(close_d, "%Y-%m-%d").date() if close_d and close_d != "To Be Updated" else None
        l = datetime.strptime(list_d, "%Y-%m-%d").date() if list_d and list_d != "To Be Updated" else None

        if o and today < o:
            return "UPCOMING"

        if o and c:
            if o <= today < c:
                return "OPEN"
            elif today == c:
                return "OPEN" if now_ist.hour < 17 else "CLOSED"

        if l:
            if today < l:
                return "CLOSED"
            elif today == l:
                is_after_10am = (now_ist.hour > 10) or (now_ist.hour == 10 and now_ist.minute >= 0)
                return "LISTED" if (is_after_10am and listing_price > 0.0) else "CLOSED"
            elif today > l:
                return "LISTED"

        if c and today > c:
            return "CLOSED"
    except Exception:
        pass

    if raw_status and raw_status.upper() in ["OPEN", "CLOSED", "LISTED", "UPCOMING"]:
        return raw_status.upper()

    return "UPCOMING"

def validate_record(item: dict) -> bool:
    if item["issuePriceMin"] < 0 or item["issuePriceMax"] < 0:
        return False
    if item["issuePriceMin"] > item["issuePriceMax"] and item["issuePriceMax"] > 0:
        return False
    if item["lotSize"] < 0:
        return False
    return True

# ----------------- DYNAMIC LIVE GMP WEB SCRAPER & STORAGE -----------------
def fetch_live_web_gmp() -> dict:
    """Scrapes live GMP values and discovered issues across all tables on InvestorGain."""
    gmp_map = {}
    urls = [
        "https://www.investorgain.com/report/live-ipo-gmp/331/ipo/",
        "https://www.investorgain.com/report/ipo-gmp-live/331/"
    ]
    for url in urls:
        try:
            res = requests.get(url, headers=WEB_HEADERS, timeout=12)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                for table in soup.find_all("table"):
                    for row in table.find_all("tr"):
                        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
                        if len(cells) >= 3:
                            name_cell = cells[0].strip()
                            clean_k = clean_company_name(name_cell)
                            for c in cells[1:]:
                                m = re.search(r"(?:₹|\+)\s*([\d,]+(?:\.\d+)?)", c)
                                if m:
                                    clean_val = float(m.group(1).replace(",", ""))
                                    if clean_k and clean_val > 0:
                                        gmp_map[clean_k] = {
                                            "name": name_cell,
                                            "gmp": clean_val
                                        }
                                    break
            if gmp_map:
                break
        except Exception as e:
            print(f">>> [Live GMP] Notice: Failed scraping from {url}: {e}", flush=True)

    print(f">>> [Live GMP] Successfully scraped {len(gmp_map)} live GMP quotes from web.", flush=True)
    return gmp_map

def load_existing_gmp():
    if os.path.exists(GMP_DATA_PATH):
        try:
            with open(GMP_DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def sync_and_save_gmp(all_items: list, existing_gmp: dict):
    web_gmp = fetch_live_web_gmp()
    today_str = datetime.now(IST).strftime("%d %b")
    updated_gmp_map = {}

    # Seed with live scraped GMP data
    for k, v in web_gmp.items():
        updated_gmp_map[k] = {
            "name": v["name"],
            "gmp": v["gmp"],
            "last_updated": today_str
        }

    # Retain existing entries if not in current web tables
    for item in all_items:
        norm_key = clean_company_name(item.get("name", ""))
        if norm_key not in updated_gmp_map:
            saved_entry = existing_gmp.get(norm_key, {})
            val = float(saved_entry.get("gmp") or 0.0) if isinstance(saved_entry, dict) else float(saved_entry or 0.0)
            updated_gmp_map[norm_key] = {
                "name": item.get("name", ""),
                "gmp": val,
                "last_updated": today_str
            }

    try:
        with open(GMP_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(updated_gmp_map, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f">>> [GMP] Sync save notice: {e}", flush=True)

    return updated_gmp_map

# ----------------- WEB PIPELINE FALLBACK -----------------
def scrape_live_ipo_feed() -> list:
    """Scrapes currently open and upcoming IPO issues directly from live tables."""
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/ipo/"
    discovered = []
    seen = set()
    try:
        res = requests.get(url, headers=WEB_HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for table in soup.find_all("table"):
                for row in table.find_all("tr"):
                    cells = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
                    if len(cells) >= 4:
                        raw_name = cells[0].strip()
                        if any(term in raw_name.lower() for term in ["ipo", "company", "price", "gmp"]) and len(raw_name) < 5:
                            continue
                        
                        clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited)\b", "", raw_name, flags=re.IGNORECASE).strip()
                        k = clean_company_name(clean_name)
                        if k and k not in seen and len(clean_name) > 2:
                            seen.add(k)
                            is_sme = "sme" in raw_name.lower()
                            discovered.append({
                                "name": clean_name,
                                "raw_name": raw_name,
                                "is_sme": is_sme
                            })
        print(f">>> [Live Feed Scraper] Discovered {len(discovered)} active issues via live web.", flush=True)
    except Exception as e:
        print(f">>> [Live Feed Scraper] Notice: {e}", flush=True)
    return discovered

# ----------------- UPSTOX INGESTION -----------------
def try_fetch_upstox():
    if not UPSTOX_TOKEN:
        return None, "No token supplied in environment."

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_TOKEN}"
    }

    all_upstox_items = []
    queries = [{"status": "open"}, {"status": "upcoming"}, {"status": "closed"}, {"status": "listed"}]

    for q in queries:
        try:
            res = requests.get(f"{UPSTOX_BASE_URL}/ipos", headers=headers, params=q, timeout=6)
            if res.status_code == 200:
                all_upstox_items.extend(res.json().get("data", []))
            elif res.status_code in [401, 403]:
                return None, f"Upstox Auth Token Expired / Invalid (HTTP {res.status_code})."
        except Exception as e:
            print(f">>> Upstox query notice for {q}: {e}", flush=True)

    deduped = {}
    for it in all_upstox_items:
        iid = it.get("id") or it.get("symbol") or it.get("name")
        if iid and iid not in deduped:
            deduped[iid] = it

    upstox_list = list(deduped.values())
    print(f"      Enriching {len(upstox_list)} unique records via Upstox /ipos/{{id}}...", flush=True)

    for item in upstox_list:
        ipo_id = item.get("id")
        if not ipo_id:
            continue
        try:
            d_res = requests.get(f"{UPSTOX_BASE_URL}/ipos/{ipo_id}", headers=headers, timeout=5)
            if d_res.status_code == 200:
                details = d_res.json().get("data", {})
                if details:
                    item["lot_size"] = details.get("lot_size") or details.get("minimum_quantity") or item.get("lot_size")
                    item["issue_price_min"] = details.get("minimum_price") or item.get("minimum_price")
                    item["issue_price_max"] = details.get("maximum_price") or item.get("maximum_price")
                    item["rhp_url"] = details.get("rhp_url")
                    item["drhp_url"] = details.get("drhp_url")

                    raw_total_size = details.get("total_issue_size") or details.get("issue_size") or details.get("issue_size_in_crores") or item.get("issue_size")
                    clean_size = clean_num_or_none(raw_total_size)
                    issue_size_cr = 0.0

                    if clean_size and clean_size > 0:
                        issue_size_cr = round(float(clean_size), 2) if clean_size < 100000 else round(float(clean_size) / 10000000.0, 2)
                    item["issue_size_cr"] = issue_size_cr
                    item["listing_price"] = details.get("listing_price")

                    timeline = details.get("timeline") or {}
                    item["allotment_date"] = timeline.get("allotment_start_date") or timeline.get("allotment_date")
                    item["listing_date"] = timeline.get("listing_date")

                    raw_status_str = str(details.get("allotment_status") or timeline.get("allotment_status") or "").upper()
                    item["is_allotment_done"] = bool(
                        details.get("is_allotment_done") is True or
                        timeline.get("is_allotment_done") is True or
                        any(w in raw_status_str for w in ["ALLOTTED", "FINALIZED", "COMPLETED", "DECLARED", "SUCCESS"])
                    )

                    reg_info = details.get("registrar_info") or {}
                    item["registrar_name"] = reg_info.get("name")
                    item["registrar_url"] = reg_info.get("website")

                    # Subscription Parsing
                    item["total_subscription"] = clean_num_or_none(details.get("total_subscription")) or 0.0
                    item["subscription_retail"] = 0.0
                    item["subscription_hni"] = 0.0
                    item["subscription_qib"] = 0.0

            time.sleep(0.05)
        except Exception as e:
            print(f"Error enriching item {ipo_id}: {e}", flush=True)

    return upstox_list, "OK"

# ----------------- MAIN PIPELINE ORCHESTRATOR -----------------
def run_pipeline():
    print(">>> [Phase 1/3] Initializing pipeline session...", flush=True)

    previous_cache = {}
    last_known_good = []
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                last_known_good = json.load(f)
                for item in last_known_good:
                    k = clean_company_name(item.get("name", ""))
                    if k:
                        previous_cache[k] = item
        except Exception:
            pass

    existing_gmp = load_existing_gmp()

    print(">>> [Phase 2/3] Ingesting IPO pipeline data...", flush=True)
    final_dataset = []
    active_source = "None"
    upstox_data, status_msg = try_fetch_upstox()

    today_ist_str = datetime.now(IST).strftime("%Y-%m-%d")
    today_label = datetime.now(IST).strftime("%d %b")
    now_hour = datetime.now(IST).hour

    if upstox_data:
        active_source = "UPSTOX_OFFICIAL"
        seen_keys = set()
        idx_counter = 1

        for item in upstox_data:
            raw_name = item.get("name", "").strip()
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited)\b", "", raw_name, flags=re.IGNORECASE).strip()
            norm_key = clean_company_name(clean_name)
            seen_keys.add(norm_key)

            price_min = float(item.get("issue_price_min") or item.get("minimum_price") or 0.0)
            price_max = float(item.get("issue_price_max") or item.get("maximum_price") or 0.0)
            lot_size = int(clean_num_or_none(item.get("lot_size")) or 0)

            open_d = parse_date_or_none(item.get("bidding_start_date") or item.get("open_date"))
            close_d = parse_date_or_none(item.get("bidding_end_date") or item.get("close_date"))
            allot_d = parse_date_or_none(item.get("allotment_date")) or "To Be Updated"
            list_d = parse_date_or_none(item.get("listing_date")) or "To Be Updated"
            raw_status = (item.get("status") or "UPCOMING").upper()

            # Merge previous cached attributes (logos, issue structure, BRLMs)
            prev = previous_cache.get(norm_key, {})
            status = determine_status(open_d, close_d, list_d if list_d != "To Be Updated" else None, raw_status=raw_status)

            cand = {
                "id": str(idx_counter),
                "name": clean_name,
                "symbol": str(item.get("symbol") or re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()),
                "category": "SME" if str(item.get("issue_type", "")).lower() == "sme" else "Mainboard",
                "status": status,
                "logoUrl": prev.get("logoUrl"),
                "issueSizeCr": float(item.get("issue_size_cr") or prev.get("issueSizeCr", 0.0)),
                "issuePriceMin": price_min or float(prev.get("issuePriceMin", 0.0)),
                "issuePriceMax": price_max or float(prev.get("issuePriceMax", 0.0)),
                "lotSize": lot_size or int(prev.get("lotSize", 0)),
                "openDate": open_d or prev.get("openDate", "To Be Updated"),
                "closeDate": close_d or prev.get("closeDate", "To Be Updated"),
                "allotmentDate": allot_d if allot_d != "To Be Updated" else prev.get("allotmentDate", "To Be Updated"),
                "listingDate": list_d if list_d != "To Be Updated" else prev.get("listingDate", "To Be Updated"),
                "gmpAmount": 0.0,
                "gmpPercent": 0.0,
                "gmpHistory": prev.get("gmpHistory", []),
                "issueStructure": prev.get("issueStructure"),
                "leadManagers": prev.get("leadManagers", []),
                "anchorPortionCr": prev.get("anchorPortionCr", 0.0),
                "financials": prev.get("financials", []),
                "subscriptionTotal": float(item.get("total_subscription") or prev.get("subscriptionTotal", 0.0)),
                "subscriptionRetail": float(item.get("subscription_retail", 0.0) or prev.get("subscriptionRetail", 0.0)),
                "subscriptionHNI": float(item.get("subscription_hni", 0.0) or prev.get("subscriptionHNI", 0.0)),
                "subscriptionQIB": float(item.get("subscription_qib", 0.0) or prev.get("subscriptionQIB", 0.0)),
                "listingPrice": float(item.get("listing_price") or prev.get("listingPrice", 0.0)),
                "listingGainPercent": float(prev.get("listingGainPercent", 0.0)),
                "registrarName": item.get("registrar_name") or prev.get("registrarName", "To Be Updated"),
                "registrarUrl": item.get("registrar_url") or prev.get("registrarUrl", "https://www.sebi.gov.in"),
                "isAllotmentDone": bool(item.get("is_allotment_done", False) or prev.get("isAllotmentDone", False)),
                "rhpPdfUrl": item.get("rhp_url") or prev.get("rhpPdfUrl", ""),
                "drhpPdfUrl": item.get("drhp_url") or prev.get("drhpPdfUrl", ""),
                "openNotified": bool(prev.get("openNotified", False)),
                "closeNotified": bool(prev.get("closeNotified", False))
            }
            if validate_record(cand):
                final_dataset.append(cand)
                idx_counter += 1

        for old in last_known_good:
            old_k = clean_company_name(old.get("name", ""))
            if old_k not in seen_keys:
                old["id"] = str(idx_counter)
                final_dataset.append(old)
                idx_counter += 1
    else:
        print(f"    Tier 1 Notice: {status_msg}. Running Web Feed Discovery...", flush=True)
        active_source = "LIVE_WEB_FALLBACK"
        discovered_ipos = scrape_live_ipo_feed()

        # Merge with existing cache
        merged_dict = {clean_company_name(item.get("name", "")): item for item in last_known_good}
        idx = len(last_known_good) + 1

        for disc in discovered_ipos:
            k = clean_company_name(disc["name"])
            if k not in merged_dict:
                merged_dict[k] = {
                    "id": str(idx),
                    "name": disc["name"],
                    "symbol": re.sub(r"[^A-Za-z0-9]", "", disc["name"])[:7].upper(),
                    "category": "SME" if disc["is_sme"] else "Mainboard",
                    "status": "OPEN",
                    "logoUrl": None,
                    "issueSizeCr": 0.0,
                    "issuePriceMin": 0.0,
                    "issuePriceMax": 0.0,
                    "lotSize": 0,
                    "openDate": today_ist_str,
                    "closeDate": "To Be Updated",
                    "allotmentDate": "To Be Updated",
                    "listingDate": "To Be Updated",
                    "gmpAmount": 0.0,
                    "gmpPercent": 0.0,
                    "gmpHistory": [],
                    "issueStructure": None,
                    "leadManagers": [],
                    "anchorPortionCr": 0.0,
                    "financials": [],
                    "subscriptionTotal": 0.0,
                    "subscriptionRetail": 0.0,
                    "subscriptionHNI": 0.0,
                    "subscriptionQIB": 0.0,
                    "listingPrice": 0.0,
                    "listingGainPercent": 0.0,
                    "registrarName": "To Be Updated",
                    "registrarUrl": "https://www.sebi.gov.in",
                    "isAllotmentDone": False,
                    "rhpPdfUrl": "",
                    "drhpPdfUrl": "",
                    "openNotified": False,
                    "closeNotified": False
                }
                idx += 1

        final_dataset = list(merged_dict.values())

    # Synchronize live GMP map across entire dataset
    synced_gmp_map = sync_and_save_gmp(final_dataset, existing_gmp)

    # ----------------- ATTACH GMP VIA FUZZY TOKEN MATCHING -----------------
    updated_gmp_count = 0
    for item in final_dataset:
        k = clean_company_name(item.get("name", ""))
        price_max = float(item.get("issuePriceMax", 0.0))

        if item.get("status") != "LISTED":
            gmp_val = 0.0

            # 1. Exact match
            if k in synced_gmp_map:
                gmp_val = float(synced_gmp_map[k].get("gmp", 0.0))
            else:
                # 2. Token overlap fallback (handles titles like Core4 Engineers vs Core4)
                item_tokens = get_tokens(item.get("name", ""))
                for g_k, g_v in synced_gmp_map.items():
                    target_tokens = get_tokens(g_v.get("name", ""))
                    if item_tokens and target_tokens and (item_tokens.issubset(target_tokens) or target_tokens.issubset(item_tokens)):
                        gmp_val = float(g_v.get("gmp", 0.0))
                        break

            if gmp_val > 0.0:
                item["gmpAmount"] = gmp_val
                item["gmpPercent"] = round((gmp_val / price_max * 100.0), 2) if price_max > 0.0 else 0.0
                updated_gmp_count += 1

                # Append daily historical tracking
                history = item.get("gmpHistory", [])
                if not history or history[-1].get("dateLabel") != today_label:
                    history.append({"dateLabel": today_label, "amount": gmp_val})
                    item["gmpHistory"] = history[-7:]
                else:
                    history[-1]["amount"] = gmp_val
        else:
            prev_entry = previous_cache.get(k, {})
            if prev_entry.get("gmpAmount", 0.0) > 0:
                item["gmpAmount"] = prev_entry.get("gmpAmount", 0.0)
                item["gmpPercent"] = prev_entry.get("gmpPercent", 0.0)

    print(f">>> [Phase 3/3] Saving feed to disk. Applied live GMP to {updated_gmp_count} non-listed IPOs...", flush=True)
    if not final_dataset:
        print("CRITICAL: Ingestion failed. Preserving existing ipos.json.", flush=True)
        return

    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)

    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("# IPO Pipeline Data Health Audit\n\n")
        f.write(f"**Last Sync (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"**Primary Active Source:** {active_source}\n")
        f.write(f"**Total Records Ingested:** {len(final_dataset)}\n")
        f.write(f"**Live GMP Records Attached:** {updated_gmp_count}\n")

    print(f">>> Pipeline completed successfully via [{active_source}]. Processed {len(final_dataset)} records.", flush=True)

if __name__ == "__main__":
    run_pipeline()
