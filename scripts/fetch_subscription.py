import os
import json
import re
import requests
from bs4 import BeautifulSoup

def clean_multiplier(val_str):
    if not val_str:
        return 0.0
    # Extract decimal multiplier like "131.25" or "4.43x"
    match = re.search(r"(\d+(\.\d+)?)", str(val_str).replace(",", ""))
    return float(match.group(1)) if match else 0.0

def normalize(text):
    """Strips punctuation and suffixes like 'limited', 'ltd', 'ipo' for fuzzy matching."""
    t = text.lower().strip()
    t = re.sub(r"\b(limited|ltd|ipo|bse|nse|sme)\b", "", t)
    return re.sub(r"[^\w]", "", t)

def scrape_table(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
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

            # Identify header indexes
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

            # If this table doesn't have at least retail or total headers, skip
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

                    if tot > 0.0 or r > 0.0:
                        results.append({
                            "name": raw_name,
                            "norm_name": normalize(raw_name),
                            "qib": q,
                            "nii": n,
                            "retail": r,
                            "total": tot
                        })
        return results
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []

def fetch_all_sources():
    # Live bidding + Past closed status tables
    urls = [
        "https://www.investorgain.com/report/live-ipo-subscription-status/331/",
        "https://www.investorgain.com/report/ipo-subscription-status-live-bidding-data-bse-nse/333/"
    ]
    all_data = []
    seen = set()
    for url in urls:
        records = scrape_table(url)
        for r in records:
            if r["norm_name"] not in seen:
                seen.add(r["norm_name"])
                all_data.append(r)
    return all_data

def update_root_ipos_json():
    target_path = "ipos.json"
    if not os.path.exists(target_path):
        print("ipos.json not found at repository root.")
        return

    sub_data = fetch_all_sources()
    print(f"Scraped {len(sub_data)} valid subscription entries across sources.")

    with open(target_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    is_dict = isinstance(data, dict)
    items = data.get("ipos", data.get("data", [])) if is_dict else data

    matched_count = 0
    for item in items:
        # Strip redundant nested field if present
        item.pop("subscription", None)

        norm_name = normalize(item.get("name") or "")
        norm_sym = normalize(item.get("symbol") or "")

        matched = None
        for sub in sub_data:
            s_norm = sub["norm_name"]
            if (norm_name and (norm_name in s_norm or s_norm in norm_name)) or \
               (norm_sym and (norm_sym in s_norm or s_norm in norm_sym)):
                matched = sub
                break

        if matched:
            # Overwrite only if scraped values are greater than zero,
            # or if current values are empty/zero.
            if matched["total"] > 0:
                item["subscriptionTotal"] = matched["total"]
            if matched["qib"] > 0:
                item["subscriptionQIB"] = matched["qib"]
            if matched["nii"] > 0:
                item["subscriptionHNI"] = matched["nii"]
            if matched["retail"] > 0:
                item["subscriptionRetail"] = matched["retail"]
            matched_count += 1

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Updated subscriptions for {matched_count} IPOs in {target_path}")

if __name__ == "__main__":
    update_root_ipos_json()
    
