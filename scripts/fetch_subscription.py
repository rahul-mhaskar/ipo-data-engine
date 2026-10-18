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
    """Converts strings like '12.45x', '12.45', or '-' to float."""
    if not val_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(val_str).strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def fetch_from_investorgain():
    """Fetches live subscription figures from InvestorGain's server-rendered table."""
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
            # Looking for rows with issuer name, QIB, NII, Retail, Total
            if len(cols) >= 5:
                first_col = cols[0]
                if any(header_word in first_col.lower() for header_word in ["issuer", "company", "ipo name"]):
                    continue
                
                # Check for numerical subscription data across row cells
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

def fetch_from_chittorgarh_api():
    """Queries the internal JSON table data endpoint used by Chittorgarh."""
    url = "https://webnodejs.chittorgarh.com/report/data-read/21"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.chittorgarh.com/"
    }
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            items = data.get("reportData") or data.get("data") or []
            results = []
            for item in items:
                name = item.get("Company_Name") or item.get("name") or item.get("Issuer_Company") or ""
                if name:
                    results.append({
                        "name": name,
                        "qib": clean_multiplier(item.get("QIB")),
                        "nii": clean_multiplier(item.get("NII")),
                        "retail": clean_multiplier(item.get("RII") or item.get("Retail")),
                        "total": clean_multiplier(item.get("Total"))
                    })
            if results:
                return results
    except Exception as e:
        print(f"Chittorgarh internal API call bypassed: {e}")
    return []

def sync_to_firestore():
    # Attempt primary JSON API first, then fall back to server-rendered HTML table
    sub_data = fetch_from_chittorgarh_api()
    if not sub_data:
        print("API endpoint unavailable, querying server-rendered table...")
        sub_data = fetch_from_investorgain()

    if not sub_data:
        print("No subscription records could be retrieved from either source.")
        return

    print(f"Retrieved {len(sub_data)} live subscription records.")

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
            # Match against company name or ticker/symbol
            if (doc_name and (doc_name in item_name or item_name in doc_name)) or \
               (doc_symbol and doc_symbol in item_name):
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
            print(f"Updated subscription for {doc.id} ({matched['name']}) -> Total: {sub_payload['total']}x")
            updated_count += 1

    print(f"Sync complete. Updated {updated_count} active IPO records.")

if __name__ == "__main__":
    sync_to_firestore()
    
