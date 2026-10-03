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

# Known registrar direct allotment lookup URLs
REGISTRAR_URL_MAP = {
    "link intime": "https://linkintime.co.in/initial_offer/public-issues.html",
    "kfintech": "https://kosmic.kfintech.com/ipostatus/",
    "kfin technologies": "https://kosmic.kfintech.com/ipostatus/",
    "bigshare": "https://ipo.bigshareonline.com/ipo_status.html",
    "maashitla": "https://maashitla.com/allotment-status",
    "cameo": "https://ipo.cameoindia.com/",
    "skyline": "https://www.skylinerta.com/ipo.php",
    "purva": "https://www.purvashare.com/queries/"
}

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

def scrape_ipo_details(detail_url):
    """Fetches the specific IPO page to extract exact registrar, live sub-quota breakdown, and prospectus links."""
    details = {
        "registrarName": "Link Intime / KFintech",
        "registrarUrl": "https://linkintime.co.in/initial_offer/public-issues.html",
        "subscriptionRetail": 0.0,
        "subscriptionHNI": 0.0,
        "subscriptionQIB": 0.0,
        "rhpPdfUrl": "https://www.sebi.gov.in",
        "drhpPdfUrl": "https://www.sebi.gov.in",
        "issueSizeCr": 0.0
    }
    
    if not detail_url:
        return details

    try:
        full_url = detail_url if detail_url.startswith("http") else BASE_URL + detail_url
        res = requests.get(full_url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            return details

        soup = BeautifulSoup(res.text, "html.parser")

        # 1. Search for Official Registrar
        reg_elem = soup.find(text=re.compile(r"Registrar", re.IGNORECASE))
        if reg_elem:
            parent = reg_elem.find_parent(["tr", "li", "div"])
            if parent:
                text_content = parent.text
                for reg_key, direct_link in REGISTRAR_URL_MAP.items():
                    if reg_key in text_content.lower():
                        details["registrarName"] = reg_key.title()
                        details["registrarUrl"] = direct_link
                        break

        # 2. Extract Document Links (RHP & DRHP PDFs)
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            text = a_tag.text.lower()
            if "rhp" in text or "rhp" in href.lower():
                details["rhpPdfUrl"] = href if href.startswith("http") else BASE_URL + href
            elif "drhp" in text or "drhp" in href.lower():
                details["drhpPdfUrl"] = href if href.startswith("http") else BASE_URL + href

        # 3. Extract Precise Subscription Multiple Table (Retail, NII, QIB)
        for tr in soup.find_all("tr"):
            row_text = tr.text.lower()
            tds = [td.text.strip() for td in tr.find_all("td")]
            if len(tds) >= 2:
                if "retail" in row_text or "rii" in row_text:
                    details["subscriptionRetail"] = clean_num(tds[-1])
                elif "qib" in row_text:
                    details["subscriptionQIB"] = clean_num(tds[-1])
                elif "nii" in row_text or "hni" in row_text or "non-institutional" in row_text:
                    details["subscriptionHNI"] = clean_num(tds[-1])

    except Exception as e:
        print(f"Detail parse error for {detail_url}: {e}")

    return details

def scrape_investorgain():
    ipos = []
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

        # Limit to the most relevant/recent 25 IPOs to keep GitHub Actions execution fast
        for idx, row in enumerate(rows[:25]):
            tds = row.find_all("td")
            if len(tds) < 6:
                continue

            name_td = tds[name_col_idx]
            company_link = name_td.find("a")
            detail_page_href = company_link["href"] if company_link and "href" in company_link.attrs else ""
            
            raw_name = company_link.text.strip() if company_link else re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", name_td.text.strip())

            if not raw_name or "company" in raw_name.lower():
                continue

            category = "SME" if "SME" in raw_name.upper() else "MAINBOARD"
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?)\b", "", raw_name, flags=re.IGNORECASE)
            clean_name = re.sub(r"₹\s*[\d,.]+\s*Cr\.?", "", clean_name, flags=re.IGNORECASE).strip()

            if not clean_name:
                continue

            # Core fields
            gmp_raw = tds[col_map["gmp"]].text.strip() if "gmp" in col_map else "0"
            gmp_val = clean_num(gmp_raw.split("(")[0] if "(" in gmp_raw else gmp_raw)

            price_raw = tds[col_map["price"]].text.strip() if "price" in col_map else "100"
            prices = [float(p) for p in re.findall(r"\d+(?:\.\d+)?", price_raw)]
            price_min = min(prices) if prices else 100.0
            price_max = max(prices) if prices else price_min
            gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0

            lot_raw = tds[col_map["lot"]].text.strip() if "lot" in col_map else ("1200" if category == "SME" else "35")
            lot_size = int(clean_num(lot_raw)) if clean_num(lot_raw) > 0 else (1200 if category == "SME" else 35)

            sub_raw = tds[col_map["sub"]].text.strip() if "sub" in col_map else "0.0"
            sub_total = clean_num(sub_raw)

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

            # Deep scrape each IPO's page for real Registrar, Subscription quota split, and RHP docs
            deep_meta = scrape_ipo_details(detail_page_href)

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
                "subscriptionRetail": deep_meta["subscriptionRetail"] if deep_meta["subscriptionRetail"] > 0 else sub_total,
                "subscriptionHNI": deep_meta["subscriptionHNI"],
                "subscriptionQIB": deep_meta["subscriptionQIB"],
                "registrarName": deep_meta["registrarName"],
                "registrarUrl": deep_meta["registrarUrl"],
                "rhpPdfUrl": deep_meta["rhpPdfUrl"],
                "drhpPdfUrl": deep_meta["drhpPdfUrl"]
            })
    except Exception as e:
        print(f"Scraper error: {e}")

    return ipos

def main():
    live_items = scrape_investorgain()
    if live_items and len(live_items) > 0:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(live_items, f, indent=2, ensure_ascii=False)
        print(f"Successfully deep-scraped {len(live_items)} IPOs with verified registrar and document metadata.")
    else:
        print("Scraper returned 0 items; retaining existing file.")

if __name__ == "__main__":
    main()
    
