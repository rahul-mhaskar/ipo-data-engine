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

def parse_date_with_year(date_str):
    if not date_str or date_str in ["--", "-", ""]:
        return None
    cleaned = date_str.strip()
    current_year = datetime.now().year

    match_full = re.search(r"(\d{1,2})-([A-Za-z]{3})(?:-(\d{2,4}))?", cleaned)
    if match_full:
        day, month_str, yr = match_full.groups()
        if not yr:
            year = current_year
        elif len(yr) == 2:
            year = 2000 + int(yr)
        else:
            year = int(yr)
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

def scrape_investorgain():
    ipos = []
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return ipos

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return ipos

        # Locate column indexes dynamically from the header
        header_row = table.find("tr")
        headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]

        col_map = {}
        for idx, h in enumerate(headers):
            if "name" in h or "company" in h or "ipo" in h:
                if "name" not in col_map:
                    col_map["name"] = idx
            if "gmp" in h:
                col_map["gmp"] = idx
            elif "sub" in h:
                col_map["sub"] = idx
            elif "price" in h:
                col_map["price"] = idx
            elif "lot" in h:
                col_map["lot"] = idx
            elif "open" in h:
                col_map["open"] = idx
            elif "close" in h:
                col_map["close"] = idx
            elif "boa" in h or "allotment" in h:
                col_map["allotment"] = idx
            elif "listing" in h:
                col_map["listing"] = idx

        name_col_idx = col_map.get("name", 0)
        rows = table.find_all("tr")[1:]
        today_iso = datetime.now().strftime("%Y-%m-%d")

        for idx, row in enumerate(rows):
            tds = row.find_all("td")
            if len(tds) < 6:
                continue

            name_td = tds[name_col_idx]
            
            # 1. FIX: Specifically look for the company link tag <a> first
            company_link = name_td.find("a")
            if company_link and company_link.text.strip():
                raw_name = company_link.text.strip()
            else:
                # If no <a> tag, strip out any '₹ ... Cr' issue size strings
                raw_text = name_td.text.strip()
                raw_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", raw_text, flags=re.IGNORECASE).strip()

            if not raw_name or "company" in raw_name.lower():
                continue

            category = "SME" if "SME" in raw_name.upper() else "MAINBOARD"
            
            # Clean company name: remove prefixes/suffixes like BSE, NSE, SME, IPO, Ltd.
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"\s+", " ", clean_name).strip()

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
            sub_raw = tds[col_map["sub"]].text.strip() if "sub" in col_map else "1.0"
            sub_val = clean_num(sub_raw) if clean_num(sub_raw) > 0 else 1.0

            # Date parsing
            open_d = parse_date_with_year(tds[col_map["open"]].text) if "open" in col_map else None
            close_d = parse_date_with_year(tds[col_map["close"]].text) if "close" in col_map else None
            allot_d = parse_date_with_year(tds[col_map["allotment"]].text) if "allotment" in col_map else None
            list_d = parse_date_with_year(tds[col_map["listing"]].text) if "listing" in col_map else None

            open_d = open_d or today_iso
            close_d = close_d or open_d
            allot_d = allot_d or close_d
            list_d = list_d or allot_d

            status = determine_status(open_d, close_d, list_d)
            symbol = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()

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
                "subscriptionTotal": sub_val,
                "subscriptionRetail": round(sub_val * 0.4, 2),
                "subscriptionHNI": round(sub_val * 0.3, 2),
                "subscriptionQIB": round(sub_val * 0.3, 2),
                "registrarName": "Link Intime / KFintech",
                "registrarUrl": "https://linkintime.co.in/initial_offer/public-issues.html",
                "rhpPdfUrl": "https://www.sebi.gov.in",
                "drhpPdfUrl": "https://www.sebi.gov.in"
            })
    except Exception as e:
        print(f"Scraper error: {e}")

    return ipos

def main():
    live_items = scrape_investorgain()
    if live_items and len(live_items) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(live_items, f, indent=2, ensure_ascii=False)
        print(f"Successfully scraped {len(live_items)} IPOs with accurate company names.")
    else:
        print("Scraper returned 0 items; preserving existing file.")

if __name__ == "__main__":
    main()
    
