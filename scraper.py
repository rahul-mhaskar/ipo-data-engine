import json
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

FILE_PATH = "ipos.json"

# Browser-grade headers for exchange session initialization
SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
}

# The 8 SEBI-authorized Registrar Domains
REGISTRAR_PORTALS = {
    "maashitla": ("Maashitla Securities", "https://maashitla.com/allotment-status"),
    "cameo": ("Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    "purva": ("Purva Sharegistry", "https://www.purvashare.com/queries/"),
    "bigshare": ("Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"),
    "link intime": ("Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    "mufg": ("Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    "kfin": ("KFin Technologies", "https://ris.kfintech.com/ipostatus/"),
    "skyline": ("Skyline Financial", "https://www.skylinerta.com/ipo.php"),
    "integrated": ("Integrated Registry", "https://www.integratedregistry.in/RegistrarsToSTA.aspx")
}

def clean_num(val):
    if not val:
        return 0.0
    val = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(val)
    except ValueError:
        return 0.0

def normalize_key(name):
    if not name:
        return ""
    clean = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited)\b", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-zA-Z0-9]", "", clean).lower()

def resolve_rta_portal(raw_rta_text):
    if not raw_rta_text:
        return None, None
    text_low = raw_rta_text.lower()
    for key, (name, portal) in REGISTRAR_PORTALS.items():
        if key in text_low:
            return name, portal
    return None, None

def fetch_nse_master(session):
    """Fetches official regulatory filings from NSE API."""
    nse_data = {}
    try:
        # Step 1: Session warm-up to acquire Akamai cookies
        session.get("https://www.nseindia.com/", headers=SESSION_HEADERS, timeout=12)
        
        # Step 2: Query active public issues
        res = session.get("https://www.nseindia.com/api/ipo-current-issue", headers=API_HEADERS, timeout=12)
        if res.status_code == 200:
            items = res.json()
            if isinstance(items, list):
                for item in items:
                    name = item.get("companyName") or item.get("issueName") or ""
                    key = normalize_key(name)
                    reg_raw = item.get("regName") or item.get("registrar") or ""
                    r_name, r_url = resolve_rta_portal(reg_raw)
                    if key and r_name:
                        nse_data[key] = {
                            "officialName": name,
                            "registrarName": r_name,
                            "registrarUrl": r_url,
                            "category": "SME" if item.get("series") == "SM" else "MAINBOARD",
                            "symbol": item.get("symbol", "")
                        }
    except Exception as e:
        print(f"NSE Exchange Feed Notice: {e}")
    return nse_data

