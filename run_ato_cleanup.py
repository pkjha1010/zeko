import logging
import json
import requests
from kiteconnect import KiteConnect

# Optional: Enable debug logs to print out full server requests and responses
logging.basicConfig(level=logging.DEBUG)

# ==========================================
# CONFIGURATION & INITIALIZATION
# ==========================================
API_KEY = "your_api_key_here"
ACCESS_TOKEN = "your_access_token_here"  # Re-use your authenticated access token

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

# Define headers using kiteconnect internally formatted properties
headers = {
    "X-Kite-Version": "3",
    "Authorization": f"token {API_KEY}:{ACCESS_TOKEN}"
}

def fetch_and_cleanup_atos():
    print("Scanning active Kite alerts/ATOs...")
    
    # 1. Fetch alerts using standard HTTP requests to ensure full payload accessibility
    url = "https://kite.trade"
    
    try:
        response = requests.get(url, headers=headers)
        
        # Guard against non-JSON HTTP errors (e.g. 401 Unauthorized, 502 Bad Gateway)
        if response.status_code != 200:
            print(f"Execution failed: API returned status code {response.status_code}")
            print(f"Response Payload: {response.text}")
            return

        # Safe parsing block to resolve "Expecting value: line 1 column 1 (char 0)"
        try:
            alerts_list = response.json()
        except json.JSONDecodeError as json_err:
            print(f"Execution failed parsing JSON: {json_err}")
            print(f"Raw un-parsable response received: '{response.text}'")
            return

        # Extract data payload array if it is packed within a standard dict wrap
        if isinstance(alerts_list, dict) and "data" in alerts_list:
            alerts_list = alerts_list["data"]

        # 2. Filter for untriggered (enabled) ATO orders
        untriggered_atos = [
            alert for alert in alerts_list
            if alert.get("type") == "ato" and alert.get("status") == "enabled"
        ]

        if not untriggered_atos:
            print("No untriggered ATO orders found.")
            return

        print(f"Found {len(untriggered_atos)} untriggered ATO configuration(s).")
        
        # 3. Print or Action (Looping through and deleting them)
        for ato in untriggered_atos:
            ato_uuid = ato.get("uuid")
            ato_name = ato.get("name", "Unnamed ATO")
            print(f"Targeting ATO -> Name: {ato_name} | UUID: {ato_uuid}")
            
            # Execute DELETE query for the matching UUID
            delete_url = f"https://kite.trade?uuid={ato_uuid}"
            delete_response = requests.delete(delete_url, headers=headers)
            
            if delete_response.status_code == 200:
                print(f"Successfully deleted ATO: {ato_uuid}")
            else:
                print(f"Failed to delete ATO {ato_uuid}: {delete_response.text}")

    except Exception as e:
        print(f"An unexpected system exception occurred: {e}")

if __name__ == "__main__":
    fetch_and_cleanup_atos()
            
