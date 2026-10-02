import os
import sys
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(login_url)

        # Enter User ID and Password
        page.fill('input[type="text"]', USER_ID)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"]')

        # Wait for the 2FA numerical input box to appear
        page.wait_for_selector('input[type="number"], input[type="text"]', timeout=15000)
        
        totp = pyotp.TOTP(TOTP_KEY)
        token_2fa = totp.now()
        
        # --- FIXED LOGIC ---
        # We start expecting the background network navigation loop to trigger BEFORE we push our keys
        with page.expect_navigation(url="**/127.0.0.1*", wait_until="commit", timeout=30000) as navigation_info:
            # We target the input block and fill it. Zerodha's web interface automatically 
            # triggers form submission the moment the 6th digit is typed, so a manual button click is omitted.
            2fa_input = page.locator('div.twofa-value input, input[type="number"], input[maxlength="6"]').first
            2fa_input.fill(token_2fa)
            
            # Fallback: if it doesn't auto-submit on key fill, press "Enter" safely on the keyboard
            2fa_input.press("Enter")
        
        final_url = navigation_info.value.url
        
        # Extract the request token parameter out of the final URL string
        request_token = final_url.split("request_token=")[1].split("&")[0]
        browser.close()

    # Exchange the request token for a valid daily session access token
    data = kite.generate_session(request_token, secret=API_SECRET)
    kite.set_access_token(data["access_token"])
    print("Session authenticated successfully.")
    return kite


def delete_untriggered_atos():
    try:
        kite = get_kite_session()
        print("Scanning active Kite alerts/ATOs...")

        # Fetch all active alerts (triggers)
        alerts = kite.get_triggers()
        deleted_count = 0

        for alert in alerts:
            # Targets untriggered, active ATO-linked baskets exclusively
            if alert.get("status") == "active" and alert.get("type") == "ato":
                alert_id = alert["id"]
                alert_name = alert.get("name", "Unnamed ATO")
                print(f"Deleting ATO: {alert_name} (ID: {alert_id})")
                
                # Delete the specific alert from the server
                kite.delete_trigger(trigger_id=alert_id)
                deleted_count += 1

        print(f"Successfully cleaned up {deleted_count} untriggered ATOs.")

    except Exception as e:
        print(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    delete_untriggered_atos()
    
