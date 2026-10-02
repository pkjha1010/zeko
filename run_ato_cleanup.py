import os
import sys
import time
from urllib.parse import urlparse, parse_qs
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright
import pyotp

# Fetch secure credentials from GitHub environment variables
API_KEY = os.environ["KITE_API_KEY"]
API_SECRET = os.environ["KITE_API_SECRET"]
USER_ID = os.environ["ZERODHA_USER_ID"]
PASSWORD = os.environ["ZERODHA_PASSWORD"]
TOTP_KEY = os.environ["ZERODHA_TOTP_KEY"]


def get_kite_session():
    print("Initiating automated login to generate fresh access token...")
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    final_url = None

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
        
        # --- TOTP EXPIRATION GUARD ---
        totp = pyotp.TOTP(TOTP_KEY)
        time_remaining = 30 - (int(time.time()) % 30)
        if time_remaining < 4:
            print(f"TOTP token rotating in {time_remaining}s. Waiting for a fresh code...")
            time.sleep(time_remaining + 1)
            
        token_2fa = totp.now()
        # ------------------------------
        
        # Fill the 2FA box
        two_fa_input = page.locator('div.twofa-value input, input[type="number"], input[maxlength="6"]').first
        two_fa_input.fill(token_2fa)
        
        # Intercept redirection to the 127.0.0.1 link safely
        try:
            two_fa_input.press("Enter")
            page.wait_for_url("**/127.0.0.1*", timeout=15000, wait_until="commit")
            final_url = page.url
        except Exception:
            # Fallback: Read address bar string values if a network refusal frame triggers
            final_url = page.url

        browser.close()

    # Parse request token parameter safely out of the URL string
    if final_url and "request_token=" in final_url:
        parsed_url = urlparse(final_url)
        params = parse_qs(parsed_url.query)
        request_token = params.get("request_token")
        # Ensure we extract string values out of parameter dictionary arrays
        if isinstance(request_token, list):
            request_token = request_token[0]
    else:
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

        # Matches GET /alerts official documentation backend routing rules
        alerts = kite._get("alerts", {})
        deleted_count = 0

        for alert in alerts:
            # Filter condition parameters: status is 'enabled' and type is 'ato'
            if alert.get("status") == "enabled" and alert.get("type") == "ato":
                alert_uuid = alert["uuid"]  # Extract unique hash string
                alert_name = alert.get("name", "Unnamed ATO")
                
                print(f"Deleting untriggered ATO: {alert_name} (UUID: {alert_uuid})")
                
                # Matches DELETE /alerts?uuid={uuid} query parameter rule mapping
                kite._delete("alerts", params={"uuid": alert_uuid})
                deleted_count += 1

        print(f"Successfully cleaned up {deleted_count} untriggered ATOs.")

    except Exception as e:
        print(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    delete_untriggered_atos()
    
