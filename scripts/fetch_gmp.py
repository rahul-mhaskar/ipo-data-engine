import os
import json
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Set
import requests
from bs4 import BeautifulSoup

try:
    from rapidfuzz import fuzz
except ImportError:
    # Fallback to standard library ratio if rapidfuzz is not yet installed
    import difflib
    class FuzzFallback:
        @staticmethod
        def token_set_ratio(s1: str, s2: str) -> int:
            t1 = " ".join(sorted(set(s1.split())))
            t2 = " ".join(sorted(set(s2.split())))
            return int(difflib.SequenceMatcher(None, t1, t2).ratio() * 100)
    fuzz = FuzzFallback()

IST = timezone(timedelta(hours=5, minutes=30))
IPOS_JSON_PATH = "ipos.json"
GMP_DATA_PATH = "gmp_data.json"
OVERRIDES_PATH = "manual_overrides.json"

# Sector noise and corporate suffixes to strip when isolating the unique entity
INDUSTRY_NOISE: Set[str] = {
    "pharma", "pharmaceuticals", "pharmaceutical", "jewels", "jewellers", 
    "jewellery", "tech", "technologies", "technology", "solutions", 
    "infra", "infrastructure", "finance", "financial", "enterprises", 
    "logistics", "industries", "energy", "power", "capital", "holdings",
    "limited", "ltd", "pvt", "private", "corp", "corporation", "ipo",
    "india", "global", "international", "services", "group", "allotted",
    "closing", "today", "sme", "bse", "nse"
}

# Common parent conglomerate prefixes
PARENT_PREFIXES: Set[str] = {
    "reliance", "tata", "adani", "bajaj", "birla", "mahindra", "godrej", "l&t"
}

def clean_tokens(name: str) -> Set[str]:
    """Extracts alphanumeric words from a name."""
    if not name:
        return set()
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    return set(cleaned.split())

def extract_core_entity(name: str) -> Set[str]:
    """
    Strips noise words and conglomerate prefixes to isolate the distinct entity.
    E.g., 'Reliance Jio Platforms Ltd' -> {'jio'}
    """
    tokens = clean_tokens(name) - INDUSTRY_NOISE
    specific_tokens = tokens - PARENT_PREFIXES
    return specific_tokens if specific_tokens else tokens

def check_alias_match(name1: str, name2: str, aliases: Dict[str, str]) -> bool:
    """Checks explicit bidirectional mappings from manual_overrides.json."""
    n1 = name1.strip().lower()
    n2 = name2.strip().lower()
    if aliases.get(n1) == n2 or aliases.get(n2) == n1:
        return True
    for k, v in aliases.items():
        if (k in n1 and v in n2) or (v in n1 and k in n2):
            return True
    return False

def find_best_gmp_match(
    target_ipo: Dict, 
    candidates: List[Dict], 
    aliases: Dict[str, str],
    min_score: int = 86
) -> Optional[Dict]:
    """
    Multi-stage composite matcher:
    1. Direct Symbol match (if both provide symbol)
    2. Explicit manual aliases
    3. Core brand anchor match (prevents cross-matching distinct Pharma/Jewel companies)
    4. Guarded Token Set Fuzzy ratio
    """
    target_name = target_ipo.get("name", "")
    target_symbol = target_ipo.get("symbol", "").strip().upper()
    target_close = target_ipo.get("closeDate", "").strip()

    # 1. Symbol Match (100% deterministic)
    if target_symbol:
        for cand in candidates:
            cand_symbol = cand.get("symbol", "").strip().upper()
            if cand_symbol and cand_symbol == target_symbol:
                return cand

    target_core = extract_core_entity(target_name)
    best_candidate = None
    highest_score = 0

    for cand in candidates:
        cand_name = cand.get("name", "")
        cand_close = cand.get("closeDate", "").strip()

        # 2. Manual Alias Match
        if check_alias_match(target_name, cand_name, aliases):
            return cand

        # 3. Core Anchor Overlap Gate
        cand_core = extract_core_entity(cand_name)
        common_anchors = target_core.intersection(cand_core)
        
        # If both have meaningful words and share zero anchors, reject immediately.
        # This prevents 'Zenith Pharma' from matching 'Apex Pharma'.
        if target_core and cand_core and not common_anchors:
            continue

        # 4. Date Gate (if close dates are known)
        if target_close and cand_close and target_close != cand_close:
            continue

        # 5. Fuzzy Match on Cleaned Strings
        score = fuzz.token_set_ratio(target_name.lower(), cand_name.lower())
        if score > highest_score and score >= min_score:
            highest_score = score
            best_candidate = cand

    return best_candidate

