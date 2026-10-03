import json
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

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

def load_cached_data():
    if os.path.exists(FILE_PATH):
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def scrape_market_feed():
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

        rows = table.find_all("tr")[1:]
        today_str = datetime.now().strftime("%Y-%m-%d")

        for idx, row in enumerate(rows):
            cols = [c.text.strip() for c in row.find_all("td")]
            if len(cols) < 5:
                continue

            raw_name = cols[0]
            if not raw_name or "Close" in raw_name:
                continue

            category = "SME" if "SME" in raw_name.upper() else "MAINBOARD"
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE)\b", "", raw_name, flags=re.IGNORECASE).strip()

            gmp_val = clean_num(cols[1])
            price_vals = re.findall(r"\d+", cols[2]) if len(cols) > 2 else []
            price_min = float(price_vals[0]) if price_vals else 100.0
            price_max = float(price_vals[-1]) if price_vals else price_min
            gmp_pct = round((gmp_val / price_max * 100), 2) if price_max > 0 else 0.0
            sub_total = clean_num(cols[4]) if len(cols) > 4 else 1.0

            ipos.append({
                "id": str(idx + 1),
                "name": clean_name,
                "symbol": clean_name[:6].upper().replace(" ", ""),
                "category": category,
                "status": "OPEN",
                "issuePriceMin": price_min,
                "issuePriceMax": price_max,
                "lotSize": 100 if category == "SME" else 30,
                "openDate": today_str,
                "closeDate": today_str,
                "allotmentDate": today_str,
                "listingDate": today_str,
                "gmpAmount": gmp_val,
                "gmpPercent": gmp_pct,
                "subscriptionTotal": sub_total,
                "subscriptionRetail": round(sub_total * 0.4, 2),
                "subscriptionHNI": round(sub_total * 0.3, 2),
                "subscriptionQIB": round(sub_total * 0.3, 2),
                "registrarName": "Link Intime / KFintech",
                "registrarUrl": "https://linkintime.co.in/initial_offer/public-issues.html",
                "rhpPdfUrl": "https://www.sebi.gov.in",
                "drhpPdfUrl": "https://www.sebi.gov.in"
            })
    except Exception as e:
        print(f"Scraper error caught: {e}")

    return ipos

def main():
    cached = load_cached_data()
    fresh_ipos = scrape_market_feed()

    # Use fresh items if obtained; otherwise fallback to existing records
    dataset = fresh_ipos if len(fresh_ipos) > 0 else cached

    # Update lifecycle
    today = datetime.now().date()
    for item in dataset:
        try:
            o_date = datetime.strptime(item.get("openDate", ""), "%Y-%m-%d").date()
            c_date = datetime.strptime(item.get("closeDate", ""), "%Y-%m-%d").date()
            l_date = datetime.strptime(item.get("listingDate", ""), "%Y-%m-%d").date()

            if today < o_date:
                item["status"] = "UPCOMING"
            elif o_date <= today <= c_date:
                item["status"] = "OPEN"
            elif c_date < today < l_date:
                item["status"] = "CLOSED"
            else:
                item["status"] = "LISTED"
        except Exception:
            continue

    if dataset:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(dataset, f, indent=2, ensure_ascii=False)
        print(f"Pipeline executed successfully: {len(dataset)} items available.")

if __name__ == "__main__":
    main()
    
