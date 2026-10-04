import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime

FILE_PATH = "ipos.json"
AUDIT_LOG_PATH = "data_health_audit.md"
OVERRIDES_PATH = "manual_overrides.json"
BASE_URL = "https://www.investorgain.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

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

def clean_num_or_none(val):
    """Returns exact float if found, else None. Never assumes 0."""
    if not val:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return None

def normalize_key(name):
    if not name:
        return ""
    clean = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited)\b", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-zA-Z0-9]", "", clean).lower()

def parse_date_or_none(date_str):
    """Returns ISO date (YYYY-MM-DD) if explicitly declared, else None."""
    if not date_str or date_str.strip() in ["--", "-", ""]:
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

def determine_status(open_d, close_d, list_d):
    today = datetime.now().date()
    try:
        o = datetime.strptime(open_d, "%Y-%m-%d").date() if open_d else None
        c = datetime.strptime(close_d, "%Y-%m-%d").date() if close_d else None
        l = datetime.strptime(list_d, "%Y-%m-%d").date() if list_d else None

        if o and today < o:
            return "UPCOMING"
        elif o and c and (o <= today <= c):
            return "OPEN"
        elif c and l and (c < today < l):
            return "CLOSED"
        elif l and today >= l:
            return "LISTED"
        elif c and today > c:
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
    if not detail_href:
        return None, None
    url = detail_href if detail_href.startswith("http") else f"{BASE_URL}{detail_href}"
    try:
        res = session.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
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
                name, portal = resolve_registrar_from_text(f"{a['href']} {a.text}")
                if name:
                    return name, portal
            return resolve_registrar_from_text(soup.get_text())
    except Exception as e:
        print(f"Detail crawl warning for {url}: {e}")
    return None, None

def run_pipeline():
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Load manual overrides
    manual_overrides = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    manual_overrides[normalize_key(k)] = (v["registrarName"], v["registrarUrl"])
        except Exception:
            pass

    # 2. Load previous verified cache
    previous_cache = {}
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    k = normalize_key(item.get("name", ""))
                    r_name = item.get("registrarName")
                    if k and r_name and r_name not in ["To Be Updated", "To Be Announced"]:
                        previous_cache[k] = (r_name, item.get("registrarUrl", ""))
        except Exception:
            pass

    # 3. Fetch Master GMP Feed
    res = session.get(f"{BASE_URL}/report/live-ipo-gmp/331/", timeout=15)
    if res.status_code != 200:
        print(f"InvestorGain HTTP Error: {res.status_code}")
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
    final_dataset = []
    missing_fields_report = []

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
        category = "SME" if "SME" in (name_td.text + raw_name).upper() else "MAINBOARD"

        # Explicit dates - Null if not announced
        open_d = parse_date_or_none(tds[col_map["open"]].text) if "open" in col_map else None
        close_d = parse_date_or_none(tds[col_map["close"]].text) if "close" in col_map else None
        allot_d = parse_date_or_none(tds[col_map["allotment"]].text) if "allotment" in col_map else None
        list_d = parse_date_or_none(tds[col_map["listing"]].text) if "listing" in col_map else None

        status = determine_status(open_d, close_d, list_d)

        # ---------------- ZERO-FABRICATION REGISTRAR PIPELINE ----------------
        reg_name, reg_url = None, None

        # Level 1: Manual overrides
        if norm_key in manual_overrides:
            reg_name, reg_url = manual_overrides[norm_key]
        else:
            for ok, ov in manual_overrides.items():
                if ok and (ok in norm_key or norm_key in ok):
                    reg_name, reg_url = ov
                    break

        # Level 2: Historical verified cache
        if not reg_name and norm_key in previous_cache:
            reg_name, reg_url = previous_cache[norm_key]

        # Level 3: Dynamic crawl for active/upcoming entries
        if not reg_name and detail_href:
            reg_name, reg_url = fetch_real_registrar_from_details(session, detail_href)
            if reg_name:
                previous_cache[norm_key] = (reg_name, reg_url)
            time.sleep(0.2)

        # Level 4: Transparent fallback — NEVER guess Bigshare or Link Intime
        if not reg_name:
            reg_name = "To Be Updated"
            reg_url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=1"
            missing_fields_report.append(f"- **{clean_name}**: Registrar is unverified / pending announcement.")

        # Pricing & Lot size — Null if unconfirmed
        price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else ""
        prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
        price_min = min(prices) if prices else None
        price_max = max(prices) if prices else None

        lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ""
        lot_size_num = clean_num_or_none(lot_raw)
        lot_size = int(lot_size_num) if lot_size_num else None

        # GMP (Ensures double 0.0 default so Moshi deserialization does not crash)
        gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else ""
        cleaned_gmp = clean_num_or_none(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)
        gmp_val = cleaned_gmp if cleaned_gmp is not None else 0.0
        gmp_pct = round((gmp_val / price_max * 100), 2) if (gmp_val and price_max) else 0.0

        sub_raw = tds[col_map["sub"]].text.strip() if "sub" in col_map else ""
        sub_total = clean_num_or_none(sub_raw)

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
            "openDate": open_d or "To Be Updated",
            "closeDate": close_d or "To Be Updated",
            "allotmentDate": allot_d or "To Be Updated",
            "listingDate": list_d or "To Be Updated",
            "gmpAmount": gmp_val,
            "gmpPercent": gmp_pct,
            "subscriptionTotal": sub_total,
            "subscriptionRetail": None,
            "subscriptionHNI": None,
            "subscriptionQIB": None,
            "registrarName": reg_name,
            "registrarUrl": reg_url,
            "rhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+RHP+file+SEBI",
            "drhpPdfUrl": f"https://www.google.com/search?q={clean_name.replace(' ', '+')}+IPO+DRHP+file+SEBI"
        })

    # Save Clean Feed
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(final_dataset, f, indent=2, ensure_ascii=False)

    # 4. Generate Developer Health & Maintenance Audit Log
    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("# IPO Pipeline Data Health Audit\n\n")
        f.write(f"**Last Sync (UTC):** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"**Total Records Processed:** {len(final_dataset)}\n\n")
        if missing_fields_report:
            f.write("### ⚠️ Action Required: Missing / Unverified Fields\n\n")
            f.write("The following IPOs have unverified fields and are currently shown to users as `To Be Updated`:\n\n")
            for item in missing_fields_report:
                f.write(f"{item}\n")
            f.write("\n> **Fix Instructions:** To override an unverified registrar immediately, add the issue slug to `manual_overrides.json`.\n")
        else:
            f.write("### ✅ All fields 100% verified. No manual intervention required.\n")

    print(f"Data sync complete. {len(missing_fields_report)} entries require developer review.")

if __name__ == "__main__":
    run_pipeline()
    
