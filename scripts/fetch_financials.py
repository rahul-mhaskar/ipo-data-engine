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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

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
    """Extracts FY label: '31 Mar 2026' -> 'FY26', 'FY24' -> 'FY24'"""
    if not period_str:
        return None
    match = re.search(r"(?:20)?(\d{2})\b", period_str)
    if match:
        return f"FY{match.group(1)}"
    return None

def scrape_individual_financials(ipo_url: str) -> List[Dict]:
    """Scrapes the server-side static financial table on an individual IPO page."""
    try:
        res = requests.get(ipo_url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        tables = soup.find_all("table")

        for table in tables:
            headers_text = [th.get_text(strip=True).lower() for th in table.find_all(["th", "td"])]
            # Match standard company financials table
            if any("revenue" in h or "total income" in h for h in headers_text) and any("pat" in h or "profit after tax" in h for h in headers_text):
                rows = table.find_all("tr")
                if len(rows) < 2:
                    continue

                col_headers = [td.get_text(strip=True) for td in rows[0].find_all(["th", "td"])]
                financial_records = []

                # Many detail pages arrange periods across columns (horizontal format)
                # or across rows (vertical format)
                rev_row, pat_row, nw_row = None, None, None
                for row in rows:
                    row_txt = row.get_text(separator=" ", strip=True).lower()
                    cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                    if "revenue" in row_txt or "total income" in row_txt:
                        rev_row = cells
                    elif "pat" in row_txt or "profit after tax" in row_txt:
                        pat_row = cells
                    elif "net worth" in row_txt:
                        nw_row = cells

                if rev_row and pat_row and len(col_headers) >= 2:
                    # Horizontal layout: column 1..N are dates
                    for col_idx in range(1, len(col_headers)):
                        yr_label = parse_financial_year(col_headers[col_idx])
                        if not yr_label:
                            continue
                        r_val = clean_num(rev_row[col_idx]) if col_idx < len(rev_row) else 0.0
                        p_val = clean_num(pat_row[col_idx]) if col_idx < len(pat_row) else 0.0
                        nw_val = clean_num(nw_row[col_idx]) if nw_row and col_idx < len(nw_row) else 0.0

                        financial_records.append({
                            "year": yr_label,
                            "revenue": r_val,
                            "pat": p_val,
                            "netWorth": nw_val
                        })

                    if financial_records:
                        return sorted(financial_records, key=lambda x: x["year"])[-3:]

        return []
    except Exception:
        return []

def scrape_financials_directory() -> Dict[str, List[Dict]]:
    """
    Crawls active IPO profiles and aggregates financial metrics.
    """
    directory_url = "https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/all/?year=2026"
    results: Dict[str, List[Dict]] = {}

    try:
        res = requests.get(directory_url, headers=HEADERS, timeout=25)
        if res.status_code != 200:
            print(f"[WARN] Failed to fetch directory: HTTP {res.status_code}")
            return results

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return results

        rows = table.find_all("tr")[1:]
        links_to_crawl = []

        for row in rows:
            a_tag = row.find("a")
            if a_tag and a_tag.get("href") and "/ipo/" in a_tag.get("href"):
                name = a_tag.get_text(strip=True)
                clean_name = re.sub(r"\b(IPO|SME|BSE|NSE)\b", "", name, flags=re.IGNORECASE).strip()
                norm_key = clean_company_name(clean_name)
                full_url = a_tag["href"] if a_tag["href"].startswith("http") else f"https://www.chittorgarh.com{a_tag['href']}"
                links_to_crawl.append((norm_key, full_url))

        print(f"[Financials Scraper] Found {len(links_to_crawl)} IPO detail profiles to crawl.")

        # Limit to the top 25 active/recent IPOs to respect workflow execution limits
        for norm_key, url in links_to_crawl[:25]:
            metrics = scrape_individual_financials(url)
            if metrics:
                results[norm_key] = metrics

    except Exception as e:
        print(f"[ERROR] Exception during directory scrape: {e}")

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

        # 3. Fuzzy Match against Scraped Profiles
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
        elif "financials" not in item:
            item["financials"] = []

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"Successfully synced financial scorecards for {updated_count}/{len(items)} IPOs.")

if __name__ == "__main__":
    sync_all_financials()
    
