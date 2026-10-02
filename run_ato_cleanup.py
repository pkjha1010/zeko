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

        # Network interceptor to extract request token from redirect URLs
        def route_listener(request):
            nonlocal request_token
            url = request.url
            if "request_token=" in url:
                try:
                    # Extracts the exact parameter value from the token string
                    request_token = url.split("request_token=").split("&")
                except Exception:
                    pass

        page.on("request", route_listener)
        page.goto(login_url)

        # Enter User ID and Password
        page.fill('input[type="text"]', USER_ID)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"]')

        # Wait for the 2FA input box
        page.wait_for_selector('input[type="number"], input[type="text"]', timeout=15000)
        
        # TOTP Window Safety Check 
        totp = pyotp.TOTP(TOTP_KEY)
        time_remaining = 30 - (int(time.time()) % 30)
        if time_remaining < 4:
            print(f"TOTP rotating in {time_remaining}s. Waiting for a fresh window...")
            time.sleep(time_remaining + 1)
            
        token_2fa = totp.now()
        
        # Enter 2FA code and trigger submit sequence
        two_fa_input = page.locator('div.twofa-value input, input[type="number"], input[maxlength="6"]').first
        two_fa_input.fill(token_2fa)
        two_fa_input.press("Enter")
        
        # --- FIXED LOGIC BLOCK ---
        # Instead of closing the browser instantly, we pause and monitor the background 
        # network traffic loop for up to 10 seconds or until the token parameter arrives.
        for _ in range(20):  # 20 loops x 500ms = 10 seconds total wait buffer
            if request_token:
                break
            page.wait_for_timeout(5000)
        # --------------------------

        browser.close()

    if not request_token:
        raise Exception("Failed to intercept the request_token from the login redirect sequence.")

    # Exchange token for the daily access credentials 
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    kite.set_access_token(data["access_token"])
    print("Session authenticated successfully.")
    return kite


def delete_untriggered_atos():
    try:
        kite = get_kite_session()
        print("Scanning active Kite alerts/ATOs...")

        # 1. Fetch live alerts using the explicit backend namespace dictionary path
        alerts = kite._get("alerts", {})
        deleted_count = 0

        for alert in alerts:
            # 2. Check documentation parameter filters: status is 'enabled' and type is 'ato'
            if alert.get("status") == "enabled" and alert.get("type") == "ato":
                alert_uuid = alert["uuid"]  # Key matches string schema hash value
                alert_name = alert.get("name", "Unnamed ATO")
                print(f"Deleting ATO: {alert_name} (UUID: {alert_uuid})")
                
                # 3. Route explicit delete network transaction passing target uuid payload
                kite._delete("alerts", params={"uuid": alert_uuid})
                deleted_count += 1

        print(f"Successfully cleaned up {deleted_count} untriggered ATOs.")

    except Exception as e:
        print(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    delete_untriggered_atos()
    