def scrape_investorgain_gmp() -> List[Dict]:
    url = "https://www.investorgain.com/report/ipo-gmp-live/331/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    scraped_list = []
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            print(f"[GMP Scraper] HTTP {res.status_code} from InvestorGain.")
            return scraped_list

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            print("[GMP Scraper] Table not found on page.")
            return scraped_list

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

                # Clean name of trailing UI markers
                clean_name = re.sub(r"\b(IPO|SME|BSE|NSE)\b", "", raw_name, flags=re.IGNORECASE).strip()

                if clean_name:
                    scraped_list.append({
                        "name": clean_name,
                        "raw_name": raw_name,
                        "gmp": gmp_amt,
                        "percent": gmp_pct
                    })

        print(f"[GMP Scraper] Extracted {len(scraped_list)} live GMP records.")
    except Exception as e:
        print(f"[GMP Scraper] Error during fetch: {e}")

    return scraped_list

def sync_gmp():
    scraped_candidates = scrape_investorgain_gmp()

    # Load manual overrides
    manual_overrides = {}
    aliases = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw_overrides = json.load(f)
                aliases = raw_overrides.get("aliases", {})
                manual_overrides = {k: v for k, v in raw_overrides.items() if k != "aliases"}
        except Exception as e:
            print(f"[WARN] Failed to load manual overrides: {e}")

    # Load persistent GMP storage
    gmp_store = {}
    if os.path.exists(GMP_DATA_PATH):
        try:
            with open(GMP_DATA_PATH, "r", encoding="utf-8") as f:
                gmp_store = json.load(f)
        except Exception:
            pass

    today_str = datetime.now(IST).strftime("%d %b")

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
        clean_key = re.sub(r"[^a-z0-9]", "", raw_name.lower())

        final_gmp = None
        final_pct = None

        # 1. Priority 1: Check manual overrides file
        override_entry = manual_overrides.get(raw_name.lower()) or manual_overrides.get(raw_name)
        if override_entry and "gmp" in override_entry:
            final_gmp = float(override_entry["gmp"])

        # 2. Priority 2: Multi-layer composite matching against scraped data
        if final_gmp is None:
            match = find_best_gmp_match(item, scraped_candidates, aliases)
            if match:
                final_gmp = match["gmp"]
                final_pct = match["percent"]

        # 3. Priority 3: Fallback to historical local store
        if final_gmp is None and clean_key in gmp_store:
            saved = gmp_store[clean_key]
            final_gmp = float(saved.get("gmp", 0.0) if isinstance(saved, dict) else saved)

        if final_gmp is None:
            continue

        # Recalculate percentage if needed
        p_max = float(item.get("issuePriceMax", 0.0))
        if final_pct is None:
            final_pct = round((final_gmp / p_max * 100.0), 2) if (p_max > 0 and final_gmp > 0) else 0.0

        item["gmpAmount"] = final_gmp
        item["gmpPercent"] = final_pct

        # Update rolling 6-day history for Compose Canvas sparkline
        history = item.setdefault("gmpHistory", [])
        if history and history[-1].get("dateLabel") == today_str:
            history[-1]["amount"] = final_gmp
        else:
            history.append({
                "dateLabel": today_str,
                "amount": final_gmp
            })

        # Retain last 6 days
        item["gmpHistory"] = history[-6:]

        # Update local persistence store
        gmp_store[clean_key] = {
            "name": raw_name,
            "gmp": final_gmp,
            "percent": final_pct
        }
        updated_count += 1

    # Save updated datasets
    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    with open(GMP_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(gmp_store, f, indent=2, ensure_ascii=False)

    print(f"Successfully synced GMP and history points for {updated_count}/{len(items)} IPOs.")

if __name__ == "__main__":
    sync_gmp()
