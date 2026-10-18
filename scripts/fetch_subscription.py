import os
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase Admin SDK
cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if cred_json:
    try:
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
    except Exception as e:
        print(f"Firebase init error: {e}")
        db = None
else:
    db = None

def clean_multiplier(val_str):
    if not val_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(val_str).strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def fetch_from_investorgain():
    url = "https://www.investorgain.com/report/live-ipo-subscription-status/331/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            print(f"InvestorGain returned HTTP {res.status_code}")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.find("table")
        if not table:
            return []

        results = []
        rows = table.find_all("tr")
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cols) >= 5:
                first_col = cols[0]
                if any(h in first_col.lower() for h in ["issuer", "company", "ipo name"]):
                    continue
                numbers = [clean_multiplier(c) for c in cols if any(ch.isdigit() for ch in c)]
                if len(numbers) >= 3:
                    results.append({
                        "name": first_col,
                        "qib": numbers[0] if len(numbers) >= 4 else 0.0,
                        "nii": numbers[1] if len(numbers) >= 4 else numbers[0],
                        "retail": numbers[2] if len(numbers) >= 4 else numbers[1],
                        "total": numbers[-1]
                    })
        return results
    except Exception as e:
        print(f"Error fetching from InvestorGain: {e}")
        return []

def update_root_ipos_json(sub_data, now_str):
    target_path = "ipos.json"
    if not os.path.exists(target_path):
        print("ipos.json not found at repository root.")
        return

    with open(target_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as err:
            print(f"Error decoding ipos.json: {err}")
            return

    # Handle list of IPO objects or dict wrapping a list {"ipos": [...]}
    is_dict = isinstance(data, dict)
    items = data.get("ipos", data.get("data", [])) if is_dict else data

    matched_count = 0
    for item in items:
        name = (item.get("name") or item.get("companyName") or "").lower().strip()
        symbol = (item.get("symbol") or "").lower().strip()

        matched = None
        for sub in sub_data:
            s_name = sub["name"].lower().strip()
            if (name and (name in s_name or s_name in name)) or (symbol and symbol in s_name):
                matched = sub
                break

        if matched:
            item["subscription"] = {
                "qib": matched["qib"],
                "nii": matched["nii"],
                "retail": matched["retail"],
                "total": matched["total"],
                "updatedAt": now_str
            }
            matched_count += 1

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Updated subscription for {matched_count} IPOs directly in {target_path}")

def sync_all():
    sub_data = fetch_from_investorgain()
    if not sub_data:
        print("No subscription records scraped.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # 1. Update root ipos.json
    update_root_ipos_json(sub_data, now_str)

    # 2. Update Firestore if connected
    if db:
        ipos_ref = db.collection("ipos")
        for item in sub_data:
            slug = re.sub(r"[-\s]+", "_", re.sub(r"[^\w\s-]", "", item["name"].lower().strip()))
            ipos_ref.document(slug).set({
                "name": item["name"],
                "subscription": {
                    "qib": item["qib"],
                    "nii": item["nii"],
                    "retail": item["retail"],
                    "total": item["total"],
                    "updatedAt": now_str
                }
            }, merge=True)
        print("Firestore updated in parallel.")

if __name__ == "__main__":
    sync_all()
    
