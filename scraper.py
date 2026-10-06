import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

FILE_PATH = "ipos.json"
AUDIT_LOG_PATH = "data_health_audit.md"
OVERRIDES_PATH = "manual_overrides.json"

UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

# Explicit Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json"
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
    if not name:
        return ""
    s = name.replace("’", "").replace("'", "")
    s = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Industry|Enterprises|Services|Home)\b", "", s, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", s.lower())

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

def fetch_live_listing_quote(symbol: str, headers: dict) -> float:
    """Probes Mainboard and SME ticker permutations for opening trade discovery price."""
    if not symbol or symbol == "IPO":
        return 0.0
    
    clean_sym = symbol.strip().upper()
    keys_to_try = [
        f"NSE_EQ|{clean_sym}",
        f"NSE_EQ|{clean_sym}-SM",
        f"NSE_EQ|{clean_sym}-ST",
        f"BSE_EQ|{clean_sym}"
    ]
    
    for instrument_key in keys_to_try:
        try:
            url = f"{UPSTOX_BASE_URL}/market-quote/quotes?instrument_key={instrument_key}"
            res = requests.get(url, headers=headers, timeout=4)
            if res.status_code == 200:
                data = res.json().get("data", {})
                for _, v in data.items():
                    ohlc = v.get("ohlc", {})
                    open_price = float(ohlc.get("open") or 0.0)
                    if open_price > 0.0:
                        return open_price
                    ltp = float(v.get("last_price") or 0.0)
                    if ltp > 0.0:
                        return ltp
        except Exception:
            pass
    return 0.0

def determine_status(open_d, close_d, list_d, raw_status=None, listing_price=0.0):
    """Accurately classifies status according to IST time and confirmed debut trade price."""
    now_ist = datetime.now(IST)
    today = now_ist.date()

    try:
        o = datetime.strptime(open_d, "%Y-%m-%d").date() if open_d and open_d != "To Be Updated" else None
        c = datetime.strptime(close_d, "%Y-%m-%d").date() if close_d and close_d != "To Be Updated" else None
        l = datetime.strptime(list_d, "%Y-%m-%d").date() if list_d and list_d != "To Be Updated" else None

        # 1. Bidding Window
        if o and today < o:
            return "UPCOMING"
        elif o and c and (o <= today <= c):
            return "OPEN"

        # 2. Listing Day Logic (Strictly verified against 10:00 AM IST)
        if l:
            if today < l:
                return "CLOSED"
            elif today == l:
                is_after_10am = (now_ist.hour > 10) or (now_ist.hour == 10 and now_ist.minute >= 0)
                if is_after_10am and listing_price > 0.0:
                    return "LISTED"
                else:
                    return "CLOSED"
            elif today > l:
                return "LISTED"

        # 3. Post-Close Buffer
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

# ----------------- UPSTOX INGESTION (ALL STAGES) -----------------
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
        {"status": "closed"},
        {"status": "listed"}
    ]

    for q in queries:
        try:
            print(f"  --> Calling Upstox with query {q}...", flush=True)
            res = requests.get(f"{UPSTOX_BASE_URL}/ipos", headers=headers, params=q, timeout=6)
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
            res = requests.get(f"{UPSTOX_BASE_URL}/ipos", headers=headers, timeout=6)
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
                    item["listing_price"] = details.get("listing_price")

                    timeline = details.get("timeline") or {}
                    item["allotment_date"] = (
                        timeline.get("allotment_start_date") or 
                        timeline.get("basis_of_allotment_date") or 
                        timeline.get("allotment_date")
                    )
                    item["listing_date"] = timeline.get("listing_date")

                    reg_info = details.get("registrar_info") or {}
                    item["registrar_name"] = reg_info.get("name")
                    item["registrar_url"] = reg_info.get("website")

                    # Aggregate subscription
                    item["total_subscription"] = (
                        clean_num_or_none(details.get("total_subscription")) or 
                        clean_num_or_none(item.get("total_subscription")) or 
                        0.0
                    )

                                        # Ingest Category Breakdown
                    cats = (
                        details.get("categories") or 
                        details.get("distribution") or 
                        details.get("bidding_details") or 
                        details.get("investor_categories") or 
                        details.get("sub_categories") or 
                        []
                    )

                    # Probe dedicated endpoints if missing from root details
                    if not cats:
                        for endpoint_suffix in ["subscriptions", "bids", "details"]:
                            try:
                                sub_res = requests.get(f"{UPSTOX_BASE_URL}/ipos/{ipo_id}/{endpoint_suffix}", headers=headers, timeout=4)
                                if sub_res.status_code == 200:
                                    sdata = sub_res.json().get("data", {})
                                    cats = sdata.get("categories") or sdata.get("distribution") or sdata.get("bidding_details") or []
                                    if cats:
                                        break
                            except Exception:
                                pass

                    sub_retail, sub_hni, sub_qib = 0.0, 0.0, 0.0
                    for c in cats:
                        c_name = str(c.get("category") or c.get("name") or c.get("category_name") or "").upper().strip()
                        c_rate = float(clean_num_or_none(
                            c.get("subscription_rate") or 
                            c.get("rate") or 
                            c.get("times_subscribed") or 
                            c.get("subscription") or
                            c.get("oversubscription")
                        ) or 0.0)

                        if any(k in c_name for k in ["RETAIL", "RII", "INDIVIDUAL"]):
                            sub_retail = c_rate
                        elif any(k in c_name for k in ["NII", "HNI", "NON-INSTITUTIONAL", "NON INSTITUTIONAL"]):
                            sub_hni = c_rate
                        elif "QIB" in c_name:
                            sub_qib = c_rate

                    item["subscription_retail"] = sub_retail
                    item["subscription_hni"] = sub_hni
                    item["subscription_qib"] = sub_qib


            time.sleep(0.05)
        except Exception:
            pass

    return upstox_list, "OK"

