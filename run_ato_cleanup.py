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

    request_token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Listen to all background URL requests. The moment it hits our 127.0.0.1 redirect URL,
        # we pull out the request_token parameter immediately from the network layer.
        def route_listener(request):
            nonlocal request_token
            url = request.url
            if "127.0.0.1" in url and "request_token=" in url:
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
        
        totp = pyotp.TOTP(TOTP_KEY)
        token_2fa = totp.now()
        
        # Fill the 2FA box and press Enter
        two_fa_input = page.locator('div.twofa-value input, input[type="number"], input[maxlength="6"]').first
        two_fa_input.fill(token_2fa)
        
        # We wrap the submit action in a safe try-except block. Even if the network crashes
        # with a connection refusal, our background listener will have already intercepted the token.
        try:
            two_fa_input.press("Enter")
            page.wait_for_timeout(5000)  # Give it 5 seconds to process redirection fully
        except Exception:
            pass  # Ignore network errors since we only need the URL string

        browser.close()

    if not request_token:
        raise Exception("Failed to intercept the request_token from the login redirect sequence.")

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
    
