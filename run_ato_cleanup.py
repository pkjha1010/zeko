import os
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs

import pyotp
import requests
from kiteconnect import KiteConnect

API_KEY = os.environ["KITE_API_KEY"].strip()
API_SECRET = os.environ["KITE_API_SECRET"].strip()
USER_ID = os.environ["ZERODHA_USER_ID"].strip()
PASSWORD = os.environ["ZERODHA_PASSWORD"].strip()
TOTP_KEY = os.environ["ZERODHA_TOTP_KEY"].strip().replace(" ", "")

LOGIN_URL = "https://kite.zerodha.com/connect/login"
API_LOGIN = "https://kite.zerodha.com/api/login"
API_TWOFA = "https://kite.zerodha.com/api/twofa"
ALERTS_URL = "https://api.kite.trade/alerts"


def log(msg):
    print(msg, flush=True)


def check(r, step):
    if r.status_code != 200:
        log(f"[{step}] FAILED status={r.status_code} body={r.text[:300]}")
        r.raise_for_status()

