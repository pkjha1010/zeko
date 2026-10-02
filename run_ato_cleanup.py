import requests

API_KEY = "your_api_key_here"
ACCESS_TOKEN = "your_access_token_here"

headers = {
    "X-Kite-Version": "3",
    "Authorization": f"token {API_KEY}:{ACCESS_TOKEN}"
}

def verify_profile():
    # Target the profile metadata route
    url = "https://kite.trade"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            user_data = response.json().get("data", {})
            server_user_id = user_data.get("user_id")
            user_name = user_data.get("user_name")
            
            print("==========================================")
            print(f"✅ CONNECTED TO USER ID: {server_user_id} ({user_name})")
            print(f"📱 YOUR MOBILE APP USER ID IS: INH195")
            print("==========================================")
            
            if str(server_user_id).strip().upper() != "INH195":
                print("❌ MISMATCH DETECTED! Your script is using tokens for a different account.")
            else:
                print("👍 Account matches! The session profile is correct.")
        else:
            print(f"Authentication Failed: {response.status_code} | {response.text}")
    except Exception as e:
        print(f"Error connecting: {e}")

if __name__ == "__main__":
    verify_profile()
    
