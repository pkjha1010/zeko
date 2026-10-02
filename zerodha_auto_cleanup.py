import os
import sys
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import pyotp
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright

# Fetching the exact secret names from your screenshot
API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
USER_ID = os.environ.get("ZERODHA_USER_ID")
PASSWORD = os.environ.get("ZERODHA_PASSWORD")
TOTP_SECRET = os.environ.get("ZERODHA_TOTP_KEY")

if not all([API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET]):
    print("Error: Missing required secrets configuration.")
    sys.exit(1)

def get_automated_access_token():
    """Uses a real headless browser to bypass anti-bot locks and grab the login token"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting browser auto-login...")
    
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    with sync_playwright() as p:
        # Launch an invisible cloud browser instance
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # Navigate directly to Zerodha's official login page
        page.goto(login_url)
        page.wait_for_load_state("networkidle")
        
        # Type Client ID and Password safely into the fields
        page.locator("input[type='text']").fill(USER_ID)
        page.locator("input[type='password']").fill(PASSWORD)
        page.locator("button[type='submit']").click()
        
        # Wait for the 2FA TOTP input field to appear
        page.wait_for_selector("input[label='TOTP']")
        
        # Generate the dynamic 6-digit TOTP pin
        totp_pin = pyotp.TOTP(TOTP_SECRET).now()
        
        # Type the TOTP pin and submit
        page.locator("input[label='TOTP']").fill(totp_pin)
        
        # Wait for the page to process and redirect back to your callback URL
        page.wait_for_load_state("networkidle")
        
        # Capture the final URL which contains our short-lived request token
        final_url = page.url
        browser.close()
        
        # Extract the request token from the URL parameters
        parsed_url = urlparse(final_url)
        query_params = parse_qs(parsed_url.query)
        
        if "request_token" not in query_params:
            raise Exception("Authentication redirect failed. Check credentials or TOTP seed key.")
            
        request_token = query_params["request_token"][0]
        
        # Exchange the request token for a daily active trading session
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        print("Successfully generated daily Zerodha access token!")
        return session_data["access_token"]

def clean_untriggered_orders():
    try:
        # 1. Automate the browser login to get the access token
        access_token = get_automated_access_token()
        
        # 2. Connect to the Kite API using the generated token
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
        
        print("Scanning order book for untriggered orders...")
        orders = kite.orders()
        cancelled_count = 0
        
        for order in orders:
            # Drop orders that are still waiting for triggers (ATO / SL / SL-M)
            if order["status"] == "TRIGGER PENDING":
                print(f"Cancelling Untriggered Order -> ID: {order['order_id']} | Symbol: {order['tradingsymbol']}")
                kite.cancel_order(variety=order["variety"], order_id=order["order_id"])
                cancelled_count += 1
                
        print(f"Cleanup successfully finished. Total cleared orders: {cancelled_count}")
        
    except Exception as e:
        print(f"Automation execution error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    clean_untriggered_orders()
    