# ----------------- MAIN PIPELINE ORCHESTRATOR -----------------
def run_pipeline():
    print(">>> [Phase 1/3] Initializing pipeline session...", flush=True)

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
                    if k:
                        previous_cache[k] = item
        except Exception:
            pass

    print(">>> [Phase 2/3] Executing Upstox data ingestion...", flush=True)
    final_dataset = []
    active_source = "None"
    upstox_data, status_msg = try_fetch_upstox()

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

            raw_lot = item.get("lot_size") or item.get("minimum_quantity")
            lot_size = int(clean_num_or_none(raw_lot) or 0)

            open_d = parse_date_or_none(item.get("bidding_start_date") or item.get("open_date"))
            close_d = parse_date_or_none(item.get("bidding_end_date") or item.get("close_date"))
            allot_d = parse_date_or_none(item.get("allotment_date")) or "To Be Updated"
            list_d = parse_date_or_none(item.get("listing_date")) or "To Be Updated"
            raw_status = (item.get("status") or "UPCOMING").upper()

            total_sub = float(item.get("total_subscription") or 0.0)
            sub_retail = float(item.get("subscription_retail", 0.0))
            sub_hni = float(item.get("subscription_hni", 0.0))
            sub_qib = float(item.get("subscription_qib", 0.0))

            # CACHE PINNING: If Upstox purged listed category breakdowns, preserve from last known good run
            if norm_key in previous_cache:
                prev = previous_cache[norm_key]
                if sub_retail <= 0.0:
                    sub_retail = float(prev.get("subscriptionRetail", 0.0))
                if sub_hni <= 0.0:
                    sub_hni = float(prev.get("subscriptionHNI", 0.0))
                if sub_qib <= 0.0:
                    sub_qib = float(prev.get("subscriptionQIB", 0.0))
                if total_sub <= 0.0:
                    total_sub = float(prev.get("subscriptionTotal", 0.0))


            # 1. Primary listing price from Upstox IPO endpoint
            listing_price = float(clean_num_or_none(item.get("listing_price")) or 0.0)

            # 2. Historical pin: Preserve previously captured debut price
            if listing_price <= 0.0 and norm_key in previous_cache:
                listing_price = float(previous_cache[norm_key].get("listingPrice", 0.0))

            # 3. Live Quote Fallback: Probe live market quotes on or past listing date
            if listing_price <= 0.0 and item.get("symbol"):
                parsed_list_d = parse_date_or_none(item.get("listing_date"))
                today_ist_str = datetime.now(IST).strftime("%Y-%m-%d")
                if parsed_list_d and parsed_list_d <= today_ist_str:
                    listing_price = fetch_live_listing_quote(
                        item.get("symbol"),
                        headers={"Accept": "application/json", "Authorization": f"Bearer {UPSTOX_TOKEN}"}
                    )

            # 4. Calculate Listing Gain % against Cap Price
            listing_gain_pct = 0.0
            if listing_price > 0.0 and price_max > 0.0:
                listing_gain_pct = round(((listing_price - price_max) / price_max) * 100.0, 2)

            # 5. Status determination gated by IST clock and discovered price
            status = determine_status(
                open_d,
                close_d,
                list_d if list_d != "To Be Updated" else None,
                raw_status=raw_status,
                listing_price=listing_price
            )

            # Registrar mapping
            reg_name, reg_url = None, None
            if norm_key in manual_overrides:
                reg_name, reg_url = manual_overrides[norm_key]
            elif norm_key in previous_cache and previous_cache[norm_key].get("registrarName") not in ["To Be Updated", "To Be Announced", None]:
                reg_name = previous_cache[norm_key].get("registrarName")
                reg_url = previous_cache[norm_key].get("registrarUrl")
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

            sym = item.get("symbol")
            if not sym or str(sym).strip() in ["None", "null", ""]:
                sym = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()
            if not sym:
                sym = "IPO"

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
                "gmpAmount": 0.0,
                "gmpPercent": 0.0,
                "subscriptionTotal": total_sub,
                "subscriptionRetail": sub_retail,
                "subscriptionHNI": sub_hni,
                "subscriptionQIB": sub_qib,
                "listingPrice": listing_price,
                "listingGainPercent": listing_gain_pct,
                "registrarName": reg_name,
                "registrarUrl": reg_url,
                "rhpPdfUrl": rhp_url,
                "drhpPdfUrl": drhp_url
            }
            if validate_record(cand):
                final_dataset.append(cand)
                idx_counter += 1

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

    print(">>> [Phase 3/3] Saving feed...", flush=True)
    if not final_dataset:
        print("CRITICAL: Ingestion failed. Preserving existing ipos.json.", flush=True)
        return

    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("# IPO Pipeline Data Health Audit\n\n")
        f.write(f"**Last Sync (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"**Primary Active Source:** {active_source}\n")
        f.write(f"**Total Records Ingested:** {len(final_dataset)}\n")
        f.write("Status: Direct Upstox v2 Ingestion with Category Subscription Distribution.\n")

    print(f">>> Pipeline completed successfully via [{active_source}]. Processed {len(final_dataset)} records.", flush=True)

if __name__ == "__main__":
    run_pipeline()
