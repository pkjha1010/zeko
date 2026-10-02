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

    request_token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # --- BULLETPROOF NETWORK INTERCEPTOR ---
        # Instead of waiting for page frames to render, this actively sniffs background 
        # traffic. The exact millisecond the token parameters are created, we capture it.
        def network_sniffer(request):
            nonlocal request_token
            url = request.url
            if "request_token=" in url:
                try:
                    parsed_url = urlparse(url)
                    query_params = parse_qs(parsed_url.query)
                    if "request_token" in query_params:
                        request_token = query_params["request_token"][0]
                except Exception:
                    pass

        # Attach our network sniffer to the active web lifecycle
        page.on("request", network_sniffer)
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
        
        # Fill the 2FA box and hit Enter
        two_fa_input = page.locator('div.twofa-value input, input[type="number"], input[maxlength="6"]').first
        two_fa_input.fill(token_2fa)
        two_fa_input.press("Enter")
        
        # Give the background network engine up to 10 seconds to catch the redirect token parameter
        for _ in range(20):
            if request_token:
                break
            page.wait_for_timeout(500)

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

        # Fetch custom alerts using the backend namespace mapped endpoint
        alerts = kite._get("alerts", {})
        deleted_count = 0

        for alert in alerts:
            # Filter condition parameters: status is 'enabled' and type is 'ato'
            if alert.get("status") == "enabled" and alert.get("type") == "ato":
                alert_uuid = alert["uuid"]  # Extract unique hash string
                alert_name = alert.get("name", "Unnamed ATO")
                
                print(f"Deleting untriggered ATO: {alert_name} (UUID: {alert_uuid})")
                
                # Delete target by passing correct documentation query parameters
                kite._delete("alerts", params={"uuid": alert_uuid})
                deleted_count += 1

        print(f"Successfully cleaned up {deleted_count} untriggered ATOs.")

    except Exception as e:
        print(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    delete_untriggered_atos()
    
