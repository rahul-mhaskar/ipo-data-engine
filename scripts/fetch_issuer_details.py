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

def clean_float(val_str: str) -> float:
    if not val_str:
        return 0.0
    clean = re.sub(r"[^\d.-]", "", val_str)
    try:
        return round(float(clean), 2)
    except Exception:
        return 0.0

def resolve_chittorgarh_url(company_name: str) -> Optional[str]:
    search_slug = re.sub(r"[^a-zA-Z0-9\s]", "", company_name).lower().strip().replace(" ", "-")
    direct_candidates = [
        f"https://www.chittorgarh.com/ipo/{search_slug}-ipo/",
        f"https://www.chittorgarh.com/ipo/{search_slug.replace('-ltd', '')}-ipo/",
        f"https://www.chittorgarh.com/ipo/{clean_company_name(company_name).replace(' ', '-')}-ipo/"
    ]
    for cand_url in direct_candidates:
        try:
            r = requests.get(cand_url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code == 200 and ("Issue Type" in r.text or "Lead Manager" in r.text):
                return cand_url
        except Exception:
            continue

    try:
        q_url = f"https://www.chittorgarh.com/search/?search={requests.utils.quote(clean_company_name(company_name))}"
        r = requests.get(q_url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/ipo/" in href and "-ipo/" in href:
                    return href if href.startswith("http") else f"https://www.chittorgarh.com{href}"
    except Exception:
        pass
    return None

def extract_issuer_details(soup: BeautifulSoup) -> Dict:
    details = {
        "freshIssueCr": 0.0,
        "ofsCr": 0.0,
        "promoterHoldingPre": None,
        "promoterHoldingPost": None,
        "leadManagers": [],
        "anchorPortionCr": 0.0
    }

    # 1. Parse Issue Structure (Fresh vs OFS) and Promoter Holdings
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            k = cells[0].lower()
            v = cells[1]

            if "fresh issue" in k and details["freshIssueCr"] == 0.0:
                match_cr = re.search(r"₹?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)", v, re.IGNORECASE)
                if match_cr:
                    details["freshIssueCr"] = clean_float(match_cr.group(1))

            if "offer for sale" in k and details["ofsCr"] == 0.0:
                match_cr = re.search(r"₹?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)", v, re.IGNORECASE)
                if match_cr:
                    details["ofsCr"] = clean_float(match_cr.group(1))

            if "pre issue" in k and "share holding" in k:
                pct_match = re.search(r"([\d.]+)\s*%", v)
                if pct_match:
                    details["promoterHoldingPre"] = float(pct_match.group(1))

            if "post issue" in k and "share holding" in k:
                pct_match = re.search(r"([\d.]+)\s*%", v)
                if pct_match:
                    details["promoterHoldingPost"] = float(pct_match.group(1))

    # 2. Parse Lead Managers (BRLMs)
    for tag in soup.find_all(["div", "p", "td", "li"]):
        txt = tag.get_text(separator=" ", strip=True)
        if "lead manager" in txt.lower() and len(txt) < 300:
            # Often styled as list items or comma-separated names
            links = tag.find_all("a")
            if links:
                for a in links:
                    name = a.get_text(strip=True)
                    if len(name) > 3 and "report" not in name.lower() and name not in details["leadManagers"]:
                        details["leadManagers"].append(name)

    # 3. Parse Anchor Portion
    for table in tables:
        txt = table.get_text(separator=" ", strip=True).lower()
        if "anchor" in txt and ("portion" in txt or "shares offered" in txt or "size" in txt):
            for row in table.find_all("tr"):
                row_txt = row.get_text(separator=" ", strip=True)
                if "anchor" in row_txt.lower():
                    match_cr = re.search(r"₹?\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)", row_txt, re.IGNORECASE)
                    if match_cr:
                        details["anchorPortionCr"] = clean_float(match_cr.group(1))
                        break

    return details

def sync_issuer_details():
    if not os.path.exists(IPOS_JSON_PATH):
        print(f"[ERROR] {IPOS_JSON_PATH} not found.")
        return

    with open(IPOS_JSON_PATH, "r", encoding="utf-8") as f:
        ipos_data = json.load(f)

    manual_overrides = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                manual_overrides = json.load(f)
        except Exception:
            pass

    items = ipos_data.get("ipos", ipos_data.get("data", [])) if isinstance(ipos_data, dict) else ipos_data
    updated = 0

    print(f"[Issuer Details Sync] Processing {len(items)} IPOs...")

    for item in items:
        status = str(item.get("status", "")).upper()
        if status not in ["OPEN", "UPCOMING", "CLOSED"]:
            continue

        name = item.get("name", "")
        norm_name = clean_company_name(name)

        # Check manual overrides
        override = manual_overrides.get(norm_name) or manual_overrides.get(name.lower())
        if override:
            if "issueStructure" in override:
                item["issueStructure"] = override["issueStructure"]
            if "leadManagers" in override:
                item["leadManagers"] = override["leadManagers"]
            if "anchorPortionCr" in override:
                item["anchorPortionCr"] = override["anchorPortionCr"]

        # If already populated with non-zero Fresh/OFS, keep cached
        if item.get("issueStructure") and (item["issueStructure"].get("freshIssueCr", 0) > 0 or item.get("leadManagers")):
            updated += 1
            continue

        url = resolve_chittorgarh_url(name)
        if url:
            try:
                res = requests.get(url, headers=HEADERS, timeout=12)
                if res.status_code == 200:
                    d = extract_issuer_details(BeautifulSoup(res.text, "html.parser"))
                    
                    item["issueStructure"] = {
                        "freshIssueCr": d["freshIssueCr"],
                        "ofsCr": d["ofsCr"],
                        "promoterHoldingPre": d["promoterHoldingPre"],
                        "promoterHoldingPost": d["promoterHoldingPost"]
                    }
                    item["leadManagers"] = d["leadManagers"]
                    item["anchorPortionCr"] = d["anchorPortionCr"]
                    updated += 1
                    print(f"[{updated}] Enriched: {name} (Fresh: ₹{d['freshIssueCr']}Cr | OFS: ₹{d['ofsCr']}Cr | BRLMs: {len(d['leadManagers'])})")
            except Exception as e:
                print(f"[WARN] Failed fetching {name}: {e}")

    with open(IPOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ipos_data, f, indent=2, ensure_ascii=False)

    print(f"[Done] Enriched issuer details for {updated} IPOs.")

if __name__ == "__main__":
    sync_issuer_details()
                              