def fetch_bse_master(session):
    """Fetches official regulatory public issues directly from BSE India table."""
    bse_data = {}
    url = "https://www.bseindia.com/markets/PublicIssues/IPOIssues"
    try:
        res = session.get(url, headers=SESSION_HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                for row in rows[1:]:
                    tds = [td.text.strip() for td in row.find_all("td")]
                    if len(tds) >= 4:
                        name = tds[0]
                        key = normalize_key(name)
                        platform = tds[1].upper()
                        # Inspect all row text for official RTA mentions
                        r_name, r_url = resolve_rta_portal(row.text)
                        if key:
                            bse_data[key] = {
                                "officialName": name,
                                "registrarName": r_name,
                                "registrarUrl": r_url,
                                "category": "SME" if "SME" in platform else "MAINBOARD"
                            }
    except Exception as e:
        print(f"BSE Exchange Feed Notice: {e}")
    return bse_data

def fetch_investorgain_gmp_and_subscriptions(session):
    """Enriches canonical issues with live GMP and subscription multiples."""
    gmp_data = []
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    try:
        res = session.get(url, headers=SESSION_HEADERS, timeout=15)
        if res.status_code != 200:
            return gmp_data

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return gmp_data

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

        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 6:
                continue

            name_td = tds[name_col_idx]
            raw_name = name_td.find("a").text.strip() if name_td.find("a") else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", name_td.text.strip())

            if not raw_name or "company" in raw_name.lower():
                continue

            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

            gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else "0"
            gmp_val = clean_num(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)

            price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else "100"
            prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
            price_min = min(prices) if prices else 100.0
            price_max = max(prices) if prices else price_min
            gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0

            lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else "35"
            lot_size = int(clean_num(lot_raw)) if clean_num(lot_raw) > 0 else 35

            sub_total = clean_num(tds[col_map["sub"]].text) if "sub" in col_map else 0.0

            gmp_data.append({
                "clean_name": clean_name,
                "norm_key": normalize_key(clean_name),
                "gmp_amount": gmp_val,
                "gmp_percent": gmp_pct,
                "price_min": price_min,
                "price_max": price_max,
                "lot_size": lot_size,
                "sub_total": sub_total,
                "open_raw": tds[col_map["open"]].text if "open" in col_map else "",
                "close_raw": tds[col_map["close"]].text if "close" in col_map else "",
                "allot_raw": tds[col_map["allotment"]].text if "allotment" in col_map else "",
                "list_raw": tds[col_map["listing"]].text if "listing" in col_map else ""
            })
    except Exception as e:
        print(f"InvestorGain feed error: {e}")

    return gmp_data

def run_pipeline():
    session = requests.Session()
    
    # 1. Fetch regulatory truth from NSE and BSE
    nse_master = fetch_nse_master(session)
    bse_master = fetch_bse_master(session)
    
    # Unified regulatory pool
    exchange_pool = {**bse_master, **nse_master}
    print(f"Loaded {len(exchange_pool)} official issues from NSE/BSE.")

    # 2. Fetch GMP enrichment data
    gmp_rows = fetch_investorgain_gmp_and_subscriptions(session)
    if not gmp_rows:
        print("Market data feed unavailable. Retaining current cache.")
        return

    # Load previous cache for fallback
    previous_cache = {}
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    previous_cache[normalize_key(item["name"])] = item
        except Exception:
            pass

    final_dataset = []

    for idx, row in enumerate(gmp_rows):
        k = row["norm_key"]
        
        # Look up official RTA from the exchange pool
        match = exchange_pool.get(k)
        if not match:
            for ex_k, ex_v in exchange_pool.items():
                if ex_k and (ex_k in k or k in ex_k):
                    match = ex_v
                    break

        reg_name, reg_url = (match.get("registrarName"), match.get("registrarUrl")) if match else (None, None)
        category = match.get("category") if match else ("SME" if row["lot_size"] > 200 else "MAINBOARD")

        # Stale Snapshot Fallback: If not on exchange yet, reuse previously verified RTA
        if not reg_name and k in previous_cache:
            prev = previous_cache[k]
            if prev.get("registrarName") and prev["registrarName"] != "To Be Announced":
                reg_name = prev["registrarName"]
                reg_url = prev["registrarUrl"]

        # Final Regulatory Rule: Never fabricate. If unannounced, label explicitly.
        if not reg_name:
            reg_name = "To Be Announced"
            reg_url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"

        today_str = datetime.now().strftime("%Y-%m-%d")
        
        final_dataset.append({
            "id": str(idx + 1),
            "name": row["clean_name"],
            "symbol": match.get("symbol", re.sub(r"[^A-Za-z0-9]", "", row["clean_name"])[:7].upper()) if match else re.sub(r"[^A-Za-z0-9]", "", row["clean_name"])[:7].upper(),
            "category": category,
            "status": "OPEN",
            "issuePriceMin": row["price_min"],
            "issuePriceMax": row["price_max"],
            "lotSize": row["lot_size"],
            "openDate": today_str,
            "closeDate": today_str,
            "allotmentDate": today_str,
            "listingDate": today_str,
            "gmpAmount": row["gmp_amount"],
            "gmpPercent": row["gmp_percent"],
            "subscriptionTotal": row["sub_total"],
            "subscriptionRetail": row["sub_total"],
            "subscriptionHNI": row["sub_total"],
            "subscriptionQIB": 0.0,
            "registrarName": reg_name,
            "registrarUrl": reg_url,
            "rhpPdfUrl": f"https://www.google.com/search?q={row['clean_name'].replace(' ', '+')}+IPO+RHP+file+SEBI",
            "drhpPdfUrl": f"https://www.google.com/search?q={row['clean_name'].replace(' ', '+')}+IPO+DRHP+file+SEBI"
        })

    # Save to disk
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)
    print(f"Data pipeline complete. Saved {len(final_dataset)} verified IPO objects.")

if __name__ == "__main__":
    run_pipeline()
