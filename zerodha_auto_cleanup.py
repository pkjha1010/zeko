import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import pyotp
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright

# Fetching your exact secret names from your repository settings screenshot
API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
USER_ID = os.environ.get("ZERODHA_USER_ID")
PASSWORD = os.environ.get("ZERODHA_PASSWORD")
TOTP_SECRET = os.environ.get("ZERODHA_TOTP_KEY")

if not all([API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET]):
    print("Error: Missing required secrets configuration in GitHub settings.")
    sys.exit(1)

def get_automated_access_token():
    """Bypasses web scrapers and grabs the login token using structural IDs"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting browser auto-login context...")
    
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    with sync_playwright() as p:
        # Launch Chromium with a standard desktop user agent string to prevent bot blocks
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        # Navigate to login link
        page.goto(login_url)
        page.wait_for_load_state("networkidle")
        
        # Fill User ID with human-like delay adjustments
        page.locator("#userid").wait_for(state="visible", timeout=15000)
        page.locator("#userid").fill(USER_ID)
        time.sleep(0.5)
        
        # Fill Password
        page.locator("#password").fill(PASSWORD)
        time.sleep(0.5)
        
        # Click Continue
        page.locator("button[type='submit']").click()
        
        # Monitor and wait for the structural TOTP field container to mount
        print("Passing credentials... waiting for 2FA verification panel.")
        page.locator("#totp").wait_for(state="visible", timeout=20000)
        
        # Compute the live 6-digit TOTP key parameter
        totp_pin = pyotp.TOTP(TOTP_SECRET).now()
        
        # Input the dynamic token key
        page.locator("#totp").fill(totp_pin)
        
        # Wait for authentication redirect validation to settle completely
        page.wait_for_load_state("networkidle")
        time.sleep(3) 
        
        # Grab final authorized redirect endpoint string URL
        final_url = page.url
        browser.close()
        
        # Parse parameters out of url string context
        parsed_url = urlparse(final_url)
        query_params = parse_qs(parsed_url.query)
        
        if "request_token" not in query_params:
            raise Exception(f"Redirect failed. Token absent from endpoint path location: {final_url}")
            
        request_token = query_params["request_token"][0]
        
        # Exchange the request token for a standard API session access token
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        print("Successfully generated daily Zerodha access token!")
        return session_data["access_token"]

def clean_untriggered_orders():
    try:
        # 1. Execute login automation flow
        access_token = get_automated_access_token()
        
        # 2. Bind active token to the official connector library
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
        
        print("Scanning current open orders list...")
        orders = kite.orders()
        cancelled_count = 0
        
        for order in orders:
            # Check for orders that are still untriggered (ATO / SL / SL-M / Basket Alerts)
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
    
