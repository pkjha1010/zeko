import json
import logging
import requests

# Enable explicit console logging
logging.basicConfig(level=logging.INFO)

# ==========================================
# CREDENTIAL CONFIGURATION
# ==========================================
# Replace these with your live credentials or pass them via environment variables
API_KEY = "your_api_key_here"
ACCESS_TOKEN = "your_access_token_here"

headers = {
    "X-Kite-Version": "3",
    "Authorization": f"token {API_KEY}:{ACCESS_TOKEN}",
    "Content-Type": "application/x-www-form-urlencoded"
}

def execute_ato_cleanup():
    url = "https://kite.trade"
    
    try:
        print("Executing fetch call to Zerodha Alerts API...")
        response = requests.get(url, headers=headers)
        
        # 1. Guard against authorization errors or server communication drops
        if response.status_code != 200:
            print(f"Error: API returned HTTP status {response.status_code}")
            print(f"Raw Error Content: {response.text}")
            return

        # 2. Safely parse the server payload response
        try:
            payload = response.json()
        except json.JSONDecodeError as decode_err:
            print(f"JSON Parsing crashed. Input stream text was: '{response.text}'")
            return

        # Access the mandatory documentation "data" block array
        alerts_list = payload.get("data", [])
        if not isinstance(alerts_list, list):
            print("Unexpected response structure: 'data' field is not a list array.")
            return

        print(f"Successfully fetched active stream. Scanning {len(alerts_list)} total alert profiles...")

        # 3. Filter for ATO types while bypassing strict status checks
        target_atos = []
        for alert in alerts_list:
            alert_type = str(alert.get("type", "")).strip().lower()
            alert_status = str(alert.get("status", "")).strip().lower()
            alert_name = alert.get("name", "Unnamed Alert")
            
            # Catch ANY alert flagged as an ATO type
            if alert_type == "ato":
                print(f"DEBUG CRON MATCH: Found ATO alert '{alert_name}' | Status on server: '{alert_status}'")
                target_atos.append(alert)

        # 4. Fallback trace loop if no matches were identified
        if not target_atos:
            print("\n[!] No matching ATO configurations discovered with this run.")
            print("--- PRINTING RAW SYSTEM ENTRIES TO DIAGNOSE PAYLOAD STRUCTURE ---")
            print(json.dumps(alerts_list, indent=2))
            return

        print(f"\nIdentified {len(target_atos)} target ATO configuration(s) for forced removal.")

        # 5. Delete the targeted ATOs using query string routing parameters
        for ato in target_atos:
            uuid = ato.get("uuid")
            name = ato.get("name", "Unnamed ATO")
            print(f"Sending DELETE request for target: '{name}' | UUID: {uuid}")
            
            delete_url = f"https://kite.trade?uuid={uuid}"
            delete_response = requests.delete(delete_url, headers=headers)
            
            if delete_response.status_code == 200:
                print(f"  -> Success: Deleted {uuid}. Server says: {delete_response.text}")
            else:
                print(f"  -> Failed: Could not delete {uuid}. Server code: {delete_response.status_code} | Details: {delete_response.text}")

    except Exception as general_err:
        print(f"A system execution runtime exception occurred: {general_err}")

if __name__ == "__main__":
    execute_ato_cleanup()
    
