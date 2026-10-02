import json
import logging
import requests

# Enable explicit console debugging to see the raw request flow
logging.basicConfig(level=logging.INFO)

# ==========================================
# CREDENTIAL CONFIGURATION
# ==========================================
# Pass these securely from GitHub Secrets or write them here directly for testing
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
        print("Executing GET call to https://kite.trade...")
        response = requests.get(url, headers=headers)
        
        # 1. Guard against authorization errors or bad gateway drops
        if response.status_code != 200:
            print(f"Error: API returned HTTP status {response.status_code}")
            print(f"Raw Error Content: {response.text}")
            return

        # 2. Parse response and access the mandatory documentation "data" block
        try:
            payload = response.json()
        except json.JSONDecodeError as decode_err:
            print(f"JSON Parsing crashed. Input stream text was: '{response.text}'")
            return

        # As seen in the official doc screenshot: {"status": "success", "data": [...]}
        alerts_list = payload.get("data", [])
        if not isinstance(alerts_list, list):
            print("Unexpected format: 'data' field is not a list array.")
            return

        print(f"Successfully connected. Scanning {len(alerts_list)} total alerts...")

        # 3. Filter strictly based on the structural documentation fields
        target_atos = []
        for alert in alerts_list:
            alert_type = str(alert.get("type", "")).strip().lower()
            alert_status = str(alert.get("status", "")).strip().lower()
            
            # Target alert rules matching your active NIFTY order state
            if alert_type == "ato" and alert_status == "enabled":
                target_atos.append(alert)

        if not target_atos:
            print("No matching, untriggered ATO orders found in this account scan.")
            # Safety backup dump so you can view unmatched objects inside GitHub Action logs
            print(f"Raw system alerts trace: {json.dumps(alerts_list, indent=2)}")
            return

        print(f"Matched {len(target_atos)} active ATO configuration(s) for removal.")

        # 4. Delete the matched ATOs using query string routing per image_8OqkjI.png
        for ato in target_atos:
            uuid = ato.get("uuid")
            name = ato.get("name", "Unnamed ATO")
            print(f"Sending DELETE request for: '{name}' | UUID: {uuid}")
            
            delete_url = f"https://kite.trade?uuid={uuid}"
            delete_response = requests.delete(delete_url, headers=headers)
            
            if delete_response.status_code == 200:
                print(f"-> Success: Deleted {uuid}. Server payload: {delete_response.text}")
            else:
                print(f"-> Failed: Could not delete {uuid}. Server code: {delete_response.status_code} | Payload: {delete_response.text}")

    except Exception as general_err:
        print(f"A system execution runtime exception occurred: {general_err}")

if __name__ == "__main__":
    execute_ato_cleanup()
    
