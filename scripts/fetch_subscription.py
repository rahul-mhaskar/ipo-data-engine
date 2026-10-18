import os
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup

def clean_multiplier(val_str):
    if not val_str:
        return 0.0
    # Match decimal multiplier like "12.45" or "4.43x"
    match = re.search(r"(\d+(\.\d+)?)", str(val_str).replace(",", ""))
    return float(match.group(1)) if match else 0.0

def fetch_from_investorgain():
    url = "https://www.investorgain.com/report/live-ipo-subscription-status/331/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return []

        # Map header positions dynamically
        headers_map = {}
        header_row = table.find("tr")
        if header_row:
            for idx, th in enumerate(header_row.find_all(["th", "td"])):
                text = th.get_text(strip=True).upper()
                if "QIB" in text:
                    headers_map["qib"] = idx
                elif "NII" in text or "HNI" in text:
                    headers_map["nii"] = idx
                elif "RETAIL" in text or "RII" in text:
                    headers_map["retail"] = idx
                elif "TOTAL" in text:
                    headers_map["total"] = idx

        results = []
        rows = table.find_all("tr")[1:]
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cols) >= 5:
                name = cols[0]
                if any(h in name.lower() for h in ["issuer", "company", "ipo name"]):
                    continue

                qib = clean_multiplier(cols[headers_map["qib"]]) if "qib" in headers_map and headers_map["qib"] < len(cols) else 0.0
                nii = clean_multiplier(cols[headers_map["nii"]]) if "nii" in headers_map and headers_map["nii"] < len(cols) else 0.0
                retail = clean_multiplier(cols[headers_map["retail"]]) if "retail" in headers_map and headers_map["retail"] < len(cols) else 0.0
                total = clean_multiplier(cols[headers_map["total"]]) if "total" in headers_map and headers_map["total"] < len(cols) else 0.0

                results.append({
                    "name": name,
                    "qib": qib,
                    "nii": nii,
                    "retail": retail,
                    "total": total
                })
        return results
    except Exception as e:
        print(f"Error fetching from InvestorGain: {e}")
        return []

def update_root_ipos_json():
    target_path = "ipos.json"
    if not os.path.exists(target_path):
        print("ipos.json not found at repository root.")
        return

    sub_data = fetch_from_investorgain()
    if not sub_data:
        print("No subscription records scraped.")
        return

    with open(target_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    is_dict = isinstance(data, dict)
    items = data.get("ipos", data.get("data", [])) if is_dict else data

    matched_count = 0
    for item in items:
        # Remove any faulty nested subscription object if it was added
        item.pop("subscription", None)

        name = (item.get("name") or "").lower().strip()
        symbol = (item.get("symbol") or "").lower().strip()

        matched = None
        for sub in sub_data:
            s_name = sub["name"].lower().strip()
            if (name and (name in s_name or s_name in name)) or (symbol and symbol in s_name):
                matched = sub
                break

        if matched:
            # Overwrite the clean flat fields
            item["subscriptionQIB"] = matched["qib"]
            item["subscriptionHNI"] = matched["nii"]
            item["subscriptionRetail"] = matched["retail"]
            item["subscriptionTotal"] = matched["total"]
            matched_count += 1

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Cleaned up and synced flat subscription keys for {matched_count} IPOs.")

if __name__ == "__main__":
    update_root_ipos_json()
    
