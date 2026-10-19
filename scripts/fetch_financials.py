import os
import json
import re
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup

try:
    from rapidfuzz import fuzz
except ImportError:
    import difflib
    class FuzzFallback:
        @staticmethod
        def token_set_ratio(s1: str, s2: str) -> int:
            t1 = " ".join(sorted(set(s1.split())))
            t2 = " ".join(sorted(set(s2.split())))
            return int(difflib.SequenceMatcher(None, t1, t2).ratio() * 100)
    fuzz = FuzzFallback()

IPOS_JSON_PATH = "ipos.json"
OVERRIDES_PATH = "manual_overrides.json"

INDUSTRY_NOISE = {
    "pharma", "pharmaceuticals", "jewels", "jewellers", "tech", 
    "technologies", "infra", "infrastructure", "finance", "enterprises", 
    "limited", "ltd", "pvt", "private", "corp", "ipo", "india", "sme"
}

def clean_company_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    tokens = [t for t in cleaned.split() if t not in INDUSTRY_NOISE]
    return " ".join(tokens) if tokens else cleaned

def clean_num(val_str: str) -> float:
    if not val_str:
        return 0.0
    clean = re.sub(r"[^\d.-]", "", val_str)
    try:
        return round(float(clean), 2)
    except Exception:
        return 0.0

def scrape_financials_directory() -> Dict[str, List[Dict]]:
    """
    Scrapes the aggregate public financial disclosures directory.
    Extracts multi-period Revenue, PAT, and Net Worth in ₹ Cr.
    """
    url = "https://www.chittorgarh.com/report/ipo-key-financial-details/161/all/?year=2026"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    results = {}
    try:
        res = requests.get(url, headers=headers, timeout=25)
        if res.status_code != 200:
            print(f"[WARN] Failed to fetch financials table: HTTP {res.status_code}")
            return results

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return results

        rows = table.find_all("tr")
        for row in rows[1:]:
            cols = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
            if len(cols) < 5:
                continue

            raw_name = cols[0]
            # Strip trailing status tags like 'IPO', 'SME', 'BSE', 'NSE'
            clean_name = re.sub(r"\b(IPO|SME|BSE|NSE)\b", "", raw_name, flags=re.IGNORECASE).strip()
            norm_key = clean_company_name(clean_name)

            # Period extraction (e.g., '31-Mar-2025' -> 'FY25')
            period_raw = cols[1]
            match_yr = re.search(r"(\d{2,4})$", period_raw)
            yr_str = f"FY{match_yr.group(1)[-2:]}" if match_yr else period_raw

            rev = clean_num(cols[2])      # Revenue from operations
            pat = clean_num(cols[3])      # Profit After Tax (PAT)
            net_worth = clean_num(cols[4]) # Net Worth / Reserves

            entry = {
                "year": yr_str,
                "revenue": rev,
                "pat": pat,
                "netWorth": net_worth
            }

            if norm_key not in results:
                results[norm_key] = []
            
            # Keep up to 3 most recent financial periods
            if not any(f["year"] == yr_str for f in results[norm_key]):
                results[norm_key].append(entry)

    except Exception as e:
        print(f"[ERROR] Financials scraper encountered an exception: {e}")

    # Ensure chronological order (e.g. FY24, FY25, FY26)
    for k in results:
        results[k] = sorted(results[k], key=lambda x: x["year"])[-3:]

    print(f"[Financials Scraper] Extracted financial scorecards for {len(results)} issuers.")
    return results

def sync_all_financials():
    scraped_financials = scrape_financials_directory()

    manual_overrides = {}
    aliases = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                aliases = raw_data.get("aliases", {})
                manual_overrides = {k: v for k, v in raw_data.items() if k != "aliases"}
        except Exception:
            pass

    if not os.path.exists(IPOS_JSON_PATH):
        print(f"{IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    items = ipos_data.get("ipos", ipos_data.get("data", [])) if isinstance(ipos_data, dict) else ipos_data

    updated_count = 0
    for item in items:
        name = item.get("name", "")
        norm_name = clean_company_name(name)

        # 1. Check manual overrides first
        override_entry = manual_overrides.get(norm_name) or manual_overrides.get(name.lower())
        if override_entry and "financials" in override_entry and override_entry["financials"]:
            item["financials"] = override_entry["financials"]
            updated_count += 1
            continue

        # 2. Check alias mapping
        alias_target = aliases.get(norm_name) or aliases.get(name.lower())
        lookup_name = clean_company_name(alias_target) if alias_target else norm_name

        # 3. Match against scraped financials using token ratio
        best_match_key = None
        highest_score = 0
        for scraped_key in scraped_financials:
            score = fuzz.token_set_ratio(lookup_name, scraped_key)
            if score > highest_score and score >= 85:
                highest_score = score
                best_match_key = scraped_key

        if best_match_key:
            item["financials"] = scraped_financials[best_match_key]
            updated_count += 1
        elif not item.get("financials"):
            item["financials"] = []

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"Successfully synced financial scorecards for {updated_count}/{len(items)} IPOs.")

if __name__ == "__main__":
    sync_all_financials()
      
