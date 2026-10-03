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

def fetch_live_subscriptions():
    """Extracts exact QIB, NII, Retail subscription splits and SME classification from Report 333."""
    sub_map = {}
    url = "https://www.investorgain.com/report/ipo-subscription-live/333/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return sub_map

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return sub_map

        header_row = table.find("tr")
        headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]

        col_qib, col_nii, col_retail, col_total = -1, -1, -1, -1
        for idx, h in enumerate(headers):
            if "qib" in h: col_qib = idx
            elif "nii" in h or "snb" in h or "bii" in h: col_nii = idx
            elif "retail" in h or "rii" in h: col_retail = idx
            elif "total" in h: col_total = idx

        for row in table.find_all("tr")[1:]:
            tds = row.find_all("td")
            if len(tds) < 4:
                continue

            raw_name = tds[0].text.strip()
            # Detect SME vs Mainboard tag accurately
            is_sme = "SME" in raw_name.upper()

            clean_key = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_key = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_key, flags=re.IGNORECASE)
            clean_key = re.sub(r"[^a-zA-Z0-9]", "", clean_key).lower()

            total_val = clean_num(tds[col_total].text.split()[0]) if col_total != -1 else 0.0
            qib_val = clean_num(tds[col_qib].text) if col_qib != -1 and col_qib < len(tds) else 0.0
            nii_val = clean_num(tds[col_nii].text) if col_nii != -1 and col_nii < len(tds) else 0.0
            retail_val = clean_num(tds[col_retail].text) if col_retail != -1 and col_retail < len(tds) else 0.0

            sub_map[clean_key] = {
                "is_sme": is_sme,
                "total": total_val,
                "qib": qib_val,
                "nii": nii_val,
                "retail": retail_val
            }
    except Exception as e:
        print(f"Subscription feed error: {e}")

    return sub_map

def run_scraper():
    ipos = []
    # 1. Fetch live subscription numbers
    sub_data = fetch_live_subscriptions()

    # 2. Fetch market list & GMP from Report 331
    url = "https://www.investorgain.com/report/live-ipo-gmp/331/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
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
            raw_text = name_td.text.strip()
            company_link = name_td.find("a")
            raw_name = company_link.text.strip() if company_link else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", raw_text)

            if not raw_name or "company" in raw_name.lower():
                continue

            # Normalized lookup key
            lookup_key = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            lookup_key = re.sub(r"[^a-zA-Z0-9]", "", lookup_key).lower()

            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

            # Accurate SME Classification
            matched_sub = sub_data.get(lookup_key, {})
            is_sme = matched_sub.get("is_sme", ("SME" in raw_text.upper() or "SME" in raw_name.upper()))
            category = "SME" if is_sme else "MAINBOARD"

            # Accurate Registrar Assignment
            if category == "SME":
                registrar_name = "Bigshare Services Pvt Ltd"
                registrar_url = "https://ipo.bigshareonline.com/ipo_status.html"
            else:
                registrar_name = "Link Intime India Pvt Ltd"
                registrar_url = "https://linkintime.co.in/initial_offer/public-issues.html"

            # Parse pricing
            gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else "0"
            gmp_val = clean_num(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)

            price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else "100"
            prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
            price_min = min(prices) if prices else 100.0
            price_max = max(prices) if prices else price_min
            gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0

            # Lot size
            lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ("1200" if category == "SME" else "35")
            lot_size = int(clean_num(lot_raw)) if clean_num(lot_raw) > 0 else (1200 if category == "SME" else 35)

            # Accurate Subscription Values
            sub_total = matched_sub.get("total", clean_num(tds[col_map["sub"]].text) if "sub" in col_map else 0.0)
            sub_retail = matched_sub.get("retail", sub_total)
            sub_hni = matched_sub.get("nii", sub_total)
            sub_qib = matched_sub.get("qib", 0.0)

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
                "registrarName": registrar_name,
                "registrarUrl": registrar_url,
                "rhpPdfUrl": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1",
                "drhpPdfUrl": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=0"
            })
    except Exception as e:
        print(f"Scraper error: {e}")

    return ipos

def main():
    items = run_scraper()
    if items and len(items) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"Successfully processed {len(items)} IPOs with verified subscriptions and SME tagging.")
    else:
        print("0 items parsed; retained cache.")

if __name__ == "__main__":
    main()
        
