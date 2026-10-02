import os
import sys
import time
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright
import pyotp

# 1. Fetch secure credentials from GitHub environment variables
API_KEY = os.environ["KITE_API_KEY"]
API_SECRET = os.environ["KITE_API_SECRET"]
USER_ID = os.environ["ZERODHA_USER_ID"]
PASSWORD = os.environ["ZERODHA_PASSWORD"]
TOTP_KEY = os.environ["ZERODHA_TOTP_KEY"]


def get_kite_session():
    print("Initiating automated login to generate fresh access token...")
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    request_token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Listen to background URL requests to intercept the request token
        def route_listener(request):
            nonlocal request_token
            url = request.url
            if "request_token=" in url:
                try:
                    request_token = url.split("request_token=")[1].split("&")[0]
                except Exception:
                    pass

        page.on("request", route_listener)
        page.goto(login_url)

        # Enter User ID and Password
        page.fill('input[type="text"]', USER_ID)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"]')

        # Wait for the 2FA numerical input box to appear
        page.wait_for_selector('input[type="number"], input[type="text"]', timeout=15000)
        
        # --- TOTP EXPIRATION GUARD ---
        totp = pyotp.TOTP(TOTP_KEY)
        
        # Calculate how many seconds are left before the current 6-digit token expires
        time_remaining = 30 - (int(time.time()) % 30)
        if time_remaining < 4:
            print(f"TOTP token rotating in {time_remaining}s. Waiting for a fresh code...")
            time.sleep(time_remaining + 1)
            
        token_2fa = totp.now()
        # ------------------------------
        
        # Fill the 2FA box and press Enter
        two_fa_input = page.locator('div.twofa-value input, input[type="number"], input[maxlength="6"]').first
        two_fa_input.fill(token_2fa)
        
        try:
            two_fa_input.press("Enter")
            page.wait_for_timeout(8000)  # Wait for redirection to execute completely
        except Exception:
            pass 

        browser.close()

    if not request_token:
        raise Exception("Failed to intercept the request_token from the login redirect sequence.")

    # Authenticate daily session
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    kite.set_access_token(data["access_token"])
    print("Session authenticated successfully.")
    return kite


def delete_untriggered_atos():
    try:
        kite = get_kite_session()
        print("Scanning active Kite alerts/ATOs...")

        # FIX: Using the verified, native endpoint string mapped in pykiteconnect
        alerts = kite._get("gtt.orders", {})
        deleted_count = 0

        for alert in alerts:
            # Targets untriggered, active orders exclusively
            # Status "active" means it's sitting on the server waiting to trigger
            if alert.get("status") == "active":
                alert_id = alert["id"]
                # Fallback to trading symbol or type descriptor if name field is custom
                alert_name = alert.get("condition", {}).get("tradingsymbol", "Unnamed ATO")
                
                print(f"Deleting ATO/GTT Order: {alert_name} (ID: {alert_id})")
                
                # FIX: Utilizing the correct delete routing API structure mapping
                kite._delete("gtt.delete", url_args={"trigger_id": alert_id})
                deleted_count += 1

        print(f"Successfully cleaned up {deleted_count} untriggered ATOs.")

    except Exception as e:
        print(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    delete_untriggered_atos()
    
