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
    import difflib
    class FuzzFallback:
        @staticmethod
        def token_set_ratio(s1: str, s2: str) -> int:
            t1 = " ".join(sorted(set(s1.split())))
            t2 = " ".join(sorted(set(s2.split())))
            return int(difflib.SequenceMatcher(None, t1, t2).ratio() * 100)
    fuzz = FuzzFallback()

IST = timezone(timedelta(hours=5, minutes=30))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "scripts" in os.path.dirname(os.path.abspath(__file__)) else os.path.abspath(".")
IPOS_JSON_PATH = os.path.join(REPO_ROOT, "ipos.json")
GMP_DATA_PATH = os.path.join(REPO_ROOT, "gmp_data.json")
OVERRIDES_PATH = os.path.join(REPO_ROOT, "manual_overrides.json")

NOISE_WORDS: Set[str] = {
    "limited", "ltd", "pvt", "private", "corp", "corporation", "ipo",
    "india", "services", "solutions", "technologies", "tech", "group",
    "industries", "sme", "bse", "nse", "enterprises"
}

def clean_company_name(name: str) -> str:
    if not name:
        return ""
    s = name.replace("’", "").replace("'", "")
    s = re.sub(r"\b(IPO|SME|BSE|NSE|Ltd\.?|Limited|Pvt\.?|Private|Industries|Industry|Enterprises|Services|Holdings)\b", "", s, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", s.lower())

def extract_clean_name(cell) -> str:
    """Extracts company name, stripping out InvestorGain's appended GMP and ticker badges."""
    a_tag = cell.find("a")
    raw = a_tag.get_text(strip=True) if a_tag else cell.get_text(separator=" ", strip=True)
    
    # Cut off everything once 'GMP:' or 'L@' starts
    raw = re.split(r"gmp\s*:", raw, flags=re.IGNORECASE)[0]
    raw = re.split(r"l\s*@", raw, flags=re.IGNORECASE)[0]
    
    cleaned = re.sub(r"\b(IPO|SME|BSE|NSE)\b", " ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b[OCLU]\b\.?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", cleaned)
    return " ".join(cleaned.split())

def get_tokens(name: str) -> Set[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    return {t for t in cleaned.split() if t not in NOISE_WORDS and len(t) > 1}

def matches_target(target_name: str, cand_name: str, aliases: Dict[str, str]) -> bool:
    t_clean = target_name.lower().strip()
    c_clean = cand_name.lower().strip()

    # Manual alias check
    if aliases.get(t_clean) == c_clean or aliases.get(c_clean) == t_clean:
        return True
    for k, v in aliases.items():
        if (k in t_clean and v in c_clean) or (v in t_clean and k in c_clean):
            return True

    t_tokens = get_tokens(target_name)
    c_tokens = get_tokens(cand_name)

    if not t_tokens or not c_tokens:
        return False

    # Check direct containment or subset
    if t_tokens.issubset(c_tokens) or c_tokens.issubset(t_tokens):
        return True

    # Check intersection overlap
    common = t_tokens.intersection(c_tokens)
    if len(common) >= 2 or (len(t_tokens) == 1 and len(common) == 1):
        return True

    # Fallback fuzzy match
    score = fuzz.token_set_ratio(t_clean, c_clean)
    return score >= 72

def scrape_live_sources() -> List[Dict]:
    urls = [
        "https://www.investorgain.com/report/live-ipo-gmp/331/all/",
        "https://www.investorgain.com/report/live-ipo-gmp/331/",
        "https://www.investorgain.com/report/ipo-subscription-live/333/"
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/"
    }

    records_by_name = {}

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                print(f"[WARN] Failed fetching {url}: HTTP {res.status_code}")
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if not rows:
                    continue

                for row in rows[1:]:
                    cells = row.find_all(["td", "th"])
                    if len(cells) < 3:
                        continue

                    first_cell = cells[0]
                    clean_name = extract_clean_name(first_cell)
                    raw_text = row.get_text(separator=" ", strip=True)

                    if not clean_name or any(h in clean_name.lower() for h in ["issuer", "company", "ipo name", "closing date", "fire rating"]):
                        continue

                    gmp_amt = None
                    gmp_pct = None

                    # 1. Direct regex on entire row for explicit "GMP: ₹X (Y%)" or "GMP: --"
                    amt_match = re.search(r"GMP\s*:\s*(?:₹|\+)?\s*([0-9]+(?:\.[0-9]+)?)", raw_text, re.IGNORECASE)
                    pct_match = re.search(r"GMP:.*?\((\d+(?:\.\d+)?)\%\)", raw_text, re.IGNORECASE)

                    if amt_match:
                        gmp_amt = float(amt_match.group(1))
                        gmp_pct = float(pct_match.group(1)) if pct_match else 0.0
                    else:
                        # 2. Inspect cells 1 to 4 where InvestorGain places GMP and Sub figures
                        for cell in cells[1:4]:
                            c_text = cell.get_text(strip=True)
                            
                            # Skip empty or dashed indicators
                            if c_text in ["--", "-", "N/A", ""]:
                                continue

                            # Detect currency symbols or explicit numerical GMP values
                            m = re.search(r"(?:₹|\+|\b)\s*([\d,]+(?:\.\d+)?)\s*(?:\([+-]?\d+(?:\.\d+)?%\))?", c_text)
                            p_m = re.search(r"\(([+-]?[\d,]+(?:\.\d+)?)\s*%\)", c_text)
                            
                            if m:
                                val_clean = m.group(1).replace(",", "")
                                try:
                                    parsed_val = float(val_clean)
                                    # Guard against parsing price bands/dates as GMP
                                    if parsed_val > 0 and parsed_val < 5000:
                                        gmp_amt = parsed_val
                                        gmp_pct = float(p_m.group(1).replace(",", "")) if p_m else 0.0
                                        break
                                except ValueError:
                                    pass

                    if gmp_amt is not None and gmp_amt > 0:
                        key = clean_company_name(clean_name)
                        if key:
                            records_by_name[key] = {
                                "name": clean_name,
                                "gmp": gmp_amt,
                                "percent": gmp_pct
                            }
        except Exception as e:
            print(f"[WARN] Error fetching {url}: {e}")

    results = list(records_by_name.values())
    print(f"[GMP Scraper] Extracted {len(results)} live real-time GMP records.")
    for r in results:
        print(f"   -> Found: '{r['name']}' = ₹{r['gmp']} ({r['percent']}%)")
    return results

def sync_gmp():
    scraped_candidates = scrape_live_sources()

    manual_overrides = {}
    aliases = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                raw_overrides = json.load(f)
                aliases = raw_overrides.get("aliases", {})
                manual_overrides = {k: v for k, v in raw_overrides.items() if k != "aliases"}
        except Exception as e:
            print(f"[WARN] Failed to load overrides: {e}")

    gmp_store = {}
    if os.path.exists(GMP_DATA_PATH):
        try:
            with open(GMP_DATA_PATH, "r", encoding="utf-8") as f:
                gmp_store = json.load(f)
        except Exception:
            pass

    today_str = datetime.now(IST).strftime("%d %b")

    if not os.path.exists(IPOS_JSON_PATH):
        print(f"[ERROR] {IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    is_dict = isinstance(ipos_data, dict)
    items = ipos_data.get("ipos", ipos_data.get("data", [])) if is_dict else ipos_data

    updated_count = 0
    for item in items:
        raw_name = item.get("name", "")
        clean_key = clean_company_name(raw_name)

        final_gmp = None
        final_pct = None

        # Priority 1: Manual overrides
        override_entry = manual_overrides.get(raw_name.lower()) or manual_overrides.get(raw_name)
        if override_entry and "gmp" in override_entry:
            final_gmp = float(override_entry["gmp"])

        # Priority 2: Scraped match via token & alias overlap
        if final_gmp is None:
            for cand in scraped_candidates:
                if matches_target(raw_name, cand["name"], aliases):
                    final_gmp = cand["gmp"]
                    final_pct = cand["percent"]
                    break

        # Priority 3: Fallback to persistent storage
        if final_gmp is None and clean_key in gmp_store:
            saved = gmp_store[clean_key]
            final_gmp = float(saved.get("gmp", 0.0) if isinstance(saved, dict) else saved)

        if final_gmp is None:
            continue

        p_max = float(item.get("issuePriceMax", 0.0))
        if final_pct is None or final_pct == 0.0:
            final_pct = round((final_gmp / p_max * 100.0), 2) if (p_max > 0 and final_gmp > 0) else 0.0

        item["gmpAmount"] = final_gmp
        item["gmpPercent"] = final_pct

        # Incremental history append
        history = item.get("gmpHistory", [])
        if not isinstance(history, list):
            history = []

        if history and history[-1].get("dateLabel") == today_str:
            history[-1]["amount"] = final_gmp
        else:
            history.append({
                "dateLabel": today_str,
                "amount": final_gmp
            })

        # Retain last 7 tracking captures
        item["gmpHistory"] = history[-7:]

        gmp_store[clean_key] = {
            "name": raw_name,
            "gmp": final_gmp,
            "percent": final_pct
        }
        updated_count += 1
        print(f"[MATCHED GMP] {raw_name} -> ₹{final_gmp} ({final_pct}%)")

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    with open(GMP_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(gmp_store, f, indent=2, ensure_ascii=False)

    print(f"[SUCCESS] Updated GMP for {updated_count}/{len(items)} IPOs in {IPOS_JSON_PATH}")

if __name__ == "__main__":
    sync_gmp()
                
