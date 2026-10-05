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
    val_str = str(val).strip().replace(",", "")
    m = re.search(r"[-+]?\d*\.?\d+", val_str)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None

def clean_company_name(name: str) -> str:
    """Strips quotes, embedded tags, and corporate suffixes for robust matching."""
    if not name:
        return ""
    # Normalize unicode apostrophes (curly quotes)
    s = name.replace("’", "").replace("'", "")
    # Remove HTML fragments, badges, and inline annotations
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"GMP:.*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"L@.*", "", s, flags=re.IGNORECASE)
    # Strip common noise and corporate suffixes
    s = re.sub(
        r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Industry|Enterprises|Services|Home)\b",
        "",
        s,
        flags=re.IGNORECASE
    )
    return re.sub(r"[^a-z0-9]", "", s.lower())

def match_gmp_robust(name: str, gmp_data: dict) -> dict:
    if not name or not gmp_data:
        return {}

    clean_target = clean_company_name(name)
    if not clean_target:
        return {}

    # 1. Exact match on cleaned string
    if clean_target in gmp_data:
        return gmp_data[clean_target]

    # 2. Substring & prefix overlap
    for cand_clean, data in gmp_data.items():
        if cand_clean and (cand_clean in clean_target or clean_target in cand_clean):
            return data
        min_len = min(len(cand_clean), len(clean_target))
        if min_len >= 4 and cand_clean[:4] == clean_target[:4]:
            return data

    return {}

def parse_date_or_none(date_str):
    if not date_str or str(date_str).strip() in ["--", "-", "", "N/A", "TBD", "TBA", "null", "None"]:
        return None
    cleaned = str(date_str).strip()
    current_year = datetime.now().year

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

# ----------------- TIER 1: UPSTOX FULL INGESTION -----------------
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
        except Exception as e:
            print(f"      Upstox query notice for {q}: {e}", flush=True)

    if not all_upstox_items:
        try:
            res = requests.get(f"{UPSTOX_BASE_URL}/ipos", headers=headers, timeout=8)
            if res.status_code == 200:
                all_upstox_items = res.json().get("data", [])
        except Exception as e:
            print(f"      Plain /ipos notice: {e}", flush=True)

    deduped = {}
    for it in all_upstox_items:
        iid = it.get("id") or it.get("symbol") or it.get("name")
        if iid and iid not in deduped:
            deduped[iid] = it

    upstox_list = list(deduped.values())
    print(f"      Fetched {len(upstox_list)} unique base items. Enriching via Upstox /ipos/{{id}}...", flush=True)

    # Detailed offer parameters directly from Upstox
    for item in upstox_list:
        ipo_id = item.get("id")
        if not ipo_id:
            continue
        try:
            d_res = requests.get(f"{UPSTOX_BASE_URL}/ipos/{ipo_id}", headers=headers, timeout=6)
            if d_res.status_code == 200:
                details = d_res.json().get("data", {})
                if details:
                    # 1. Lot size & pricing
                    item["lot_size"] = details.get("lot_size") or details.get("minimum_quantity") or item.get("lot_size")
                    item["issue_price_min"] = details.get("minimum_price") or item.get("minimum_price")
                    item["issue_price_max"] = details.get("maximum_price") or item.get("maximum_price")
                    item["rhp_url"] = details.get("rhp_url")
                    item["drhp_url"] = details.get("drhp_url")

                    # 2. Timeline
                    timeline = details.get("timeline") or {}
                    item["allotment_date"] = (
                        timeline.get("allotment_start_date") or 
                        timeline.get("basis_of_allotment_date") or 
                        timeline.get("allotment_date")
                    )
                    item["listing_date"] = timeline.get("listing_date")

                    # 3. Registrar info
                    reg_info = details.get("registrar_info") or {}
                    item["registrar_name"] = reg_info.get("name")
                    item["registrar_url"] = reg_info.get("website")

                    # 4. Total subscription
                    item["total_subscription"] = (
                        clean_num_or_none(details.get("total_subscription")) or 
                        clean_num_or_none(item.get("total_subscription")) or 
                        0.0
                    )

                    print(f"      [OK-UPSTOX] {item.get('name')} | Lot: {item.get('lot_size')} | Sub: {item['total_subscription']}x | Allot: {item.get('allotment_date')}", flush=True)
            else:
                print(f"      [NOTICE] Detail failed for {ipo_id}: HTTP {d_res.status_code}", flush=True)
            time.sleep(0.08)
        except Exception as e:
            print(f"      Detail fetch error for {ipo_id}: {e}", flush=True)

    return upstox_list, "OK"

# ----------------- SECONDARY GMP-ONLY CRAWLER -----------------
def fetch_gmp_only_map(session):
    gmp_map = {}
    report_urls = [
        f"{INVESTORGAIN_BASE_URL}/report/live-ipo-gmp/331/",
        f"{INVESTORGAIN_BASE_URL}/report/live-ipo-gmp/331/sme/"
    ]

    for url in report_urls:
        try:
            print(f"  --> Fetching GMP table from {url}...", flush=True)
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
            name_idx, gmp_idx = 0, -1

            for i, h in enumerate(headers):
                if any(k in h for k in ["company", "name", "ipo"]): name_idx = i
                elif "gmp" in h: gmp_idx = i

            if gmp_idx == -1:
                continue

            for row in table.find_all("tr")[1:]:
                tds = row.find_all("td")
                if len(tds) > max(name_idx, gmp_idx):
                    raw_td_text = tds[name_idx].get_text(separator=" ", strip=True)
                    clean_k = clean_company_name(raw_td_text)
                    if not clean_k:
                        continue

                    gmp_raw = tds[gmp_idx].get_text(strip=True)
                    cleaned_gmp = clean_num_or_none(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw) or 0.0

                    if cleaned_gmp > 0:
                        gmp_map[clean_k] = {
                            "gmpAmount": cleaned_gmp
                        }

        except Exception as e:
            print(f"      GMP scraper notice for {url}: {e}", flush=True)

    print(f"      Parsed {len(gmp_map)} GMP records from InvestorGain.", flush=True)
    return gmp_map

