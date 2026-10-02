import json
import requests

# Set your active access profile configs
API_KEY = "your_api_key_here"
ACCESS_TOKEN = "your_access_token_here"

headers = {
    "X-Kite-Version": "3",
    "Authorization": f"token {API_KEY}:{ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

def fetch_and_cleanup_atos():
    # --- FIXED ENDPOINT: Targeting Baskets/ATOs directly ---
    url = "https://kite.trade"
    
    try:
        print("Scanning active Kite Baskets and attached ATOs...")
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            print(f"API Connection Failure. Status code: {response.status_code}")
            print(f"Server says: {response.text}")
            return

        raw_payload = response.json()
        baskets_list = raw_payload.get("data", [])
        
        print(f"Successfully retrieved {len(baskets_list)} total basket/ATO entries.")

        # Filter for active configurations matching your image context
        target_atos = []
        for basket in baskets_list:
            # Check if this basket contains your NIFTY order bindings
            # or uses an ATO activation status tag
            if basket.get("status") == "enabled" or basket.get("type") == "ato":
                target_atos.append(basket)

        if not target_atos:
            print("No matching active ATO/Basket configurations discovered.")
            if baskets_list:
                print(f"Debug - Available items found on server: {json.dumps(baskets_list, indent=2)}")
            return

        print(f"Identified {len(target_atos)} target ATO basket configurations to clean up.")
        
        # Execute deletions using the Basket identifiers
        for ato in target_atos:
            basket_id = ato.get("basket_id") or ato.get("uuid")
            name = ato.get("name", "NIFTY 50")
            print(f"Executing deletion for: {name} (ID: {basket_id})")
            
            delete_url = f"https://kite.trade/{basket_id}"
            delete_response = requests.delete(delete_url, headers=headers)
            print(f"Delete Status: {delete_response.status_code} | Payload: {delete_response.text}")

    except Exception as e:
        print(f"An unexpected script exception occurred: {e}")

if __name__ == "__main__":
    fetch_and_cleanup_atos()
    
