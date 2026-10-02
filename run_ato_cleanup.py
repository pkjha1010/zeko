import json
import logging
import os
import requests
import pyotp  # Generates the 6-digit login token dynamically

logging.basicConfig(level=logging.INFO)

# Retrieve variables from your GitHub repository settings
API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
USER_ID = os.environ.get("ZERODHA_USER_ID")
PASSWORD = os.environ.get("ZERODHA_PASSWORD")
TOTP_KEY = os.environ.get("ZERODHA_TOTP_KEY")  # Uses the secret seed you already added

def execute_automated_login_and_cleanup():
    if not all([API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_KEY]):
        print("Error: One or more required environment secrets are missing.")
        return

    print("Initiating daily automated session authentication...")
    session = requests.Session()
    
    try:
        # Step 1: Hit login entry page to grab active cookies
        login_url = f"https://kite.trade{API_KEY}&v=3"
        session.get(login_url)
        
        # Step 2: Submit your encrypted password credentials
        payload_phase1 = {"user_id": USER_ID, "password": PASSWORD}
        response_phase1 = session.post("https://zerodha.com", data=payload_phase1)
        
        if response_phase1.status_code != 200:
            print(f"Password verification failed: {response_phase1.text}")
            return
            
        request_id = response_phase1.json().get("data", {}).get("request_id")
        
        # Step 3: Read your secret seed token and compute the live 6-digit passcode
        totp_generator = pyotp.TOTP(TOTP_KEY.replace(" ", ""))
        live_otp = totp_generator.now()
        print(f"Generated 2FA passcode sequence on the fly: {live_otp}")
        
        # Step 4: Submit the dynamic OTP token to complete the login
        payload_phase2 = {
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": live_otp,
            "twofa_type": "totp"
        }
        response_phase2 = session.post("https://zerodha.com", data=payload_phase2)
        
        if response_phase2.status_code != 200:
            print(f"2FA Code verification rejected by server: {response_phase2.text}")
            return

        # Step 5: Follow the login chain to receive the final request token redirect
        response_redirect = session.get(login_url, allow_redirects=True)
        final_destination_url = response_redirect.url
        
        if "request_token=" not in final_destination_url:
            print(f"Failed to find request_token parameter in final chain: {final_destination_url}")
            return
            
        # Parse out the string values cleanly
        request_token = final_destination_url.split("request_token=")[1].split("&")[0]
        print("Successfully generated valid daily request token!")

        # Step 6: Initialize official KiteConnect SDK to finalize the dynamic access token
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=API_KEY)
        session_profile = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = session_profile["access_token"]
        print("Daily access token activated. Scanning for your ATO alerts...")

        # Step 7: Clear out active alerts using the freshly generated token
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
            
            # Match any ATO alert configuration, regardless of status string
            if alert_type == "ato":
                uuid = alert.get("uuid")
                name = alert.get("name", "ATO Alert")
                print(f"Wiping out ATO alert structure: '{name}' | UUID: {uuid}")
                
                del_url = f"https://kite.trade?uuid={uuid}"
                del_resp = requests.delete(del_url, headers=headers)
                
                if del_resp.status_code == 200:
                    deleted_count += 1
                    print(f"  -> Successfully deleted.")
                else:
                    print(f"  -> Deletion failed: {del_resp.text}")
                    
        print(f"Automated cleanup finished. Wiped {deleted_count} active ATO entries.")

    except Exception as e:
        print(f"The automated 10:00 AM routine encountered a code error: {e}")

if __name__ == "__main__":
    execute_automated_login_and_cleanup()
    
