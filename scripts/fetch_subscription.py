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
if not cred_json:
    raise ValueError("Missing FIREBASE_SERVICE_ACCOUNT environment variable")

cred_dict = json.loads(cred_json)
cred = credentials.Certificate(cred_dict)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

def clean_multiplier(val_str):
    """Converts raw string (e.g. '12.45x', '12.45', 'N/A') to float."""
    if not val_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", val_str.strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def fetch_chittorgarh_subscription():
    """Scrapes consolidated live subscription data from Chittorgarh public tracker."""
    url = "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/21/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code != 200:
        print(f"Failed to fetch subscription source. Status: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table", {"class": "table"})
    if not table:
        print("Table not found on source page")
        return []

    results = []
    rows = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")[1:]

    for row in rows:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        # Typical schema: [Issuer Name, Close Date, QIB, NII, Retail, Total, ...]
        if len(cols) >= 6:
            name = cols[0]
            qib = clean_multiplier(cols[2])
            nii = clean_multiplier(cols[3])
            retail = clean_multiplier(cols[4])
            total = clean_multiplier(cols[5])

            results.append({
                "name": name,
                "qib": qib,
                "nii": nii,
                "retail": retail,
                "total": total
            })
    return results

def sync_to_firestore():
    sub_data = fetch_chittorgarh_subscription()
    if not sub_data:
        print("No subscription records scraped.")
        return

    # Update active IPO documents
    ipos_ref = db.collection("ipos")
    docs = ipos_ref.stream()

    updated_count = 0
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    for doc in docs:
        ipo = doc.to_dict()
        doc_name = (ipo.get("name") or ipo.get("companyName") or "").lower().strip()
        doc_symbol = (ipo.get("symbol") or "").lower().strip()

        # Match scraped issuer name against document name or symbol
        matched = None
        for item in sub_data:
            item_name = item["name"].lower().strip()
            if (doc_name and doc_name in item_name) or (item_name in doc_name) or (doc_symbol and doc_symbol in item_name):
                matched = item
                break

        if matched:
            sub_payload = {
                "qib": matched["qib"],
                "nii": matched["nii"],
                "retail": matched["retail"],
                "total": matched["total"],
                "updatedAt": now_str
            }
            ipos_ref.document(doc.id).update({
                "subscription": sub_payload
            })
            print(f"Updated subscription for: {doc.id} ({matched['name']}) -> {sub_payload['total']}x")
            updated_count += 1

    print(f"Sync complete. Updated {updated_count} active IPO records.")

if __name__ == "__main__":
    sync_to_firestore()
      
