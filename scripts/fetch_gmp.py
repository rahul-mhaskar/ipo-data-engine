import os
import json
import re
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

IST = timezone(timedelta(hours=5, minutes=30))
IPOS_JSON_PATH = "ipos.json"
GMP_DATA_PATH = "gmp_data.json"
OVERRIDES_PATH = "manual_overrides.json"

def clean_company_name(name: str) -> str:
    if not name:
        return ""
    s = name.replace("’", "").replace("'", "")
    # Strip site-specific status badges and exchange suffixes
    s = re.sub(
        r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Enterprises|Services|Allotted|Closing|Today)\b", 
        "", 
        s, 
        flags=re.IGNORECASE
    )
    # Strip trailing UI status markers like 'IPOC', 'IPOO', 'IPOU', 'SMEC', 'SMEO'
    s = re.sub(r"(ipo[a-z]*|sme[a-z]*)$", "", s.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", s.lower())

def scrape_investorgain_gmp():
    url = "https://www.investorgain.com/report/ipo-gmp-live/331/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    scraped = {}
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            print(f"[GMP Scraper] HTTP {res.status_code} from InvestorGain.")
            return scraped

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            print("[GMP Scraper] Table not found on page.")
            return scraped

        # Locate column indexes dynamically
        headers_row = table.find("tr")
        col_name_idx = 0
        col_gmp_idx = 1

        if headers_row:
            for idx, th in enumerate(headers_row.find_all(["th", "td"])):
                txt = th.get_text(strip=True).upper()
                if any(k in txt for k in ["NAME", "COMPANY", "IPO"]):
                    col_name_idx = idx
                elif "GMP" in txt:
                    col_gmp_idx = idx

        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
            if len(cols) > max(col_name_idx, col_gmp_idx):
                raw_name = cols[col_name_idx]
                raw_gmp = cols[col_gmp_idx]

                # Match patterns like: "₹69 (25.46%)" or "₹15 (5.19%)" or "₹-- (0.00%)"
                amt_match = re.search(r"₹\s*([0-9]+(?:\.[0-9]+)?)", raw_gmp)
                pct_match = re.search(r"\(([+-]?[0-9]+(?:\.[0-9]+)?)\s*%\)", raw_gmp)

                gmp_amt = float(amt_match.group(1)) if amt_match else 0.0
                gmp_pct = float(pct_match.group(1)) if pct_match else 0.0

                norm_key = clean_company_name(raw_name)
                if norm_key:
                    scraped[norm_key] = {
                        "name": raw_name,
                        "gmp": gmp_amt,
                        "percent": gmp_pct
                    }

        print(f"[GMP Scraper] Extracted {len(scraped)} live GMP records.")
    except Exception as e:
        print(f"[GMP Scraper] Error during fetch: {e}")

    return scraped

def sync_gmp():
    scraped_data = scrape_investorgain_gmp()

    # Load manual overrides if present
    manual_overrides = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                manual_overrides = json.load(f)
        except Exception:
            pass

    # Load persistent GMP storage
    gmp_store = {}
    if os.path.exists(GMP_DATA_PATH):
        try:
            with open(GMP_DATA_PATH, "r", encoding="utf-8") as f:
                gmp_store = json.load(f)
        except Exception:
            pass

    today_str = datetime.now(IST).strftime("%d %b")

    # Load ipos.json
    if not os.path.exists(IPOS_JSON_PATH):
        print(f"{IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    is_dict = isinstance(ipos_data, dict)
    items = ipos_data.get("ipos", ipos_data.get("data", [])) if is_dict else ipos_data

    updated_count = 0
    for item in items:
        raw_name = item.get("name", "")
        norm_key = clean_company_name(raw_name)

        # 1. Determine GMP value (manual override -> live scrape -> previous fallback)
        final_gmp = None
        final_pct = None

        if raw_name in manual_overrides and "gmp" in manual_overrides[raw_name]:
            final_gmp = float(manual_overrides[raw_name]["gmp"])
        elif norm_key in scraped_data:
            final_gmp = scraped_data[norm_key]["gmp"]
            final_pct = scraped_data[norm_key]["percent"]
        elif norm_key in gmp_store:
            saved = gmp_store[norm_key]
            final_gmp = float(saved.get("gmp", 0.0) if isinstance(saved, dict) else saved)

        if final_gmp is None:
            continue

        # Compute percentage if not directly available from scrape
        p_max = float(item.get("issuePriceMax", 0.0))
        if final_pct is None:
            final_pct = round((final_gmp / p_max * 100.0), 2) if (p_max > 0 and final_gmp > 0) else 0.0

        item["gmpAmount"] = final_gmp
        item["gmpPercent"] = final_pct

        # 2. Update rolling history for the Compose trend graph
        history = item.get("gmpHistory", [])
        
        # Avoid duplicate entries if executed multiple times on the same day
        if history and history[-1].get("dateLabel") == today_str:
            history[-1]["amount"] = final_gmp
        else:
            history.append({
                "dateLabel": today_str,
                "amount": final_gmp
            })

        # Keep the most recent 6 daily points
        item["gmpHistory"] = history[-6:]

        # Persist to local gmp_store
        gmp_store[norm_key] = {
            "name": raw_name,
            "gmp": final_gmp,
            "percent": final_pct
        }
        updated_count += 1

    # Save outputs
    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    with open(GMP_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(gmp_store, f, indent=2, ensure_ascii=False)

    print(f"Successfully synced GMP and history points for {updated_count} IPOs.")

if __name__ == "__main__":
    sync_gmp()
              
