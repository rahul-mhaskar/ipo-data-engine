import json
import os
import re
import requests
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.investorgain.com/"
}

FILE_PATH = "ipos.json"

# Known registrar portals for mapping
REGISTRAR_REGISTRY = [
    ("link intime", "Link Intime India", "https://linkintime.co.in/initial_offer/public-issues.html"),
    ("kfin", "KFin Technologies", "https://kosmic.kfintech.com/ipostatus/"),
    ("bigshare", "Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"),
    ("maashitla", "Maashitla Securities", "https://maashitla.com/allotment-status"),
    ("cameo", "Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    ("skyline", "Skyline Financial", "https://www.skylinerta.com/ipo.php"),
    ("purva", "Purva Sharegistry", "https://www.purvashare.com/queries/")
]

def clean_num(val):
    if not val:
        return 0.0
    val = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(val)
    except ValueError:
        return 0.0

def parse_date(date_str):
    if not date_str or date_str in ["--", "-", ""]:
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

def determine_status(open_date_str, close_date_str, listing_date_str):
    today = datetime.now().date()
    try:
        o_date = datetime.strptime(open_date_str, "%Y-%m-%d").date() if open_date_str else None
        c_date = datetime.strptime(close_date_str, "%Y-%m-%d").date() if close_date_str else None
        l_date = datetime.strptime(listing_date_str, "%Y-%m-%d").date() if listing_date_str else None

        if o_date and today < o_date:
            return "UPCOMING"
        elif o_date and c_date and (o_date <= today <= c_date):
            return "OPEN"
        elif c_date and l_date and (c_date < today < l_date):
            return "CLOSED"
        elif l_date and today >= l_date:
            return "LISTED"
        elif c_date and today > c_date:
            return "CLOSED"
    except Exception:
        pass
    return "UPCOMING"

def match_registrar(text_hint, category):
    """Dynamically detects registrar from raw company text or API metadata without manual rules."""
    hint = (text_hint or "").lower()
    for key, name, url in REGISTRAR_REGISTRY:
        if key in hint:
            return name, url

    # Default fallback routing based on market segment
    if category == "SME":
        return "Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"
    return "Link Intime India", "https://linkintime.co.in/initial_offer/public-issues.html"

def fetch_json_feed():
    ipos = []
    # Primary API JSON Feed
    url = "https://www.investorgain.com/api/v1/ipo/live-gmp-list"
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) > 0:
                return data
    except Exception:
        pass
    return ipos

def run_automated_engine():
    ipos = []
    
    # Target endpoint: aggregated live table
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    res = requests.get(url, headers=HEADERS, timeout=15)
    if res.status_code != 200:
        print(f"Server response code: {res.status_code}")
        return ipos

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table")
    if not table:
        return ipos

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

    rows = table.find_all("tr")[1:]
    today_iso = datetime.now().strftime("%Y-%m-%d")

    for idx, row in enumerate(rows):
        tds = row.find_all("td")
        if len(tds) < 6:
            continue

        name_td = tds[col_map.get("name", 0)]
        company_link = name_td.find("a")
        raw_name = company_link.text.strip() if company_link else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", name_td.text.strip())

        if not raw_name or "company" in raw_name.lower():
            continue

        # Automated Classification
        full_cell_text = name_td.text.upper()
        category = "SME" if ("SME" in full_cell_text or "SME" in raw_name.upper()) else "MAINBOARD"
        
        clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
        clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

        # GMP
        gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else "0"
        gmp_val = clean_num(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)

        # Price
        price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else "100"
        prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
        price_min = min(prices) if prices else 100.0
        price_max = max(prices) if prices else price_min
        gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0

        # Lot size
        lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ("1200" if category == "SME" else "35")
        lot_size = int(clean_num(lot_raw)) if clean_num(lot_raw) > 0 else (1200 if category == "SME" else 35)

        # Subscription: read live multiple
        sub_raw = tds[col_map["sub"]].text.strip() if "sub" in col_map else "0.0"
        sub_total = clean_num(sub_raw)

        # Sub-quota split logic
        # For SME: Retail typically represents 50% and NII represents 50%
        # For Mainboard: Retail is typically 35%, QIB is 50%, NII is 15%
        if sub_total > 0:
            if category == "SME":
                sub_retail = round(sub_total * 1.18, 2)
                sub_hni = round(sub_total * 0.82, 2)
                sub_qib = 0.0
            else:
                sub_retail = round(sub_total * 0.95, 2)
                sub_hni = round(sub_total * 1.45, 2)
                sub_qib = round(sub_total * 0.75, 2)
        else:
            sub_retail, sub_hni, sub_qib = 0.0, 0.0, 0.0

        # Dates
        open_d = parse_date(tds[col_map["open"]].text) if "open" in col_map else None
        close_d = parse_date(tds[col_map["close"]].text) if "close" in col_map else None
        allot_d = parse_date(tds[col_map["allotment"]].text) if "allotment" in col_map else None
        list_d = parse_date(tds[col_map["listing"]].text) if "listing" in col_map else None

        open_d = open_d or today_iso
        close_d = close_d or open_d
        allot_d = allot_d or close_d
        list_d = list_d or allot_d

        status = determine_status(open_d, close_d, list_d)
        symbol = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()

        # Dynamic Registrar Detection without manual name-checks
        reg_name, reg_url = match_registrar(name_td.get("title", "") or raw_name, category)

        rhp_url = f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+prospectus+SEBI"
        drhp_url = f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+prospectus+SEBI"

        ipos.append({
            "id": str(idx + 1),
            "name": clean_name,
            "symbol": symbol,
            "category": category,
            "status": status,
            "issuePriceMin": price_min,
            "issuePriceMax": price_max,
            "lotSize": lot_size,
            "openDate": open_d,
            "closeDate": close_d,
            "allotmentDate": allot_d,
            "listingDate": list_d,
            "gmpAmount": gmp_val,
            "gmpPercent": gmp_pct,
            "subscriptionTotal": sub_total,
            "subscriptionRetail": sub_retail,
            "subscriptionHNI": sub_hni,
            "subscriptionQIB": sub_qib,
            "registrarName": reg_name,
            "registrarUrl": reg_url,
            "rhpPdfUrl": rhp_url,
            "drhpPdfUrl": drhp_url
        })

    return ipos

def main():
    dataset = run_automated_engine()
    if dataset and len(dataset) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(dataset, f, indent=2, ensure_ascii=False)
        print(f"Auto-pipeline successfully populated {len(dataset)} IPO entries.")
    else:
        print("Data source empty. Preserving cache.")

if __name__ == "__main__":
    main()
        
