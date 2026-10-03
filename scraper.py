import json
import os
import requests
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

FILE_PATH = "ipos.json"

def load_current_data():
    if os.path.exists(FILE_PATH):
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []

def update_ipo_lifecycle(ipo_list):
    """Automatically marks status based on current calendar date."""
    today = datetime.now().date()
    for ipo in ipo_list:
        try:
            o_date = datetime.strptime(ipo.get("openDate", ""), "%Y-%m-%d").date()
            c_date = datetime.strptime(ipo.get("closeDate", ""), "%Y-%m-%d").date()
            l_date = datetime.strptime(ipo.get("listingDate", ""), "%Y-%m-%d").date()

            if today < o_date:
                ipo["status"] = "UPCOMING"
            elif o_date <= today <= c_date:
                ipo["status"] = "OPEN"
            elif c_date < today < l_date:
                ipo["status"] = "CLOSED"
            else:
                ipo["status"] = "LISTED"
        except Exception:
            continue
    return ipo_list

def save_data(ipo_list):
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(ipo_list, f, indent=2, ensure_ascii=False)
    print(f"Successfully processed {len(ipo_list)} IPO items.")

if __name__ == "__main__":
    data = load_current_data()
    updated_data = update_ipo_lifecycle(data)
    save_data(updated_data)
  
