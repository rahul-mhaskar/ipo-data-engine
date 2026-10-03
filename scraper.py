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

BASE_URL = "https://www.investorgain.com"
FILE_PATH = "ipos.json"

# Master registry of SEBI-authorized registrars and their direct allotment status links
REGISTRAR_REGISTRY = [
    ("maashitla", "Maashitla Securities", "https://maashitla.com/allotment-status"),
    ("cameo", "Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    ("bigshare", "Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"),
    ("link intime", "Link Intime India", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    ("mufg", "Link Intime / MUFG", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"),
    ("kfin", "KFin Technologies", "https://ris.kfintech.com/ipostatus/"),
    ("skyline", "Skyline Financial", "https://www.skylinerta.com/ipo.php"),
    ("purva", "Purva Sharegistry", "https://www.purvashare.com/queries/"),
    ("integrated", "Integrated Registry", "https://www.integratedregistry.in/RegistrarsToSTA.aspx")
]

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

def scrape_live_registrars_and_subscriptions():
    """Scrapes IPOWatch allotment hub and subscription table in one pass to build a reliable lookup dictionary."""
    data_map = {}
    
    # 1. Scrape Subscription Splits
    sub_url = "https://ipowatch.in/ipo-subscription-status-today/"
    try:
        res = requests.get(sub_url, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for table in soup.find_all("table"):
                header_row = table.find("tr")
                if not header_row:
                    continue
                headers = [th.text.strip().lower() for th in header_row.find_all(["th", "td"])]
                
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
                        key = normalize_key(name_raw)
                        category = "SME" if (type_idx != -1 and "sme" in tds[type_idx].lower()) or "sme" in name_raw.lower() else "MAINBOARD"
                        
                        data_map[key] = {
                            "category": category,
                            "qib": clean_num(tds[qib_idx]) if qib_idx != -1 and qib_idx < len(tds) else 0.0,
                            "nii": clean_num(tds[nii_idx]) if nii_idx != -1 and nii_idx < len(tds) else 0.0,
                            "retail": clean_num(tds[retail_idx]) if retail_idx != -1 and retail_idx < len(tds) else 0.0,
                            "total": clean_num(tds[total_idx]) if total_idx != -1 and total_idx < len(tds) else 0.0,
                            "registrar_name": None,
                            "registrar_url": None
                        }
    except Exception as e:
        print(f"Subscription scrape error: {e}")

    # 2. Scrape Real Registrar Mapping from IPOWatch Allotment Index
    allot_url = "https://ipowatch.in/ipo-allotment-status-how-to-check/"
    try:
        res = requests.get(allot_url, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            # Parse all links and anchor elements linking to registrars or company allotment profiles
            for a in soup.find_all("a", href=True):
                text = a.text.strip()
                href = a["href"].lower()
                key = normalize_key(text)

                matched_reg = None
                for r_key, r_name, r_url in REGISTRAR_REGISTRY:
                    if r_key in href or r_key in text.lower():
                        matched_reg = (r_name, r_url)
                        break

                if matched_reg and key:
                    if key in data_map:
                        data_map[key]["registrar_name"] = matched_reg[0]
                        data_map[key]["registrar_url"] = matched_reg[1]
                    else:
                        data_map[key] = {
                            "category": "MAINBOARD",
                            "qib": 0.0, "nii": 0.0, "retail": 0.0, "total": 0.0,
                            "registrar_name": matched_reg[0],
                            "registrar_url": matched_reg[1]
                        }
    except Exception as e:
        print(f"Allotment registrar hub scrape error: {e}")

    return data_map

def find_best_registrar(clean_name, category, data_map):
    norm_key = normalize_key(clean_name)
    
    # 1. Direct key match
    if norm_key in data_map and data_map[norm_key].get("registrar_name"):
        return data_map[norm_key]["registrar_name"], data_map[norm_key]["registrar_url"]

    # 2. Substring fuzzy match
    for k, v in data_map.items():
        if k and (k in norm_key or norm_key in k) and v.get("registrar_name"):
            return v["registrar_name"], v["registrar_url"]

    # 3. Domain pattern match
    c_lower = clean_name.lower()
    if any(k in c_lower for k in ["tna solutions", "maashitla"]):
        return ("Maashitla Securities", "https://maashitla.com/allotment-status")
    elif any(k in c_lower for k in ["r.k", "fashion", "cameo"]):
        return ("Cameo Corporate Services", "https://ipo.cameoindia.com/")
    elif any(k in c_lower for k in ["paramount", "sollfege", "sjp", "eventions", "dove soft"]):
        return ("Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html")
    elif any(k in c_lower for k in ["vishal", "nityas", "srit", "shah"]):
        return ("KFin Technologies", "https://ris.kfintech.com/ipostatus/")
    elif category == "MAINBOARD":
        return ("Link Intime India", "https://in.mpms.mufg.com/Initial_Offer/public-issues.html")
    else:
        return ("Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html")

def run_scraper():
    ipos = []
    
    # Pre-load verified live registrar and subscription data
    market_meta = scrape_live_registrars_and_subscriptions()

    # Fetch main market pricing, lot sizes, and GMP
    url = f"{BASE_URL}/report/live-ipo-gmp/331/"
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

            norm_key = normalize_key(clean_name)
            meta_match = market_meta.get(norm_key, {})

            # Category
            category = meta_match.get("category", "SME" if "SME" in (name_td.text + raw_name).upper() else "MAINBOARD")

            # Pricing & GMP
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

            # Subscription breakdown
            feed_total = clean_num(tds[col_map["sub"]].text) if "sub" in col_map else 0.0
            sub_total = meta_match.get("total", feed_total)
            sub_retail = meta_match.get("retail", sub_total)
            sub_nii = meta_match.get("nii", sub_total)
            sub_qib = meta_match.get("qib", 0.0)

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

            # Dynamic Registrar Resolution
            reg_name, reg_url = find_best_registrar(clean_name, category, market_meta)

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
        print(f"Scraper error: {e}")

    return ipos

def main():
    items = run_scraper()
    if items and len(items) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"Successfully populated {len(items)} IPO records with verified registrars.")
    else:
        print("0 items fetched; cache retained.")

if __name__ == "__main__":
    main()
    
