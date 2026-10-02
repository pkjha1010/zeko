import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import pyotp
import schedule
from kiteconnect import KiteConnect

# Selenium Components for Local Browser Control
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ==============================================================================
# LOCAL NETWORK CONFIGURATION & CREDENTIALS
# ==============================================================================
API_KEY = "your_kite_api_key_here"
API_SECRET = "your_kite_api_secret_here"
USER_ID = "your_zerodha_user_id_here"
PASSWORD = "your_zerodha_password_here"
TOTP_SECRET = "your_32_character_totp_key_here"  # Permanent seed key string

# Validate inputs
if "here" in [API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET]:
    print("CRITICAL ERROR: Please fill in your actual Zerodha credentials inside the script variables.")
    sys.exit(1)


# ==============================================================================
# LOCAL AUTO-LOGIN WITH REAL CHROME
# ==============================================================================
def get_local_access_token():
    """Launches a localized visible Chrome browser instance to bypass data center CAPTCHAs"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Booting local browser instance...")
    
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    # Configure Chrome options to look like a normal human workspace setup
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    # Boot local driver configuration
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get(login_url)
        time.sleep(2)  # Give page a brief moment to stabilize

        # 1. Fill User ID and Password fields smoothly
        driver.find_element(By.ID, "userid").send_keys(USER_ID)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        time.sleep(0.5)
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        
        # 2. Handle the 2FA TOTP Generation & Insertion screen
        print("Bypassing primary screen. Generating and entering dynamic 2FA Pin...")
        time.sleep(2)  # Wait for TOTP input to focus mount
        
        totp_pin = pyotp.TOTP(TOTP_SECRET).now()
        driver.find_element(By.ID, "totp").send_keys(totp_pin)
        
        # 3. Wait for the local redirect to capture the parameter token string
        time.sleep(4)
        final_redirect_url = driver.current_url
        driver.quit()  # Safely terminate local browser instance
        
        # 4. Extract parameters
        parsed_url = urlparse(final_redirect_url)
        query_params = parse_qs(parsed_url.query)
        
        if "request_token" not in query_params:
            raise Exception("Login failed or blocked. Did not reach local redirect URL.")
            
        request_token = query_params["request_token"][0]
        
        # Generate the verified operational daily token
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        print("✅ Daily Access Token generated successfully!")
        return session_data["access_token"]

    except Exception as e:
        driver.quit()
        print(f"❌ Local login routine encountered an error: {e}")
        return None


# ==============================================================================
# MAIN CLEANUP EXECUTION CRON ROUTINE
# ==============================================================================
def run_daily_10am_cleanup():
    print(f"\n⚡ Starting scheduled 10:00 AM order cleanup execution sequence...")
    
    # Run the dynamic local browser token generator
    access_token = get_local_access_token()
    if not access_token:
        print("Aborting cleanup task: Failed to establish active api session credentials.")
        return

    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
        
        orders = kite.orders()
        cancelled_count = 0
        
        for order in orders:
            # Safely target regular day ATO/SL orders stuck in 'TRIGGER PENDING' status
            if order["status"] == "TRIGGER PENDING":
                print(f"Cancelling Untriggered Order -> ID: {order['order_id']} | Symbol: {order['tradingsymbol']}")
                kite.cancel_order(variety=order["variety"], order_id=order["order_id"])
                cancelled_count += 1
                
        print(f"✨ Task complete! Total untriggered orders removed today: {cancelled_count}\n")

    except Exception as e:
        print(f"❌ Error encountered while communicating with order book: {e}")


# ==============================================================================
# BACKGROUND SERVICE DAEMON LOOP
# ==============================================================================
# Schedule the cleanup task to execute precisely at 10:00 AM local system time
schedule.every().day.at("10:00").do(run_daily_10am_cleanup)

print("=" * 70)
print(" Zerodha Local Network 10:00 AM Cleaner Active.")
print(" Keeping connection window alive. Do not close this terminal screen.")
print(" Ensure your system's sleep mode is disabled during market operational hours.")
print("=" * 70)

try:
    while True:
        schedule.run_pending()
        time.sleep(1)
except KeyboardInterrupt:
    print("\nLocal service daemon safely disconnected by user request.")
    
