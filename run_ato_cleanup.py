import json
import logging
import os
import requests
import pyotp

logging.basicConfig(level=logging.INFO)

# ==========================================
# SECURE CONFIGURATION FROM ENVIRONMENT
# ==========================================
API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
USER_ID = os.environ.get("ZERODHA_USER_ID")
PASSWORD = os.environ.get("ZERODHA_PASSWORD")
TOTP_KEY = os.environ.get("ZERODHA_TOTP_KEY")

def execute_automated_login_and_cleanup():
    if not all([API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_KEY]):
        print("Error: One or more required environment secrets are missing.")
        return

    print("Initiating daily automated session authentication...")
    session = requests.Session()
    
    try:
        # --- FIXED STEP: Safe parameter formatting to prevent URL character leaks ---
        login_url = "https://kite.trade"
        login_params = {
            "api_key": API_KEY,
            "v": "3"
        }
        
        # Hit the base page with clean dictionary params
        session.get(login_url, params=login_params)
        
        # Step 2: Submit password credentials
        payload_phase1 = {"user_id": USER_ID, "password": PASSWORD}
        response_phase1 = session.post("https://zerodha.com", data=payload_phase1)
        
        if response_phase1.status_code != 200:
            print(f"Password verification failed: {response_phase1.text}")
            return
            
        request_id = response_phase1.json().get("data", {}).get("request_id")
        
        # Step 3: Compute the live 2FA token sequence 
        totp_generator = pyotp.TOTP(TOTP_KEY.replace(" ", ""))
        live_otp = totp_generator.now()
        print("Generated dynamic 2FA passcode successfully.")
        
        # Step 4: Submit OTP validation request
        payload_phase2 = {
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": live_otp,
            "twofa_type": "totp"
        }
        response_phase2 = session.post("https://zerodha.com", data=payload_phase2)
        
        if response_phase2.status_code != 200:
            print(f"2FA verification rejected: {response_phase2.text}")
            return

        # Step 5: Follow login sequence to grab target request token redirect
        response_redirect = session.get(login_url, params=login_params, allow_redirects=True)
        final_destination_url = response_redirect.url
        
        if "request_token=" not in final_destination_url:
            print(f"Failed to find request token redirect parameter. Destination was: {final_destination_url}")
            return
            
        # Extract out token value strings cleanly
        request_token = final_destination_url.split("request_token=")[1].split("&")[0]
        print("Valid daily request token acquired!")

        # Step 6: Finalize session integration via official KiteConnect SDK tools
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=API_KEY)
        session_profile = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = session_profile["access_token"]
        print("Live access token successfully activated. Scanning active stream alerts...")

        # Step 7: Iterate and wipe out matching targets
        headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {API_KEY}:{access_token}"
        }
        
        alerts_response = requests.get("https://kite.trade", headers=headers)
        alerts_list = alerts_response.json().get("data", [])
        
        print(f"Discovered {len(alerts_list)} total alerts on profile.")
        
        deleted_count = 0
        for alert in alerts_list:
            alert_type = str(alert.get("type", "")).strip().lower()
            
            if alert_type == "ato":
                uuid = alert.get("uuid")
                name = alert.get("name", "ATO Alert")
                print(f"Wiping out ATO alert: '{name}' | UUID: {uuid}")
                
                del_url = f"https://kite.trade?uuid={uuid}"
                del_resp = requests.delete(del_url, headers=headers)
                
                if del_resp.status_code == 200:
                    deleted_count += 1
                    print("  -> Successfully removed.")
                else:
                    print(f"  -> Deletion request failed: {del_resp.text}")
                    
        print(f"Automated cleanup finished. Wiped {deleted_count} active ATO entries.")

    except Exception as e:
        print(f"The automated 10:00 AM routine encountered a code error: {e}")

if __name__ == "__main__":
    execute_automated_login_and_cleanup()
    
