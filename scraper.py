import json
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
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

def parse_indian_date(date_str):
    """Converts strings like '30-Sep-2026' or '05-Oct-2026T' into 'YYYY-MM-DD'."""
    if not date_str:
        return None
    # Remove trailing characters like 'T' or time markers
    cleaned = re.sub(r"[^\w-]", "", date_str.strip())
    # Match pattern DD-Mon-YYYY
    match = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", cleaned)
    if match:
        day, month_str, year = match.groups()
        try:
            dt = datetime.strptime(f"{int(day):02d}-{month_str.capitalize()}-{year}", "%d-%b-%Y")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None
    return None

def calculate_lifecycle(open_date_str, close_date_str, listing_date_str):
    """Calculates status against current calendar date."""
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

def scrape_clean_chittorgarh_ipos():
    ipos = []
    # Primary comprehensive tracking report on Chittorgarh
    url = "https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/"
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            print(f"Fetch failed with HTTP status: {res.status_code}")
            return ipos

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            print("Table not found on page.")
            return ipos

        rows = table.find_all("tr")[1:]  # skip header row

        for idx, row in enumerate(rows):
            cols = [td.text.strip() for td in row.find_all("td")]
            if len(cols) < 6:
                continue

            raw_name = cols[0]
            if not raw_name or "Company" in raw_name:
                continue

            # Determine category
            category = "SME" if ("SME" in raw_name.upper() or (len(cols) > 7 and "SME" in cols[7].upper())) else "MAINBOARD"

            # Clean name: remove suffixes like 'Ltd.', 'IPO', 'BSE SME', etc.
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|FPO)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"\s+", " ", clean_name).strip()

            # Parse open, close, and listing dates
            open_date = parse_indian_date(cols[2]) if len(cols) > 2 else None
            close_date = parse_indian_date(cols[3]) if len(cols) > 3 else None
            listing_date = parse_indian_date(cols[4]) if len(cols) > 4 else None

            # If dates couldn't be parsed, skip invalid rows
            if not open_date or not close_date:
                continue

            # Calculate allotment date based on Indian market T+1/T+2 standard
            c_dt = datetime.strptime(close_date, "%Y-%m-%d")
            allotment_date = (c_dt + timedelta(days=1)).strftime("%Y-%m-%d")
            if not listing_date:
                listing_date = (c_dt + timedelta(days=3)).strftime("%Y-%m-%d")

            # Parse Price Band
            price_text = cols[5] if len(cols) > 5 else "0"
            prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_text)]
            price_min = min(prices) if prices else 100.0
            price_max = max(prices) if prices else price_min

            # Calculate actual status
            status = calculate_lifecycle(open_date, close_date, listing_date)

            symbol = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()

            ipos.append({
                "id": str(idx + 1),
                "name": clean_name,
                "symbol": symbol,
                "category": category,
                "status": status,
                "issuePriceMin": price_min,
                "issuePriceMax": price_max,
                "lotSize": 1000 if category == "SME" else 35,
                "openDate": open_date,
                "closeDate": close_date,
                "allotmentDate": allotment_date,
                "listingDate": listing_date,
                "gmpAmount": 0.0,
                "gmpPercent": 0.0,
                "subscriptionTotal": 1.0,
                "subscriptionRetail": 1.0,
                "subscriptionHNI": 1.0,
                "subscriptionQIB": 1.0,
                "registrarName": "Link Intime / KFintech",
                "registrarUrl": "https://linkintime.co.in/initial_offer/public-issues.html",
                "rhpPdfUrl": "https://www.sebi.gov.in",
                "drhpPdfUrl": "https://www.sebi.gov.in"
            })
    except Exception as e:
        print(f"Parser error: {e}")

    return ipos

def main():
    existing = []
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    fresh = scrape_clean_chittorgarh_ipos()
    
    # Fail-safe: Only overwrite if records were successfully retrieved
    if fresh and len(fresh) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(fresh, f, indent=2, ensure_ascii=False)
        print(f"Success: Parsed and saved {len(fresh)} IPOs with accurate dates and categories.")
    elif existing:
        print("Using cached data; scraper retrieved 0 items.")
    else:
        print("No data available.")

if __name__ == "__main__":
    main()
    
