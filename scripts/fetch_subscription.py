import os
import json
import re
from typing import List, Dict, Optional, Set
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

# Common industry sectors and corporate noise to filter out when checking for entity identity
INDUSTRY_NOISE: Set[str] = {
    "pharma", "pharmaceuticals", "pharmaceutical", "jewels", "jewellers", 
    "jewellery", "tech", "technologies", "technology", "solutions", 
    "infra", "infrastructure", "finance", "financial", "enterprises", 
    "logistics", "industries", "energy", "power", "capital", "holdings",
    "limited", "ltd", "pvt", "private", "corp", "corporation", "ipo",
    "india", "global", "international", "services", "group", "allotted",
    "closing", "today", "sme", "bse", "nse"
}

# Common conglomerate parent prefixes
PARENT_PREFIXES: Set[str] = {
    "reliance", "tata", "adani", "bajaj", "birla", "mahindra", "godrej", "l&t"
}

def clean_multiplier(val_str) -> float:
    if not val_str:
        return 0.0
    match = re.search(r"(\d+(\.\d+)?)", str(val_str).replace(",", ""))
    return float(match.group(1)) if match else 0.0

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

def find_best_sub_match(
    target_ipo: Dict, 
    candidates: List[Dict], 
    aliases: Dict[str, str],
    min_score: int = 86
) -> Optional[Dict]:
    """
    Multi-stage composite matcher:
    1. Direct Symbol match
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

def scrape_table(url: str) -> List[Dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            print(f"[Sub Scraper] HTTP {res.status_code} from {url}")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []

        results = []
        for table in tables:
            headers_map = {}
            rows = table.find_all("tr")
            if not rows:
                continue

            for row in rows[:2]:
                for idx, cell in enumerate(row.find_all(["th", "td"])):
                    txt = cell.get_text(strip=True).upper()
                    if "QIB" in txt:
                        headers_map["qib"] = idx
                    elif "NII" in txt or "HNI" in txt or "sNII" in txt or "bNII" in txt:
                        headers_map["nii"] = idx
                    elif "RETAIL" in txt or "RII" in txt:
                        headers_map["retail"] = idx
                    elif "TOTAL" in txt:
                        headers_map["total"] = idx

            if "total" not in headers_map and "retail" not in headers_map:
                continue

            for row in rows[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cols) >= 4:
                    raw_name = cols[0]
                    if any(h in raw_name.lower() for h in ["issuer", "company", "ipo name", "total"]):
                        continue

                    q = clean_multiplier(cols[headers_map["qib"]]) if "qib" in headers_map and headers_map["qib"] < len(cols) else 0.0
                    n = clean_multiplier(cols[headers_map["nii"]]) if "nii" in headers_map and headers_map["nii"] < len(cols) else 0.0
                    r = clean_multiplier(cols[headers_map["retail"]]) if "retail" in headers_map and headers_map["retail"] < len(cols) else 0.0
                    tot = clean_multiplier(cols[headers_map["total"]]) if "total" in headers_map and headers_map["total"] < len(cols) else 0.0

                    # Extract pure clean name
                    clean_name = re.sub(r"\b(IPO|SME|BSE|NSE)\b", "", raw_name, flags=re.IGNORECASE).strip()

                    if tot > 0.0 or r > 0.0:
                        results.append({
                            "name": clean_name,
                            "raw_name": raw_name,
                            "qib": q,
                            "nii": n,
                            "retail": r,
                            "total": tot
                        })
        return results
    except Exception as e:
        print(f"[Sub Scraper] Error scraping {url}: {e}")
        return []

def fetch_all_sources() -> List[Dict]:
    urls = [
        "https://www.investorgain.com/report/live-ipo-subscription-status/331/",
        "https://www.investorgain.com/report/ipo-subscription-status-live-bidding-data-bse-nse/333/"
    ]
    all_data = []
    seen = set()
    for url in urls:
        records = scrape_table(url)
        for r in records:
            key = r["name"].lower().strip()
            if key not in seen:
                seen.add(key)
                all_data.append(r)
    return all_data

def update_root_ipos_json():
    if not os.path.exists(IPOS_JSON_PATH):
        print(f"{IPOS_JSON_PATH} not found at repository root.")
        return

    # Load manual overrides and aliases
    aliases = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw_overrides = json.load(f)
                aliases = raw_overrides.get("aliases", {})
        except Exception as e:
            print(f"[WARN] Failed to load manual overrides: {e}")

    sub_data = fetch_all_sources()
    print(f"[Sub Scraper] Scraped {len(sub_data)} valid subscription entries across sources.")

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    is_dict = isinstance(data, dict)
    items = data.get("ipos", data.get("data", [])) if is_dict else data

    matched_count = 0
    for item in items:
        # Strip old legacy nested structure if still present
        item.pop("subscription", None)

        matched = find_best_sub_match(item, sub_data, aliases)
        if matched:
            if matched["total"] > 0:
                item["subscriptionTotal"] = matched["total"]
            if matched["qib"] > 0:
                item["subscriptionQIB"] = matched["qib"]
            if matched["nii"] > 0:
                item["subscriptionHNI"] = matched["nii"]
            if matched["retail"] > 0:
                item["subscriptionRetail"] = matched["retail"]
            matched_count += 1

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[SUCCESS] Updated subscriptions for {matched_count}/{len(items)} IPOs in {IPOS_JSON_PATH}")

if __name__ == "__main__":
    update_root_ipos_json()
    
