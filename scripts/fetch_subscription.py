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

INDUSTRY_NOISE: Set[str] = {
    "limited", "ltd", "pvt", "private", "corp", "corporation", "ipo",
    "india", "services", "solutions", "technologies", "tech", "group",
    "industries", "sme", "bse", "nse", "enterprises"
}

def clean_multiplier(val_str) -> float:
    if not val_str:
        return 0.0
    # Capture first decimal or float number
    match = re.search(r"(\d+(?:\.\d+)?)", str(val_str).replace(",", ""))
    return float(match.group(1)) if match else 0.0

def sanitize_scraped_name(raw_name: str) -> str:
    """Strips InvestorGain table noise like GMP badges, price tags, and exchange markers."""
    if not raw_name:
        return ""
    # Remove GMP badges: GMP:₹247 (22.07%)
    s = re.sub(r"gmp\s*:\s*[₹rs\.\d\s\(\)\+\-%]+", " ", raw_name, flags=re.IGNORECASE)
    # Remove listing price tags: L@115.00 (-9.45%)
    s = re.sub(r"l\s*@\s*[\d\.\s\(\)\+\-%]+", " ", s, flags=re.IGNORECASE)
    # Remove exchange markers and single-letter status codes at word boundaries
    s = re.sub(r"\b(BSE|NSE|SME|IPO)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b[OCLU]\b\.?", " ", s, flags=re.IGNORECASE)
    # Remove special punctuation
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    return " ".join(s.split())

def extract_tokens(name: str) -> Set[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    return {t for t in cleaned.split() if t not in INDUSTRY_NOISE and len(t) > 1}

def check_alias_match(name1: str, name2: str, aliases: Dict[str, str]) -> bool:
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
    min_score: int = 70
) -> Optional[Dict]:
    target_name = target_ipo.get("name", "")
    target_symbol = target_ipo.get("symbol", "").strip().upper()
    target_tokens = extract_tokens(target_name)

    # 1. Direct symbol matching
    if target_symbol and len(target_symbol) >= 3 and target_symbol != "IPO":
        for cand in candidates:
            if target_symbol in cand.get("raw_name", "").upper():
                return cand

    best_candidate = None
    highest_score = 0

    for cand in candidates:
        cand_name = cand.get("name", "")

        # 2. Manual overrides alias check
        if check_alias_match(target_name, cand_name, aliases):
            return cand

        cand_tokens = extract_tokens(cand_name)

        # 3. Token overlap fallback
        if target_tokens and cand_tokens:
            if target_tokens.issubset(cand_tokens) or cand_tokens.issubset(target_tokens):
                return cand

        # 4. Fuzzy token ratio
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

            for row in rows[:3]:
                for idx, cell in enumerate(row.find_all(["th", "td"])):
                    txt = cell.get_text(strip=True).upper()
                    if "QIB" in txt:
                        headers_map["qib"] = idx
                    elif any(h in txt for h in ["NII", "HNI", "SNII", "BNII"]):
                        headers_map["nii"] = idx
                    elif any(r in txt for r in ["RETAIL", "RII"]):
                        headers_map["retail"] = idx
                    elif "TOTAL" in txt:
                        headers_map["total"] = idx

            if "total" not in headers_map and "retail" not in headers_map:
                continue

            for row in rows[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if len(cols) >= 4:
                    raw_name = cols[0]
                    if any(h in raw_name.lower() for h in ["issuer", "company", "ipo name", "closing date"]):
                        continue

                    q = clean_multiplier(cols[headers_map["qib"]]) if "qib" in headers_map and headers_map["qib"] < len(cols) else 0.0
                    n = clean_multiplier(cols[headers_map["nii"]]) if "nii" in headers_map and headers_map["nii"] < len(cols) else 0.0
                    r = clean_multiplier(cols[headers_map["retail"]]) if "retail" in headers_map and headers_map["retail"] < len(cols) else 0.0
                    tot = clean_multiplier(cols[headers_map["total"]]) if "total" in headers_map and headers_map["total"] < len(cols) else 0.0

                    clean_name = sanitize_scraped_name(raw_name)
                    if len(clean_name) > 2 and (tot > 0.0 or r > 0.0 or n > 0.0 or q > 0.0):
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
        "https://www.investorgain.com/report/ipo-subscription-live/333/",
        "https://www.investorgain.com/report/live-ipo-subscription-status/331/"
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
    
