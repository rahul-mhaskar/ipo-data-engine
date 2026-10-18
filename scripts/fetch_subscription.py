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

def fetch_subscription_data():
    """Fetches consolidated subscription figures with browser session headers."""
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/"
    }

    url = "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/21/"
    response = session.get(url, headers=headers, timeout=20)
    
    if response.status_code != 200:
        print(f"Direct request failed with HTTP {response.status_code}. Checking alternative table endpoint...")
        # Fallback to secondary consolidated table endpoint
        url = "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/22/"
        response = session.get(url, headers=headers, timeout=20)

    soup = BeautifulSoup(response.text, "html.parser")
    tables = soup.find_all("table")
    
    target_table = None
    for tbl in tables:
        tbl_text = tbl.get_text()
        if "QIB" in tbl_text and ("NII" in tbl_text or "HNI" in tbl_text) and "Total" in tbl_text:
            target_table = tbl
            break

    if not target_table:
        print("Subscription table not found. Debug preview of page title:", soup.title.string if soup.title else "No title")
        return []

    results = []
    tbody = target_table.find("tbody")
    rows = tbody.find_all("tr") if tbody else target_table.find_all("tr")[1:]

    for row in rows:
        cols = [td.get_text(separator=" ", strip=True) for td in row.find_all(["td", "th"])]
        if len(cols) >= 5:
            # Common pattern: Company Name is first col; QIB, NII, Retail, Total are within subsequent columns
            name = cols[0].split("\n")[0].strip()
            
            # Find numerical values across cells
            numeric_cols = []
            for c in cols[1:]:
                m = clean_multiplier(c)
                numeric_cols.append(m)

            if len(numeric_cols) >= 4:
                # Typically QIB, NII, Retail, Total
                qib = numeric_cols[1] if len(numeric_cols) > 4 else numeric_cols[0]
                nii = numeric_cols[2] if len(numeric_cols) > 4 else numeric_cols[1]
                retail = numeric_cols[3] if len(numeric_cols) > 4 else numeric_cols[2]
                total = numeric_cols[-1]

                results.append({
                    "name": name,
                    "qib": qib,
                    "nii": nii,
                    "retail": retail,
                    "total": total
                })

    print(f"Successfully scraped {len(results)} IPO subscription records.")
    return results

def sync_to_firestore():
    sub_data = fetch_subscription_data()
    if not sub_data:
        print("No subscription records scraped.")
        return

    ipos_ref = db.collection("ipos")
    docs = ipos_ref.stream()

    updated_count = 0
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    for doc in docs:
        ipo = doc.to_dict()
        doc_name = (ipo.get("name") or ipo.get("companyName") or "").lower().strip()
        doc_symbol = (ipo.get("symbol") or "").lower().strip()

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
            print(f"Updated subscription for: {doc.id} ({matched['name']}) -> Total: {sub_payload['total']}x")
            updated_count += 1

    print(f"Sync complete. Updated {updated_count} active IPO records.")

if __name__ == "__main__":
    sync_to_firestore()
    
