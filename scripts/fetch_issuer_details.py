import os
import json
import re
from typing import Dict, List, Optional, Set, Tuple
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

NOISE_WORDS: Set[str] = {
    "limited", "ltd", "pvt", "private", "ipo", "india", "sme", 
    "technologies", "tech", "solutions", "industries", "services", 
    "infra", "infrastructure", "finance", "holdings", "group"
}

def clean_tokens(name: str) -> Set[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    return {t for t in cleaned.split() if t not in NOISE_WORDS}

def clean_float(val_str: str) -> float:
    if not val_str:
        return 0.0
    clean = re.sub(r"[^\d.-]", "", val_str.replace(",", ""))
    try:
        return round(float(clean), 2)
    except Exception:
        return 0.0

def build_chittorgarh_index() -> List[Tuple[str, str]]:
    """Crawls Chittorgarh master tracker to build a real-time name -> detail page map."""
    master_urls = [
        "https://www.chittorgarh.com/ipo/ipo_dashboard.asp",
        "https://www.chittorgarh.com/report/mainboard-ipo-list-in-india-bse-nse/82/",
        "https://www.chittorgarh.com/report/sme-ipo-list-in-india-bse-sme-nse-emerge/84/"
    ]
    ipo_links = []
    seen_urls = set()

    for url in master_urls:
        try:
            res = requests.get(url, headers=HEADERS, timeout=12)
            if res.status_code != 200:
                continue
            soup = BeautifulSoup(res.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/ipo/" in href and href.endswith("-ipo/"):
                    full_url = href if href.startswith("http") else f"https://www.chittorgarh.com{href}"
                    link_title = a.get_text(strip=True)
                    if full_url not in seen_urls and len(link_title) > 2:
                        seen_urls.add(full_url)
                        ipo_links.append((link_title, full_url))
        except Exception as e:
            print(f"[WARN] Failed indexing {url}: {e}")

    print(f"[Issuer Indexer] Indexed {len(ipo_links)} live IPO detail targets.")
    return ipo_links

def match_ipo_url(target_name: str, index: List[Tuple[str, str]]) -> Optional[str]:
    target_tokens = clean_tokens(target_name)
    best_url = None
    highest_score = 0

    for title, url in index:
        cand_tokens = clean_tokens(title)
        if target_tokens and cand_tokens and not target_tokens.intersection(cand_tokens):
            continue

        score = fuzz.token_set_ratio(target_name.lower(), title.lower())
        if score > highest_score and score >= 80:
            highest_score = score
            best_url = url

    return best_url

def parse_page_details(soup: BeautifulSoup) -> Dict:
    details = {
        "freshIssueCr": 0.0,
        "ofsCr": 0.0,
        "promoterHoldingPre": None,
        "promoterHoldingPost": None,
        "leadManagers": [],
        "anchorPortionCr": 0.0
    }

    # 1. Parse Issue Structure & Promoter Dilution
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            k = cells[0].lower()
            v = " ".join(cells[1:])

            # Fresh Issue
            if "fresh issue" in k and details["freshIssueCr"] == 0.0:
                m = re.search(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)", v, re.IGNORECASE)
                if m:
                    details["freshIssueCr"] = clean_float(m.group(1))

            # OFS
            if ("offer for sale" in k or "ofs" in k) and details["ofsCr"] == 0.0:
                m = re.search(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)", v, re.IGNORECASE)
                if m:
                    details["ofsCr"] = clean_float(m.group(1))

            # Pre/Post Shareholding
            if "pre" in k and "holding" in k:
                m = re.search(r"([\d.]+)\s*%", v)
                if m:
                    details["promoterHoldingPre"] = clean_float(m.group(1))

            if "post" in k and "holding" in k:
                m = re.search(r"([\d.]+)\s*%", v)
                if m:
                    details["promoterHoldingPost"] = clean_float(m.group(1))

    # 2. Parse Lead Managers (BRLMs)
    BRLM_NOISE = {
        "performance", "managed", "report", "track record", 
        "issues", "listing gain", "market maker", "registrar",
        "lead manager", "click here", "view", "profile", "contact"
    }

    for tag in soup.find_all(["div", "td", "li", "p"]):
        txt = tag.get_text(separator=" ", strip=True)
        if "lead manager" in txt.lower() and len(txt) < 500:
            for a in tag.find_all("a"):
                candidate = a.get_text(strip=True)
                clean_cand = re.sub(r"\s+", " ", candidate).strip()
                if (
                    len(clean_cand) > 3
                    and not any(n in clean_cand.lower() for n in BRLM_NOISE)
                    and clean_cand not in details["leadManagers"]
                ):
                    details["leadManagers"].append(clean_cand)

    # 3. Parse Anchor Portion
    for table in soup.find_all("table"):
        txt = table.get_text(separator=" ", strip=True).lower()
        if "anchor" in txt:
            for row in table.find_all("tr"):
                row_txt = row.get_text(separator=" ", strip=True)
                if "anchor" in row_txt.lower() and ("cr" in row_txt.lower() or "crore" in row_txt.lower()):
                    m = re.search(r"(?:₹|rs\.?)?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)", row_txt, re.IGNORECASE)
                    if m:
                        details["anchorPortionCr"] = clean_float(m.group(1))
                        break

    return details

def sync_issuer_details():
    if not os.path.exists(IPOS_JSON_PATH):
        print(f"[ERROR] {IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    items = ipos_data.get("ipos", ipos_data.get("data", [])) if isinstance(ipos_data, dict) else ipos_data
    index = build_chittorgarh_index()
    updated = 0

    for item in items:
        status = str(item.get("status", "")).upper()
        if status not in ["OPEN", "UPCOMING", "CLOSED"]:
            continue

        name = item.get("name", "")

        # Target matched live page
        url = match_ipo_url(name, index)
        if not url:
            # Fallback direct search
            search_slug = re.sub(r"[^a-zA-Z0-9\s]", "", name).lower().strip().replace(" ", "-")
            url = f"https://www.chittorgarh.com/ipo/{search_slug}-ipo/"

        try:
            res = requests.get(url, headers=HEADERS, timeout=12)
            if res.status_code == 200:
                parsed = parse_page_details(BeautifulSoup(res.text, "html.parser"))
                
                # Check if valid issuer details were discovered
                if parsed["freshIssueCr"] > 0 or parsed["ofsCr"] > 0 or parsed["leadManagers"]:
                    item["issueStructure"] = {
                        "freshIssueCr": parsed["freshIssueCr"],
                        "ofsCr": parsed["ofsCr"],
                        "promoterHoldingPre": parsed["promoterHoldingPre"],
                        "promoterHoldingPost": parsed["promoterHoldingPost"]
                    }
                    item["leadManagers"] = parsed["leadManagers"]
                    item["anchorPortionCr"] = parsed["anchorPortionCr"]
                    updated += 1
                    print(f"[{updated}] Enriched {name} from {url}: Fresh=₹{parsed['freshIssueCr']}Cr, OFS=₹{parsed['ofsCr']}Cr, BRLMs={len(parsed['leadManagers'])}")
        except Exception as e:
            print(f"[WARN] Error parsing {name}: {e}")

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"[Done] Automatically enriched issuer details for {updated} IPOs.")

if __name__ == "__main__":
    sync_issuer_details()
    
