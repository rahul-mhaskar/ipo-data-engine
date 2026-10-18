import os
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if not cred_json:
    raise ValueError("Missing FIREBASE_SERVICE_ACCOUNT environment variable")

cred_dict = json.loads(cred_json)
cred = credentials.Certificate(cred_dict)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

def clean_multiplier(val_str):
    if not val_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(val_str).strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def slugify(text):
    """Creates a clean, consistent document ID from company name."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "_", text)

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

def sync_to_firestore():
    sub_data = fetch_from_investorgain()

    if not sub_data:
        print("No subscription records found.")
        return

    print(f"Retrieved {len(sub_data)} live subscription records.")

    ipos_ref = db.collection("ipos")
    existing_docs = {doc.id: doc.to_dict() for doc in ipos_ref.stream()}

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    written_count = 0

    for item in sub_data:
        sub_payload = {
            "qib": item["qib"],
            "nii": item["nii"],
            "retail": item["retail"],
            "total": item["total"],
            "updatedAt": now_str
        }

        # Check if matched in existing documents
        matched_doc_id = None
        for doc_id, data in existing_docs.items():
            name = (data.get("name") or data.get("companyName") or "").lower()
            if name and (name in item["name"].lower() or item["name"].lower() in name):
                matched_doc_id = doc_id
                break

        if matched_doc_id:
            ipos_ref.document(matched_doc_id).set({"subscription": sub_payload}, merge=True)
            print(f"Updated existing: {matched_doc_id} -> {sub_payload['total']}x")
        else:
            # Create document if it doesn't exist yet
            doc_id = slugify(item["name"])
            ipos_ref.document(doc_id).set({
                "name": item["name"],
                "subscription": sub_payload
            }, merge=True)
            print(f"Created new doc: {doc_id} -> {sub_payload['total']}x")

        written_count += 1

    print(f"Sync complete. Successfully populated/updated {written_count} IPO records in Firestore.")

if __name__ == "__main__":
    sync_to_firestore()
    
