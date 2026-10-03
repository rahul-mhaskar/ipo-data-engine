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

# Dynamic registrar registry with live direct allotment portals
REGISTRAR_REGISTRY = [
    ("maashitla", "Maashitla Securities", "https://maashitla.com/allotment-status"),
    ("cameo", "Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    ("bigshare", "Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"),
    ("link intime", "Link Intime India", "https://linkintime.co.in/initial_offer/public-issues.html"),
    ("kfin", "KFin Technologies", "https://kosmic.kfintech.com/ipostatus/"),
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

def scrape_ipowatch_subscriptions():
    """Scrapes IPOWatch live subscription table for QIB, NII, Retail multiples and categories."""
    sub_map = {}
    url = "https://ipowatch.in/ipo-subscription-status-today/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            return sub_map

        soup = BeautifulSoup(res.text, "html.parser")
        tables = soup.find_all("table")
        
        for table in tables:
            header_row = table.find("tr")
            if not header_row:
                continue
            headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]
            
            # Find column positions
            qib_idx, nii_idx, retail_idx, total_idx, type_idx = -1, -1, -1, -1, -1
            for i, h in enumerate(headers):
                if "qib" in h: qib_idx = i
                elif "nii" in h or "hni" in h: nii_idx = i
                elif "retail" in h or "rii" in h: retail_idx = i
                elif "total" in h: total_idx = i
                elif "type" in h or "category" in h: type_idx = i

            if retail_idx != -1 or nii_idx != -1:
                for row in table.find_all("tr")[1:]:
                    tds = [td.text.strip() for td in row.find_all("td")]
                    if len(tds) < 3:
                        continue

                    name_raw = tds[0]
                    clean_key = re.sub(r"[^a-zA-Z0-9]", "", name_raw).lower()
                    
                    category = "SME" if (type_idx != -1 and "sme" in tds[type_idx].lower()) or "sme" in name_raw.lower() else "MAINBOARD"
                    qib_val = clean_num(tds[qib_idx]) if qib_idx != -1 and qib_idx < len(tds) else 0.0
                    nii_val = clean_num(tds[nii_idx]) if nii_idx != -1 and nii_idx < len(tds) else 0.0
                    retail_val = clean_num(tds[retail_idx]) if retail_idx != -1 and retail_idx < len(tds) else 0.0
                    total_val = clean_num(tds[total_idx]) if total_idx != -1 and total_idx < len(tds) else 0.0

                    sub_map[clean_key] = {
                        "category": category,
                        "qib": qib_val,
                        "nii": nii_val,
                        "retail": retail_val,
                        "total": total_val
                    }
    except Exception as e:
        print(f"IPOWatch subscription scrape error: {e}")

    return sub_map

def detect_registrar(clean_name, category):
    """Detects and assigns the correct registrar based on corporate registrar affiliations."""
    c_lower = clean_name.lower()
    
    # Specific known registrar patterns
    if any(k in c_lower for k in ["tna solutions", "maashitla"]):
        return ("Maashitla Securities", "https://maashitla.com/allotment-status")
    elif any(k in c_lower for k in ["r.k.fashion", "cameo"]):
        return ("Cameo Corporate Services", "https://ipo.cameoindia.com/")
    elif any(k in c_lower for k in ["bajaj", "tata", "swiggy", "waaree", "premier", "hyundai", "srit"]):
        return ("Link Intime India", "https://linkintime.co.in/initial_offer/public-issues.html")
    elif category == "SME":
        return ("Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html")
    else:
        return ("KFin Technologies", "https://kosmic.kfintech.com/ipostatus/")

def run_scraper():
    ipos = []
    
    # 1. Fetch live subscription splits from IPOWatch
    subscription_data = scrape_ipowatch_subscriptions()

    # 2. Fetch GMP, dates, and lot sizes from InvestorGain live table
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

            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

            if not clean_name:
                continue

            # Normalized lookup for subscription data
            norm_key = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()
            sub_info = subscription_data.get(norm_key, {})

            # Category
            category = sub_info.get("category", "SME" if "SME" in (name_td.text + raw_name).upper() else "MAINBOARD")

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

            # Exact Subscription Breakdown
            feed_total = clean_num(tds[col_map["sub"]].text) if "sub" in col_map else 0.0
            sub_total = sub_info.get("total", feed_total)
            sub_retail = sub_info.get("retail", sub_total)
            sub_nii = sub_info.get("nii", sub_total)
            sub_qib = sub_info.get("qib", 0.0)

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

            # Dynamic Registrar Detection
            reg_name, reg_url = detect_registrar(clean_name, category)

            rhp_url = f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+file+SEBI"
            drhp_url = f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+file+SEBI"

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
                "subscriptionHNI": sub_nii,
                "subscriptionQIB": sub_qib,
                "registrarName": reg_name,
                "registrarUrl": reg_url,
                "rhpPdfUrl": rhp_url,
                "drhpPdfUrl": drhp_url
            })
    except Exception as e:
        print(f"Scraper pipeline error: {e}")

    return ipos

def main():
    items = run_scraper()
    if items and len(items) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"Successfully updated {len(items)} IPO records.")
    else:
        print("0 items fetched; cache retained.")

if __name__ == "__main__":
    main()
    
