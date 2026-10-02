import logging
import json
import requests
import pyotp
from kiteconnect import KiteConnect

# Enable clear tracking printouts
logging.basicConfig(level=logging.INFO)

# ==========================================
# CONFIGURATION & REPO INITIALIZATION
# ==========================================
import os
API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
USER_ID = os.environ.get("ZERODHA_USER_ID")
PASSWORD = os.environ.get("ZERODHA_PASSWORD")
TOTP_KEY = os.environ.get("ZERODHA_TOTP_KEY")

def fetch_and_cleanup_atos():
    print("Initiating daily session authentication sequence...")
    session = requests.Session()
    
    try:
        # ---- SAFE URL CONSTRUCTION PARSING ----
        base_login_url = "https://kite.trade"
        login_params = {
            "api_key": API_KEY,
            "v": "3"
        }
        
        # Hit the login gateway safely using dictionary arguments
        session.get(base_login_url, params=login_params)
        
        payload_phase1 = {"user_id": USER_ID, "password": PASSWORD}
        response_phase1 = session.post("https://zerodha.com", data=payload_phase1)
        request_id = response_phase1.json().get("data", {}).get("request_id")
        
        totp = pyotp.TOTP(TOTP_KEY.replace(" ", ""))
        current_otp = totp.now()
        
        payload_phase2 = {
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": current_otp,
            "twofa_type": "totp"
        }
        session.post("https://zerodha.com", data=payload_phase2)
        
        # Follow dynamic landing chains safely using parameter bindings
        response_redirect = session.get(base_login_url, params=login_params, allow_redirects=True)
        final_url = response_redirect.url
        request_token = final_url.split("request_token=")[1].split("&")[0]
        
        # Initialize official session using your preferred SDK framework
        kite = KiteConnect(api_key=API_KEY)
        session_profile = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = session_profile["access_token"]
        print("Session authenticated successfully! Daily Access Token generated.")
        
        # ---- CORRECT API ROUTING PACKETS ----
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {API_KEY}:{access_token}"
        }
        
        # Pointing to the actual live database endpoint, not the frontend site
        api_alerts_url = "https://kite.trade"
        response = requests.get(api_alerts_url, headers=headers)
        
        if response.status_code != 200:
            print(f"API Error fetching alerts: Data download returned status {response.status_code}")
            return

        payload = response.json()
        alerts_list = payload.get("data", []) if isinstance(payload, dict) else payload

        # Case-insensitive filtering loop to catch off-market 10:00 AM settings
        target_atos = []
        for alert in alerts_list:
            alert_type = str(alert.get("type", "")).strip().lower()
            if alert_type == "ato":
                target_atos.append(alert)

        if not target_atos:
            print("No active ATO baskets detected on this account scan.")
            return

        print(f"Identified {len(target_atos)} target ATO configuration(s) for cleanup.")
        
        # Delete active configurations using documentation routing parameters
        for ato in target_atos:
            uuid = ato.get("uuid")
            name = ato.get("name", "Unnamed ATO")
            print(f"Wiping out configuration: '{name}' | UUID: {uuid}")
            
            delete_url = f"https://kite.trade?uuid={uuid}"
            delete_response = requests.delete(delete_url, headers=headers)
            
            if delete_response.status_code == 200:
                print(f"  -> Success: Deleted {uuid}")
            else:
                print(f"  -> Failed: Code {delete_response.status_code} | Details: {delete_response.text}")

    except Exception as e:
        print(f"An unexpected system exception occurred inside cleanup runtime: {e}")

if __name__ == "__main__":
    fetch_and_cleanup_atos()
    
