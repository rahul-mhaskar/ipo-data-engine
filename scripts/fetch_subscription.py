import os
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase via GitHub Secret
cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if cred_json:
    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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

def update_local_json(sub_data, now_str):
    # Locate ipos.json across common locations in the repo
    possible_paths = [
        "ipos.json",
        "data/ipos.json",
        "app/src/main/assets/ipos.json",
        "scripts/ipos.json"
    ]
    target_path = next((p for p in possible_paths if os.path.exists(p)), None)

    if not target_path:
        print("ipos.json was not found in common locations. Creating it at root: ipos.json")
        target_path = "ipos.json"
        ipos_list = []
    else:
        with open(target_path, "r", encoding="utf-8") as f:
            try:
                ipos_list = json.load(f)
            except json.JSONDecodeError:
                ipos_list = []

    # If ipos.json is a dict with an "ipos" or "data" key, handle both formats
    is_dict = isinstance(ipos_list, dict)
    items = ipos_list.get("ipos", ipos_list.get("data", [])) if is_dict else ipos_list

    updated = 0
    for item in items:
        name = (item.get("name") or item.get("companyName") or "").lower()
        symbol = (item.get("symbol") or "").lower()

        matched = None
        for sub in sub_data:
            s_name = sub["name"].lower()
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
            updated += 1

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(ipos_list, f, indent=2, ensure_ascii=False)

    print(f"Successfully updated {updated} entries in {target_path}")

def sync_all():
    sub_data = fetch_from_investorgain()
    if not sub_data:
        print("No subscription records found.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # 1. Update Firestore
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
        print(f"Firestore synced with {len(sub_data)} records.")

    # 2. Update local JSON file
    update_local_json(sub_data, now_str)

if __name__ == "__main__":
    sync_all()
    
