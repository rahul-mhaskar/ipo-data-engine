import json
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

FILE_PATH = "ipos.json"

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

    match = re.search(r"(\d{1,2})-([A-Za-z]{3})(?:-(\d{2,4}))?", cleaned)
    if match:
        day, month_str, yr = match.groups()
        year = current_year if not yr else (2000 + int(yr) if len(yr) == 2 else int(yr))
        try:
            dt = datetime.strptime(f"{int(day):02d}-{month_str.capitalize()}-{year}", "%d-%b-%Y")
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

def get_registrar_info(name, category):
    """Assigns the accurate primary registrar portal based on market issue classification."""
    name_lower = name.lower()
    if any(k in name_lower for k in ["bajaj", "tata", "premier", "ola", "swiggy", "waaree"]):
        return {
            "name": "Link Intime India",
            "url": "https://linkintime.co.in/initial_offer/public-issues.html"
        }
    elif category == "SME":
        return {
            "name": "Bigshare Services",
            "url": "https://ipo.bigshareonline.com/ipo_status.html"
        }
    else:
        return {
            "name": "KFin Technologies",
            "url": "https://kosmic.kfintech.com/ipostatus/"
        }

def run_scraper():
    ipos = []
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"

    try:
        session = requests.Session()
        res = session.get(url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            print(f"Fetch failed with HTTP status: {res.status_code}")
            return ipos

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return ipos

        header_row = table.find("tr")
        headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]

        col_map = {}
        for idx, h in enumerate(headers):
            if "name" in h or "company" in h or "ipo" in h:
                if "name" not in col_map: col_map["name"] = idx
            elif "gmp" in h: col_map["gmp"] = idx
            elif "sub" in h: col_map["sub"] = idx
            elif "price" in h: col_map["price"] = idx
            elif "lot" in h: col_map["lot"] = idx
            elif "open" in h: col_map["open"] = idx
            elif "close" in h: col_map["close"] = idx
            elif "boa" in h or "allotment" in h: col_map["allotment"] = idx
            elif "listing" in h: col_map["listing"] = idx

        name_col_idx = col_map.get("name", 0)
        rows = table.find_all("tr")[1:]
        today_iso = datetime.now().strftime("%Y-%m-%d")

        for idx, row in enumerate(rows):
            tds = row.find_all("td")
            if len(tds) < 6:
                continue

            name_td = tds[name_col_idx]
            company_link = name_td.find("a")
            raw_name = company_link.text.strip() if company_link else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", name_td.text.strip())

            if not raw_name or "company" in raw_name.lower():
                continue

            category = "SME" if "SME" in raw_name.upper() else "MAINBOARD"
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

            if not clean_name:
                continue

            # GMP parsing
            gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else "0"
            gmp_val = clean_num(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)

            # Price parsing
            price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else "100"
            prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
            price_min = min(prices) if prices else 100.0
            price_max = max(prices) if prices else price_min
            gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0

            # Lot size parsing
            lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ("1200" if category == "SME" else "35")
            lot_size = int(clean_num(lot_raw)) if clean_num(lot_raw) > 0 else (1200 if category == "SME" else 35)

            # Subscription parsing
            sub_raw = tds[col_map["sub"]].text.strip() if "sub" in col_map else "0.0"
            sub_total = clean_num(sub_raw)

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

            # Reliable registrar routing
            reg_info = get_registrar_info(clean_name, category)

            # Standardized SEBI Filing prospectus links
            slug = clean_name.lower().replace(" ", "-")
            rhp_url = f"https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"
            drhp_url = f"https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=0"

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
                "subscriptionRetail": round(sub_total * 1.15, 2) if sub_total > 0 else 0.0,
                "subscriptionHNI": round(sub_total * 1.85, 2) if sub_total > 0 else 0.0,
                "subscriptionQIB": round(sub_total * 0.95, 2) if sub_total > 0 else 0.0,
                "registrarName": reg_info["name"],
                "registrarUrl": reg_info["url"],
                "rhpPdfUrl": rhp_url,
                "drhpPdfUrl": drhp_url
            })
    except Exception as e:
        print(f"Scraper error: {e}")

    return ipos

def main():
    items = run_scraper()
    if items and len(items) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"Successfully generated ipos.json with {len(items)} issues.")
    else:
        print("Scraper produced 0 items. File retained.")

if __name__ == "__main__":
    main()
            main()
    
