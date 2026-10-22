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

# Strict blacklist to eliminate website links, registrars, table terms, and prospectus terminology
REGISTRAR_AND_NOISE_TOKENS: Set[str] = {
    "visit website", "website", "performance", "managed", "report", 
    "track record", "issues", "listing gain", "market maker", "registrar",
    "lead manager", "click here", "view", "profile", "contact",
    "link intime", "mufg intime", "kfin tech", "kfintech", "bigshare", 
    "cameo", "maashitla", "skyline", "purva sharegistry",
    # Prospectus, table header, and financial metric terms:
    "equity shares", "lot size", "drhp", "rhp", "prospectus", "anchor",
    "shareholding", "face value", "shares offered", "ipo dates", "issue type",
    "retail portion", "qib portion", "nii portion", "basis of allotment",
    "listing at", "share holding pre", "share holding post", "price band"
}

def clean_company_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower())
    tokens = [t for t in cleaned.split() if t not in NOISE_WORDS]
    return " ".join(tokens) if tokens else cleaned

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

def match_ipo_url(target_name: str, target_symbol: str, index: List[Tuple[str, str]]) -> Optional[str]:
    t_clean = clean_company_name(target_name).lower()
    t_tokens = clean_tokens(target_name)
    target_symbol = target_symbol.strip().lower() if target_symbol else ""
    best_url = None
    highest_score = 0

    for title, url in index:
        cand_lower = title.lower()
        cand_tokens = clean_tokens(title)

        # 1. Direct entity triggers (e.g., handles Jio -> Reliance Jio Infocomm / Jio Platforms)
        if "jio" in t_tokens and "jio" in cand_tokens:
            return url

        # 2. Match by exchange ticker symbol
        if target_symbol and len(target_symbol) >= 3 and target_symbol in cand_lower:
            return url

        # 3. Token overlap check
        if t_tokens and cand_tokens and not t_tokens.intersection(cand_tokens):
            continue

        score = fuzz.token_set_ratio(t_clean, cand_lower)
        if score > highest_score and score >= 68:
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
            if "pre" in k and "holding" in k and details["promoterHoldingPre"] is None:
                m = re.search(r"([\d.]+)\s*%", v)
                if m:
                    details["promoterHoldingPre"] = clean_float(m.group(1))

            if "post" in k and "holding" in k and details["promoterHoldingPost"] is None:
                m = re.search(r"([\d.]+)\s*%", v)
                if m:
                    details["promoterHoldingPost"] = clean_float(m.group(1))

    # 2. Parse Lead Managers (BRLMs) with strict multi-layer filtering
    for tag in soup.find_all(["div", "td", "li", "p"]):
        txt = tag.get_text(separator=" ", strip=True)
        if "lead manager" in txt.lower() and len(txt) < 500:
            for a in tag.find_all("a"):
                candidate = a.get_text(strip=True)
                clean_cand = re.sub(r"\s+", " ", candidate).strip()
                cand_lower = clean_cand.lower()

                # Filter out UI links, registrars, and web navigational text
                is_noise = any(noise in cand_lower for noise in REGISTRAR_AND_NOISE_TOKENS)
                if (
                    len(clean_cand) > 3
                    and not is_noise
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
        # 4. Extract Issuer Company Logo (Targeted with noise exclusion)
    details["logoUrl"] = None
    
    # Chittorgarh portal & site logo assets to exclude
    SITE_ASSET_BLACKLIST = {
        "chittorgarh", "chit_logo", "site_logo", "logo.svg", "logo.png", 
        "header", "icon", "broker", "sponsor", "google", "facebook", "twitter"
    }

    # Strategy A: Target specific logo containers Chittorgarh uses in page headers
    logo_containers = soup.select(".ipo-logo img, .company-logo img, .summary-logo img, .logo-container img")
    for img in logo_containers:
        src = img.get("src") or img.get("data-src")
        if src and not any(blacklisted in src.lower() for blacklisted in SITE_ASSET_BLACKLIST):
            details["logoUrl"] = src if src.startswith("http") else f"https://www.chittorgarh.com{src}"
            break

    # Strategy B: Fallback scan across all page images with strict path matching
    if not details["logoUrl"]:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            src_lower = src.lower()
            
            # Skip common Chittorgarh brand assets
            if any(blacklisted in src_lower for blacklisted in SITE_ASSET_BLACKLIST):
                continue
                
            # Chittorgarh company logo path patterns (e.g. /company_logo/, /upload/company/)
            if any(pattern in src_lower for pattern in ["/company_logo/", "/company-logo/", "/company/", "/upload/"]):
                full_logo = src if src.startswith("http") else f"https://www.chittorgarh.com{src}"
                details["logoUrl"] = full_logo
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
        symbol = item.get("symbol", "")

        url = match_ipo_url(name, symbol, index)
        if not url:
            search_slug = re.sub(r"[^a-zA-Z0-9\s]", "", name).lower().strip().replace(" ", "-")
            url = f"https://www.chittorgarh.com/ipo/{search_slug}-ipo/"

        try:
            res = requests.get(url, headers=HEADERS, timeout=12)
            if res.status_code == 200:
                parsed = parse_page_details(BeautifulSoup(res.text, "html.parser"))
                
                # Always assign the parsed structure so keys remain defined in ipos.json
                item["issueStructure"] = {
                    "freshIssueCr": parsed["freshIssueCr"],
                    "ofsCr": parsed["ofsCr"],
                    "promoterHoldingPre": parsed["promoterHoldingPre"],
                    "promoterHoldingPost": parsed["promoterHoldingPost"]
                }
                item["leadManagers"] = parsed["leadManagers"]
                item["anchorPortionCr"] = parsed["anchorPortionCr"]
                item["logoUrl"] = parsed.get("logoUrl") or item.get("logoUrl")
                
                updated += 1
                print(f"[{updated}] Synced {name}: Fresh=₹{parsed['freshIssueCr']}Cr, OFS=₹{parsed['ofsCr']}Cr, BRLMs={len(parsed['leadManagers'])}")
        except Exception as e:
            print(f"[WARN] Error parsing {name}: {e}")

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"[Done] Cleaned and enriched issuer details for {updated} IPOs.")

if __name__ == "__main__":
    sync_issuer_details()
                    