# ----------------- MAIN PIPELINE ORCHESTRATOR -----------------
def run_pipeline():
    print(">>> [Phase 1/4] Initializing pipeline session...", flush=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Load overrides & cache
    print(">>> [Phase 2/4] Loading local overrides & historical cache...", flush=True)
    manual_overrides = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    manual_overrides[clean_company_name(k)] = (v["registrarName"], v["registrarUrl"])
        except Exception:
            pass

    previous_cache = {}
    last_known_good = []
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                last_known_good = json.load(f)
                for item in last_known_good:
                    k = clean_company_name(item.get("name", ""))
                    r_name = item.get("registrarName")
                    if k and r_name and r_name not in ["To Be Updated", "To Be Announced"]:
                        previous_cache[k] = (r_name, item.get("registrarUrl", ""))
        except Exception:
            pass

    # 2. Ingest GMP Only
    print(">>> [Phase 3/4] Ingesting secondary GMP map (InvestorGain)...", flush=True)
    gmp_data = fetch_gmp_only_map(session)

    # 3. Ingest Everything Else from Upstox
    print(">>> [Phase 4/4] Executing primary Upstox ingestion...", flush=True)
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
            norm_key = clean_company_name(clean_name)
            seen_keys.add(norm_key)

            # Pricing directly from Upstox
            price_min = float(item.get("issue_price_min") or item.get("minimum_price") or 0.0)
            price_max = float(item.get("issue_price_max") or item.get("maximum_price") or 0.0)

            # Isolated GMP matching from InvestorGain
            gmp_info = match_gmp_robust(clean_name, gmp_data)
            gmp_val = float(gmp_info.get("gmpAmount", 0.0))
            gmp_pct = round((gmp_val / price_max * 100), 2) if (gmp_val > 0 and price_max > 0) else 0.0

            # Lot size directly from Upstox
            raw_lot = item.get("lot_size") or item.get("minimum_quantity")
            lot_size = int(clean_num_or_none(raw_lot) or 0)

            # Dates directly from Upstox
            open_d = parse_date_or_none(item.get("bidding_start_date") or item.get("open_date"))
            close_d = parse_date_or_none(item.get("bidding_end_date") or item.get("close_date"))
            allot_d = parse_date_or_none(item.get("allotment_date")) or "To Be Updated"
            list_d = parse_date_or_none(item.get("listing_date")) or "To Be Updated"

            raw_status = (item.get("status") or "UPCOMING").upper()
            status = determine_status(open_d, close_d, list_d if list_d != "To Be Updated" else None)
            if status == "UPCOMING" and raw_status in ["OPEN", "CLOSED"]:
                status = raw_status

            # Total subscription directly from Upstox
            total_sub = float(item.get("total_subscription") or 0.0)

            # Registrar mapping directly from Upstox
            reg_name, reg_url = None, None
            if norm_key in manual_overrides:
                reg_name, reg_url = manual_overrides[norm_key]
            elif norm_key in previous_cache:
                reg_name, reg_url = previous_cache[norm_key]
            else:
                raw_reg = item.get("registrar_name")
                if raw_reg:
                    for pat, name, url in SEBI_REGISTRARS:
                        if pat in raw_reg.lower():
                            reg_name, reg_url = name, url
                            break
                    if not reg_name:
                        reg_name = raw_reg
                        reg_url = item.get("registrar_url") or "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"
                else:
                    reg_name = "To Be Updated"
                    reg_url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"

            # Symbol directly from Upstox
            sym = item.get("symbol")
            if not sym or str(sym).strip() in ["None", "null", ""]:
                sym = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()
            if not sym:
                sym = "IPO"

            # RHP / DRHP links directly from Upstox
            rhp_url = item.get("rhp_url") or f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+file+SEBI"
            drhp_url = item.get("drhp_url") or f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+file+SEBI"

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
                "subscriptionRetail": 0.0,
                "subscriptionHNI": 0.0,
                "subscriptionQIB": 0.0,
                "listingPrice": 0.0,
                "listingGainPercent": 0.0,
                "registrarName": reg_name,
                "registrarUrl": reg_url,
                "rhpPdfUrl": rhp_url,
                "drhpPdfUrl": drhp_url
            }
            if validate_record(cand):
                final_dataset.append(cand)
                idx_counter += 1

        # Preserve closed and listed history
        for old in last_known_good:
            old_key = clean_company_name(old.get("name", ""))
            if old_key not in seen_keys and old.get("status") in ["LISTED", "CLOSED"]:
                old["id"] = str(idx_counter)
                final_dataset.append(old)
                idx_counter += 1

    else:
        print(f"    Tier 1 Notice: {status_msg}", flush=True)
        final_dataset = last_known_good
        active_source = "CACHE_FALLBACK"

    if not final_dataset:
        print("CRITICAL: Ingestion failed. Preserving existing ipos.json.", flush=True)
        return

    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)

    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("# IPO Pipeline Data Health Audit\n\n")
        f.write(f"**Last Sync (UTC):** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"**Primary Active Source:** {active_source}\n")
        f.write(f"**Total Records Ingested:** {len(final_dataset)}\n")
        f.write("Status: Fully Powered by Upstox v2 API + Robust Isolated GMP.\n")

    print(f">>> Pipeline executed successfully using [{active_source}]. Processed {len(final_dataset)} records.", flush=True)

if __name__ == "__main__":
    run_pipeline()
    
