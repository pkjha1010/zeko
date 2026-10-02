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


def get_request_token():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    params = {"v": "3", "api_key": API_KEY}

    r = s.get(LOGIN_URL, params=params, timeout=30)
    log(f"[1] connect/login page status={r.status_code}")

    r = s.post(API_LOGIN, data={"user_id": USER_ID, "password": PASSWORD}, timeout=30)
    check(r, "2 login")
    request_id = r.json()["data"]["request_id"]
    log("[2] password accepted")

    r = s.post(
        API_TWOFA,
        data={
            "user_id": USER_ID,
            "request_id": request_id,
            "twofa_value": pyotp.TOTP(TOTP_KEY).now(),
            "twofa_type": "totp",
            "skip_session": "true",
        },
        timeout=30,
    )
    check(r, "3 twofa")
    log("[3] TOTP accepted")

    url, first = LOGIN_URL, True
    for _ in range(8):
        r = s.get(url, params=params if first else None,
                  allow_redirects=False, timeout=30)
        first = False
        loc = r.headers.get("Location")
        if not loc:
            break
        url = urljoin(r.url, loc)
        qs = parse_qs(urlparse(url).query)
        if "request_token" in qs:
            log("[4] request_token obtained")
            return qs["request_token"][0]
    raise RuntimeError(
        "request_token not found. Open your Kite app's login URL once in a "
        "browser to authorize it, and check the app's redirect URL setting."
    )


def login_with_retry(tries=3):
    last = None
    for i in range(1, tries + 1):
        try:
            kite = KiteConnect(api_key=API_KEY)
            data = kite.generate_session(get_request_token(), api_secret=API_SECRET)
            log("[5] access_token obtained")
            return data["access_token"]
        except Exception as e:
            last = e
            log(f"Login attempt {i}/{tries} failed: {e}")
            time.sleep(10)
    raise last


def fetch_alerts(headers):
    r = requests.get(ALERTS_URL, headers=headers, timeout=30)
    check(r, "alerts list")
    return r.json().get("data") or []


def is_ato(a):
    return "ato" in str(a.get("type", "")).strip().lower()


def main():
    token = login_with_retry()
    headers = {"X-Kite-Version": "3", "Authorization": f"token {API_KEY}:{token}"}

    alerts = fetch_alerts(headers)
    log(f"Total alerts returned: {len(alerts)}")
    for a in alerts:
        log(f"  uuid={a.get('uuid')} type={a.get('type')} "
            f"status={a.get('status')} name={a.get('name')}")

    atos = [a for a in alerts if is_ato(a)]
    log(f"ATO alerts to delete: {len(atos)}")

    for a in atos:
        for attempt in range(3):
            d = requests.delete(ALERTS_URL, headers=headers,
                                params={"uuid": a["uuid"]}, timeout=30)
            log(f"DELETE {a['uuid']} -> {d.status_code} {d.text[:200]}")
            if d.status_code == 200:
                break
            time.sleep(3)

    remaining = [a for a in fetch_alerts(headers) if is_ato(a)]
    log(f"ATO alerts remaining after cleanup: {len(remaining)}")
    if remaining:
        log("ERROR: some ATOs were NOT deleted.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
            
