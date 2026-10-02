import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import pyotp
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright

# Fetching environment credentials matching your GitHub secrets screenshot
API_KEY = os.environ.get("KITE_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET")
USER_ID = os.environ.get("ZERODHA_USER_ID")
PASSWORD = os.environ.get("ZERODHA_PASSWORD")
TOTP_SECRET = os.environ.get("ZERODHA_TOTP_KEY")

if not all([API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET]):
    print("Error: Missing required secrets configuration settings in GitHub.")
    sys.exit(1)

def get_automated_access_token():
    """Bypasses basic cloud automation locks using localized structural elements"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting browser auto-login sequence...")
    
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    with sync_playwright() as p:
        # Emulate standard Windows Chrome profile layout parameters to stop basic bot indicators
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        page.goto(login_url)
        page.wait_for_load_state("networkidle")
        
        # Interact with the structural input forms directly
        page.locator("#userid").wait_for(state="visible", timeout=15000)
        page.locator("#userid").fill(USER_ID)
        time.sleep(0.5)
        
        page.locator("#password").fill(PASSWORD)
        time.sleep(0.5)
        
        page.locator("button[type='submit']").click()
        
        # Await the arrival of the 2FA input layout
        print("Credentials submitted. Waiting for TOTP input fields to route...")
        page.locator("#totp").wait_for(state="visible", timeout=20000)
        
        # Generate our dynamic token string
        totp_pin = pyotp.TOTP(TOTP_SECRET).now()
        page.locator("#totp").fill(totp_pin)
        
        # Wait for redirect verification sequence to resolve
        page.wait_for_load_state("networkidle")
        time.sleep(3) 
        
        final_url = page.url
        browser.close()
        
        # Extract the short-lived request token string
        parsed_url = urlparse(final_url)
        query_params = parse_qs(parsed_url.query)
        
        if "request_token" not in query_params:
            raise Exception(f"Redirect failed. Anti-bot block or CAPTCHA triggered at target URL: {final_url}")
            
        request_token = query_params["request_token"]
        
        # Hand off request token to establish active access session context
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        print("Daily active token created successfully!")
        return session_data["access_token"]

def clean_untriggered_orders():
    try:
        # Establish session validation
        access_token = get_automated_access_token()
        
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
        
        print("Checking order book tracking positions...")
        orders = kite.orders()
        cancelled_count = 0
        
        for order in orders:
            # Handle untriggered positions (ATO / Day SL orders)
            if order["status"] == "TRIGGER PENDING":
                print(f"Cancelling Untriggered Order -> ID: {order['order_id']} | Symbol: {order['tradingsymbol']}")
                kite.cancel_order(variety=order["variety"], order_id=order["order_id"])
                cancelled_count += 1
                
        print(f"Job finished completely. Total cleared orders: {cancelled_count}")
        
    except Exception as e:
        print(f"Automation execution error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    clean_untriggered_orders()
    
