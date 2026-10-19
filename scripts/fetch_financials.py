import os
import json
import re
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "scripts" in os.path.dirname(os.path.abspath(__file__)) else os.path.abspath(".")
IPOS_JSON_PATH = os.path.join(REPO_ROOT, "ipos.json")
OVERRIDES_PATH = os.path.join(REPO_ROOT, "manual_overrides.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
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
    """Converts '31 Mar 2026', '31 Mar 26', 'FY26' -> 'FY26'. Ignores stub interim periods."""
    if not period_str:
        return None
    # If it's a quarterly stub (e.g., Jun, Sep, Dec), prioritize full Mar year-ends when possible
    match = re.search(r"(?:20)?(\d{2})\b", period_str)
    if match:
        return f"FY{match.group(1)}"
    return None

def extract_financials_from_soup(soup: BeautifulSoup) -> List[Dict]:
    """
    Parses the Chittorgarh 'Company Financials (Restated)' table.
    Row labels: 'Period Ended', 'Total Income' / 'Revenue', 'Profit After Tax', 'NET Worth'.
    """
    tables = soup.find_all("table")
    for table in tables:
        table_text = table.get_text(separator=" ", strip=True).lower()
        if "profit after tax" in table_text and ("total income" in table_text or "revenue" in table_text):
            rows = table.find_all("tr")
            if len(rows) < 3:
                continue

            period_row, rev_row, pat_row, nw_row = None, None, None, None

            for row in rows:
                txt = row.get_text(separator=" ", strip=True).lower()
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not cells:
                    continue

                if "period ended" in txt and not period_row:
                    period_row = cells
                elif ("total income" in txt or "revenue" in txt) and not rev_row:
                    rev_row = cells
                elif "profit after tax" in txt and not pat_row:
                    pat_row = cells
                elif "net worth" in txt and not nw_row:
                    nw_row = cells

            if period_row and rev_row and pat_row:
                financial_records = []
                for i in range(1, len(period_row)):
                    yr_label = parse_financial_year(period_row[i])
                    if not yr_label:
                        continue

                    r_val = clean_num(rev_row[i]) if i < len(rev_row) else 0.0
                    p_val = clean_num(pat_row[i]) if i < len(pat_row) else 0.0
                    nw_val = clean_num(nw_row[i]) if nw_row and i < len(nw_row) else 0.0

                    if not any(f["year"] == yr_label for f in financial_records):
                        financial_records.append({
                            "year": yr_label,
                            "revenue": r_val,
                            "pat": p_val,
                            "netWorth": nw_val
                        })

                if financial_records:
                    return sorted(financial_records, key=lambda x: x["year"])[-3:]

    return []

def resolve_chittorgarh_url(company_name: str) -> Optional[str]:
    """Finds the company's direct IPO detail page on Chittorgarh."""
    search_slug = re.sub(r"[^a-zA-Z0-9\s]", "", company_name).lower().strip().replace(" ", "-")
    direct_candidates = [
        f"https://www.chittorgarh.com/ipo/{search_slug}-ipo/",
        f"https://www.chittorgarh.com/ipo/{search_slug.replace('-ltd', '')}-ipo/",
        f"https://www.chittorgarh.com/ipo/{clean_company_name(company_name).replace(' ', '-')}-ipo/"
    ]

    for cand_url in direct_candidates:
        try:
            r = requests.get(cand_url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code == 200 and "Company Financials" in r.text:
                return cand_url
        except Exception:
            continue

    try:
        q_url = f"https://www.chittorgarh.com/search/?search={requests.utils.quote(clean_company_name(company_name))}"
        r = requests.get(q_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/ipo/" in href and "-ipo/" in href:
                    return href if href.startswith("http") else f"https://www.chittorgarh.com{href}"
    except Exception:
        pass

    return None

def sync_all_financials():
    manual_overrides = {}
    aliases = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                aliases = raw_data.get("aliases", {})
                manual_overrides = {k: v for k, v in raw_data.items() if k != "aliases"}
        except Exception as e:
            print(f"[WARN] Failed to load manual overrides: {e}")

    if not os.path.exists(IPOS_JSON_PATH):
        print(f"[ERROR] {IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    items = ipos_data.get("ipos", ipos_data.get("data", [])) if isinstance(ipos_data, dict) else ipos_data

    updated_count = 0
    total_items = len(items)

    print(f"[Financials Sync] Starting sync across {total_items} IPOs...")

    for idx, item in enumerate(items):
        name = item.get("name", "")
        norm_name = clean_company_name(name)

        # 1. Manual Overrides Priority (e.g. Tonbo Imaging)
        override_entry = manual_overrides.get(norm_name) or manual_overrides.get(name.lower())
        if override_entry and "financials" in override_entry and override_entry["financials"]:
            item["financials"] = override_entry["financials"]
            updated_count += 1
            continue

        # 2. Preserve existing multi-year data
        if item.get("financials") and len(item["financials"]) > 0:
            updated_count += 1
            continue

        # 3. Only actively scrape for OPEN, UPCOMING, and recently CLOSED/LISTED IPOs
        status = str(item.get("status", "")).upper()
        if status not in ["OPEN", "UPCOMING", "CLOSED"]:
            if "financials" not in item:
                item["financials"] = []
            continue

        # Check aliases
        alias_target = aliases.get(norm_name) or aliases.get(name.lower())
        target_name = alias_target if alias_target else name

        ipo_url = resolve_chittorgarh_url(target_name)
        if ipo_url:
            try:
                res = requests.get(ipo_url, headers=HEADERS, timeout=12)
                if res.status_code == 200:
                    metrics = extract_financials_from_soup(BeautifulSoup(res.text, "html.parser"))
                    if metrics:
                        item["financials"] = metrics
                        updated_count += 1
                        print(f"[{updated_count}] Synced financials for: {name} ({len(metrics)} years)")
                    else:
                        item["financials"] = []
            except Exception as e:
                print(f"[WARN] Failed to scrape {name}: {e}")
                item["financials"] = []
        else:
            if "financials" not in item:
                item["financials"] = []

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"\n[Done] Successfully verified & synced financial scorecards for {updated_count}/{total_items} IPOs.")

if __name__ == "__main__":
    sync_all_financials()
    
