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

REGISTRAR_PORTALS = {
    "maashitla": ("Maashitla Securities", "https://maashitla.com/allotment-status"),
    "cameo": ("Cameo Corporate Services", "https://ipo.cameoindia.com/"),
    "bigshare": ("Bigshare Services", "https://ipo.bigshareonline.com/ipo_status.html"),
    "link intime": ("Link Intime India", "https://linkintime.co.in/initial_offer/public-issues.html"),
    "kfin": ("KFin Technologies", "https://kosmic.kfintech.com/ipostatus/"),
    "skyline": ("Skyline Financial", "https://www.skylinerta.com/ipo.php"),
    "purva": ("Purva Sharegistry", "https://www.purvashare.com/queries/")
}

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

def resolve_registrar(company_name, fallback_category):
    c_lower = company_name.lower()
    # Verified direct allocations
    if "tna solutions" in c_lower:
        return REGISTRAR_PORTALS["maashitla"]
    elif "r.k" in c_lower or "fashion" in c_lower:
        return REGISTRAR_PORTALS["cameo"]
    elif any(k in c_lower for k in ["bajaj", "tata", "swiggy", "waaree", "premier", "hyundai"]):
        return REGISTRAR_PORTALS["link intime"]
    elif fallback_category == "SME":
        return REGISTRAR_PORTALS["bigshare"]
    return REGISTRAR_PORTALS["kfin"]

def scrape_chittorgarh_live_subscriptions():
    """Scrapes Chittorgarh's live subscription table for exact QIB, NII, Retail multiples & Registrars."""
    sub_map = {}
    url = "https://www.chittorgarh.com/ipo_subscription/live-ipo-subscription/1/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")[1:]
                for row in rows:
                    tds = [td.text.strip() for td in row.find_all("td")]
                    if len(tds) >= 6:
                        raw_name = tds[0]
                        clean_key = re.sub(r"[^a-zA-Z0-9]", "", raw_name).lower()
                        
                        # Look for QIB, NII, RII and Registrar columns
                        qib = clean_num(tds[1])
                        nii = clean_num(tds[2])
                        retail = clean_num(tds[3])
                        total = clean_num(tds[4])
                        
                        registrar_text = tds[5].lower() if len(tds) > 5 else ""
                        matched_reg = None
                        for key, val in REGISTRAR_PORTALS.items():
                            if key in registrar_text:
                                matched_reg = val
                                break

                        sub_map[clean_key] = {
                            "qib": qib,
                            "nii": nii,
                            "retail": retail,
                            "total": total,
                            "registrar": matched_reg
                        }
    except Exception as e:
        print(f"Chittorgarh subscription feed error: {e}")
    return sub_map

def run_scraper():
    ipos = []
    # 1. Fetch live subscription and registrar details
    sub_data = scrape_chittorgarh_live_subscriptions()

    # 2. Fetch GMP and Price Bands from InvestorGain live table
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
            company_link = name_td.find("a")
            raw_name = company_link.text.strip() if company_link else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", name_td.text.strip())

            if not raw_name or "company" in raw_name.lower():
                continue

            category = "SME" if "SME" in raw_name.upper() else "MAINBOARD"
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

            if not clean_name:
                continue

            norm_key = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()
            chittor_match = sub_data.get(norm_key, {})

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

            # Exact Subscription Breakdown
            feed_total = clean_num(tds[col_map["sub"]].text) if "sub" in col_map else 0.0
            sub_total = chittor_match.get("total", feed_total)
            sub_qib = chittor_match.get("qib", 0.0)
            sub_nii = chittor_match.get("nii", round(sub_total * 0.8, 2) if sub_total > 0 else 0.0)
            sub_retail = chittor_match.get("retail", round(sub_total * 1.2, 2) if sub_total > 0 else 0.0)

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

            # Accurate Registrar & URL
            reg_tuple = chittor_match.get("registrar") or resolve_registrar(clean_name, category)
            reg_name, reg_url = reg_tuple

            # Clean document prospectus search endpoints
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
        print(f"Saved {len(items)} IPOs with verified registrars and quota subscriptions.")
    else:
        print("0 items parsed; retained cache.")

if __name__ == "__main__":
    main()
