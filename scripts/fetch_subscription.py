import os
import json
import re
from typing import List, Dict, Optional, Set
import requests
from bs4 import BeautifulSoup

IPOS_JSON_PATH = "ipos.json"
OVERRIDES_PATH = "manual_overrides.json"

NOISE_WORDS = {
    "limited", "ltd", "pvt", "private", "ipo", "india", "sme", 
    "technologies", "tech", "solutions", "industries", "services", 
    "bse", "nse", "corp", "corporation"
}

def clean_multiplier(val_str) -> float:
    if not val_str:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", str(val_str).replace(",", ""))
    return float(match.group(1)) if match else 0.0

def extract_clean_name(cell) -> str:
    """Extracts company name, stripping out InvestorGain's appended GMP and ticker badges."""
    # Priority 1: Check if an anchor tag exists with clean title or text
    a_tag = cell.find("a")
    raw = a_tag.get_text(strip=True) if a_tag else cell.get_text(separator=" ", strip=True)
    
    # Priority 2: Cut off everything once 'GMP:' or 'L@' starts
    raw = re.split(r"gmp\s*:", raw, flags=re.IGNORECASE)[0]
    raw = re.split(r"l\s*@", raw, flags=re.IGNORECASE)[0]
    
    # Strip ticker noise & exchange codes
    cleaned = re.sub(r"\b(IPO|SME|BSE|NSE)\b", " ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", cleaned)
    return " ".join(cleaned.split())

def get_tokens(name: str) -> Set[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    return {t for t in cleaned.split() if t not in NOISE_WORDS and len(t) > 1}

def matches_target(target_name: str, cand_name: str, aliases: Dict[str, str]) -> bool:
    t_clean = target_name.lower().strip()
    c_clean = cand_name.lower().strip()

    # Manual alias
    if aliases.get(t_clean) == c_clean or aliases.get(c_clean) == t_clean:
        return True

    t_tokens = get_tokens(target_name)
    c_tokens = get_tokens(cand_name)

    if not t_tokens or not c_tokens:
        return False

    # Check direct containment or subset
    if t_tokens.issubset(c_tokens) or c_tokens.issubset(t_tokens):
        return True

    # At least 2 significant words overlap (e.g., 'Core4 Engineers')
    common = t_tokens.intersection(c_tokens)
    if len(common) >= 2 or (len(t_tokens) == 1 and len(common) == 1):
        return True

    return False

def scrape_table(url: str) -> List[Dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        results = []

        for table in soup.find_all("table"):
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
                cells = row.find_all(["td", "th"])
                if len(cells) >= 4:
                    first_cell = cells[0]
                    clean_name = extract_clean_name(first_cell)
                    
                    if not clean_name or any(h in clean_name.lower() for h in ["issuer", "company", "ipo name", "closing date"]):
                        continue

                    cols = [td.get_text(strip=True) for td in cells]
                    q = clean_multiplier(cols[headers_map["qib"]]) if "qib" in headers_map and headers_map["qib"] < len(cols) else 0.0
                    n = clean_multiplier(cols[headers_map["nii"]]) if "nii" in headers_map and headers_map["nii"] < len(cols) else 0.0
                    r = clean_multiplier(cols[headers_map["retail"]]) if "retail" in headers_map and headers_map["retail"] < len(cols) else 0.0
                    tot = clean_multiplier(cols[headers_map["total"]]) if "total" in headers_map and headers_map["total"] < len(cols) else 0.0

                    if tot > 0.0 or r > 0.0:
                        results.append({
                            "name": clean_name,
                            "qib": q,
                            "nii": n,
                            "retail": r,
                            "total": tot
                        })
        return results
    except Exception as e:
        print(f"[Sub Scraper] Error scraping {url}: {e}")
        return []

def update_root_ipos_json():
    if not os.path.exists(IPOS_JSON_PATH):
        print(f"{IPOS_JSON_PATH} not found.")
        return

    aliases = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                aliases = json.load(f).get("aliases", {})
        except Exception:
            pass

    urls = [
        "https://www.investorgain.com/report/ipo-subscription-live/333/",
        "https://www.investorgain.com/report/live-ipo-subscription-status/331/"
    ]
    sub_data = []
    seen = set()
    for u in urls:
        for r in scrape_table(u):
            k = r["name"].lower()
            if k not in seen:
                seen.add(k)
                sub_data.append(r)

    print(f"[Sub Scraper] Scraped {len(sub_data)} subscription records.")
    for d in sub_data:
        print(f"   -> Parsed: '{d['name']}' | Total: {d['total']}x")

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("ipos", data.get("data", [])) if isinstance(data, dict) else data
    matched_count = 0

    for item in items:
        ipo_name = item.get("name", "")
        
        # Check against scraped subscription data
        matched = None
        for cand in sub_data:
            if matches_target(ipo_name, cand["name"], aliases):
                matched = cand
                break

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
            print(f"[MATCHED] {ipo_name} -> {matched['total']}x")

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[SUCCESS] Updated subscriptions for {matched_count}/{len(items)} IPOs in {IPOS_JSON_PATH}")

if __name__ == "__main__":
    update_root_ipos_json()
    
