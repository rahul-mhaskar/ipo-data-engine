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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "scripts" in os.path.dirname(os.path.abspath(__file__)) else os.path.abspath(".")
IPOS_JSON_PATH = os.path.join(REPO_ROOT, "ipos.json")
OVERRIDES_PATH = os.path.join(REPO_ROOT, "manual_overrides.json")

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
        return round(float(clean), 1)
    except Exception:
        return 0.0

def parse_financial_year(period_str: str) -> Optional[str]:
    """Standardizes dates like '31-Mar-2024', '31-Mar-24', 'FY24' -> 'FY24'"""
    if not period_str:
        return None
    match = re.search(r"(?:20)?(\d{2})\b", period_str)
    if match:
        return f"FY{match.group(1)}"
    return None

def scrape_financials_directory() -> Dict[str, List[Dict]]:
    urls = [
        "https://www.chittorgarh.com/report/ipo-key-financial-details/161/all/?year=2026",
        "https://www.chittorgarh.com/report/ipo-key-financial-details/161/all/?year=2025"
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    results: Dict[str, List[Dict]] = {}

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=25)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            tables = soup.find_all("table")

            for table in tables:
                header_row = table.find("tr")
                if not header_row:
                    continue

                headers_text = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
                if not any("revenue" in h for h in headers_text):
                    continue

                # Dynamically locate the column indexes from the table header
                name_idx, period_idx, rev_idx, pat_idx, nw_idx = -1, -1, -1, -1, -1
                for idx, h in enumerate(headers_text):
                    if any(k in h for k in ["company", "issuer"]) and name_idx == -1:
                        name_idx = idx
                    elif "period" in h and period_idx == -1:
                        period_idx = idx
                    elif "revenue" in h and rev_idx == -1:
                        rev_idx = idx
                    elif "pat" in h and pat_idx == -1:
                        pat_idx = idx
                    elif "net worth" in h and nw_idx == -1:
                        nw_idx = idx

                # Default fallback offsets if headers omit specific labels
                if name_idx == -1: name_idx = 0
                if period_idx == -1: period_idx = 4
                if rev_idx == -1: rev_idx = 6
                if pat_idx == -1: pat_idx = 7
                if nw_idx == -1: nw_idx = 9

                rows = table.find_all("tr")[1:]
                for row in rows:
                    cols = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
                    if len(cols) <= max(name_idx, period_idx, rev_idx, pat_idx):
                        continue

                    raw_name = cols[name_idx]
                    clean_name = re.sub(r"\b(IPO|SME|BSE|NSE)\b", "", raw_name, flags=re.IGNORECASE).strip()
                    norm_key = clean_company_name(clean_name)
                    if not norm_key or len(norm_key) < 3:
                        continue

                    yr_str = parse_financial_year(cols[period_idx])
                    if not yr_str:
                        continue

                    rev = clean_num(cols[rev_idx])
                    pat = clean_num(cols[pat_idx])
                    nw = clean_num(cols[nw_idx]) if nw_idx < len(cols) else 0.0

                    entry = {
                        "year": yr_str,
                        "revenue": rev,
                        "pat": pat,
                        "netWorth": nw
                    }

                    if norm_key not in results:
                        results[norm_key] = []

                    if not any(f["year"] == yr_str for f in results[norm_key]):
                        results[norm_key].append(entry)

        except Exception as e:
            print(f"[WARN] Error fetching {url}: {e}")

    # Retain the 3 most recent chronologically ordered financial years
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
        print(f"[ERROR] {IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    items = ipos_data.get("ipos", ipos_data.get("data", [])) if isinstance(ipos_data, dict) else ipos_data

    updated_count = 0
    for item in items:
        name = item.get("name", "")
        norm_name = clean_company_name(name)

        # 1. Manual Overrides Priority
        override_entry = manual_overrides.get(norm_name) or manual_overrides.get(name.lower())
        if override_entry and "financials" in override_entry and override_entry["financials"]:
            item["financials"] = override_entry["financials"]
            updated_count += 1
            continue

        # 2. Check Aliases
        alias_target = aliases.get(norm_name) or aliases.get(name.lower())
        lookup_name = clean_company_name(alias_target) if alias_target else norm_name

        # 3. Fuzzy Match against Scraped Directory
        best_match_key = None
        highest_score = 0
        for scraped_key in scraped_financials:
            score = fuzz.token_set_ratio(lookup_name, scraped_key)
            if score > highest_score and score >= 80:
                highest_score = score
                best_match_key = scraped_key

        if best_match_key and scraped_financials[best_match_key]:
            item["financials"] = scraped_financials[best_match_key]
            updated_count += 1
        elif not item.get("financials"):
            item["financials"] = []

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"Successfully synced financial scorecards for {updated_count}/{len(items)} IPOs.")

if __name__ == "__main__":
    sync_all_financials()
    
