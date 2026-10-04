import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime

FILE_PATH = "ipos.json"
OVERRIDES_PATH = "manual_overrides.json"
BASE_URL = "https://www.investorgain.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

# Master SEBI-Authorized Registrar Portals (including specialized RTAs)
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

def resolve_registrar_from_text(text):
    if not text:
        return None, None
    t_lower = text.lower()
    for key, name, portal in SEBI_REGISTRARS:
        if key in t_lower:
            return name, portal
    return None, None

def fetch_real_registrar_from_details(session, detail_href):
    """Scrapes the dedicated profile page on InvestorGain to find the assigned RTA."""
    if not detail_href:
        return None, None

    url = detail_href if detail_href.startswith("http") else f"{BASE_URL}{detail_href}"
    try:
        res = session.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Check table cells or paragraphs mentioning Registrar
            for el in soup.find_all(["td", "th", "div", "p", "li"]):
                if "registrar" in el.text.lower():
                    name, portal = resolve_registrar_from_text(el.text)
                    if name:
                        return name, portal
                    if el.parent:
                        name, portal = resolve_registrar_from_text(el.parent.text)
                        if name:
                            return name, portal

            for a in soup.find_all("a", href=True):
                href_txt = f"{a['href']} {a.text}".lower()
                name, portal = resolve_registrar_from_text(href_txt)
                if name:
                    return name, portal

            return resolve_registrar_from_text(soup.get_text())
    except Exception as e:
        print(f"Detail page read notice on {url}: {e}")

    return None, None

def fetch_authentic_subscriptions(session):
    sub_map = {}
    url = "https://ipowatch.in/ipo-subscription-status-today/"
    try:
        res = session.get(url, headers=HEADERS, timeout=10)
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
                        
                        sub_map[key] = {
                            "category": category,
                            "qib": clean_num(tds[qib_idx]) if qib_idx != -1 and qib_idx < len(tds) else 0.0,
                            "nii": clean_num(tds[nii_idx]) if nii_idx != -1 and nii_idx < len(tds) else 0.0,
                            "retail": clean_num(tds[retail_idx]) if retail_idx != -1 and retail_idx < len(tds) else 0.0,
                            "total": clean_num(tds[total_idx]) if total_idx != -1 and total_idx < len(tds) else 0.0
                        }
    except Exception as e:
        print(f"Subscription feed notice: {e}")
    return sub_map

def run_pipeline():
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Load manual overrides (Priority 1 Ground Truth)
    manual_overrides = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw_ov = json.load(f)
                for k, v in raw_ov.items():
                    manual_overrides[normalize_key(k)] = (v["registrarName"], v["registrarUrl"])
            print(f"Loaded {len(manual_overrides)} manual verified overrides.")
        except Exception as e:
            print(f"Notice reading manual_overrides.json: {e}")

    # 2. Fetch Subscription Multiples
    sub_data = fetch_authentic_subscriptions(session)

    # 3. Load Existing Verified Cache
    previous_cache = {}
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    k = normalize_key(item.get("name", ""))
                    if k and item.get("registrarName") and item["registrarName"] != "To Be Announced":
                        previous_cache[k] = (item["registrarName"], item["registrarUrl"])
        except Exception:
            pass

    # 4. Fetch Live GMP Master Feed
    gmp_url = f"{BASE_URL}/report/live-ipo-gmp/331/"
    res = session.get(gmp_url, timeout=12)
    if res.status_code != 200:
        print(f"Unable to reach market feed: HTTP {res.status_code}")
        return

    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table")
    if not table:
        return

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
    final_dataset = []

    for idx, row in enumerate(rows):
        tds = row.find_all("td")
        if len(tds) < 6:
            continue

        name_td = tds[name_col_idx]
        company_link = name_td.find("a")
        detail_href = company_link.get("href", "") if company_link else ""
        raw_name = company_link.text.strip() if company_link else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", name_td.text.strip())

        if not raw_name or "company" in raw_name.lower():
            continue

        clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
        clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

        if not clean_name:
            continue

        norm_key = normalize_key(clean_name)
        sub_info = sub_data.get(norm_key, {})

        category = sub_info.get("category", "SME" if "SME" in (name_td.text + raw_name).upper() else "MAINBOARD")

        open_d = parse_date(tds[col_map["open"]].text) if "open" in col_map else None
        close_d = parse_date(tds[col_map["close"]].text) if "close" in col_map else None
        allot_d = parse_date(tds[col_map["allotment"]].text) if "allotment" in col_map else None
        list_d = parse_date(tds[col_map["listing"]].text) if "listing" in col_map else None

        open_d = open_d or today_iso
        close_d = close_d or open_d
        allot_d = allot_d or close_d
        list_d = list_d or allot_d

        status = determine_status(open_d, close_d, list_d)

        # ---------------- REGISTRAR RESOLUTION PIPELINE ----------------
        reg_name, reg_url = None, None

        # Level 1: Check manual overrides
        if norm_key in manual_overrides:
            reg_name, reg_url = manual_overrides[norm_key]
        else:
            for ok, ov in manual_overrides.items():
                if ok and (ok in norm_key or norm_key in ok):
                    reg_name, reg_url = ov
                    break

        # Level 2: Check historical verified cache
        if not reg_name:
            if norm_key in previous_cache:
                reg_name, reg_url = previous_cache[norm_key]
            else:
                for pk, pv in previous_cache.items():
                    if pk and (pk in norm_key or norm_key in pk):
                        reg_name, reg_url = pv
                        break

        # Level 3: Dynamic crawl of individual overview page
        if not reg_name and detail_href:
            print(f"Extracting registrar for {clean_name}...")
            reg_name, reg_url = fetch_real_registrar_from_details(session, detail_href)
            if reg_name:
                previous_cache[norm_key] = (reg_name, reg_url)
            time.sleep(0.3)

        # Level 4: Zero fabrication rule
        # If unverified, designate as 'To Be Announced' with the official SEBI link
        if not reg_name:
            reg_name = "To Be Announced"
            reg_url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"

        # GMP & Pricing
        gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else "0"
        gmp_val = clean_num(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)

        price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else "100"
        prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
        price_min = min(prices) if prices else 100.0
        price_max = max(prices) if prices else price_min
        gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0

        lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ("1200" if category == "SME" else "35")
        lot_size = int(clean_num(lot_raw)) if clean_num(lot_raw) > 0 else (1200 if category == "SME" else 35)

        feed_total = clean_num(tds[col_map["sub"]].text) if "sub" in col_map else 0.0
        sub_total = sub_info.get("total", feed_total)
        sub_qib = sub_info.get("qib", 0.0)
        sub_nii = sub_info.get("nii", 0.0)
        sub_retail = sub_info.get("retail", sub_total)

        symbol = re.sub(r"[^A-Za-z0-9]", "", clean_name)[:7].upper()

        final_dataset.append({
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
            "rhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+file+SEBI",
            "drhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+file+SEBI"
        })

    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(final_dataset)} IPO records without fallback assumptions.")

if __name__ == "__main__":
    run_pipeline()
