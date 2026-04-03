import os
import datetime
import math
import time
import requests
from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect

# ======================
# CONFIG
# ======================

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65
LIVE_TRADING = True

TARGET_DELTA = 0.70
RISK_FREE_RATE = 0.06
STRIKE_STEP = 50
MAX_STEPS = 15

SL_POINTS = 40
TARGET_POINTS = 80

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")

# ======================
# INSTRUMENT CACHE
# ======================

NIFTY_OPTIONS = []

def load_instruments():

    global NIFTY_OPTIONS

    print("Downloading instruments...")

    instruments = kite.instruments("NFO")

    NIFTY_OPTIONS = [
        i for i in instruments
        if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"
    ]

    print("NIFTY options loaded:", len(NIFTY_OPTIONS))


load_instruments()

# ======================
# NORMAL DISTRIBUTION
# ======================

def norm_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


# ======================
# BLACK SCHOLES PRICE
# ======================

def bs_price(S, K, T, r, sigma, option_type):

    if T <= 0:
        return max(0, S-K) if option_type == "CE" else max(0, K-S)

    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)

    if option_type == "CE":
        return S*norm_cdf(d1)-K*math.exp(-r*T)*norm_cdf(d2)
    else:
        return K*math.exp(-r*T)*norm_cdf(-d2)-S*norm_cdf(-d1)


# ======================
# BLACK SCHOLES DELTA
# ======================

def bs_delta(S, K, T, r, sigma, option_type):

    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))

    if option_type == "CE":
        return norm_cdf(d1)
    else:
        return norm_cdf(d1)-1


# ======================
# IMPLIED VOLATILITY
# ======================

def implied_volatility(price, S, K, T, r, option_type):

    sigma = 0.30

    for _ in range(20):

        price_est = bs_price(S, K, T, r, sigma, option_type)

        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))

        vega = S * math.sqrt(T) * (1/math.sqrt(2*math.pi)) * math.exp(-0.5*d1*d1)

        if vega == 0:
            break

        sigma = sigma - (price_est-price)/vega

        if sigma <= 0:
            sigma = 0.01

    return sigma


# ======================
# NEXT EXPIRY
# ======================

def get_nearest_expiry():

    expiries = sorted(list(set(i["expiry"] for i in NIFTY_OPTIONS)))

    today = datetime.date.today()

    for exp in expiries:

        if exp == today:
            continue

        if exp > today:
            return exp


# ======================
# FIND OPTION SYMBOL
# ======================

def get_option_symbol(strike, expiry, opt_type):

    for ins in NIFTY_OPTIONS:

        if (
            ins["strike"] == strike
            and ins["expiry"] == expiry
            and ins["instrument_type"] == opt_type
        ):
            return ins["tradingsymbol"]

    return None


# ======================
# DELTA SEARCH
# ======================

def find_delta_strike(spot, expiry, signal):

    today = datetime.date.today()
    T = max((expiry - today).days / 365, 0.01)

    atm = int(spot / STRIKE_STEP) * STRIKE_STEP

    if signal == "CALL":
        opt_type = "CE"
        direction = -STRIKE_STEP
    else:
        opt_type = "PE"
        direction = STRIKE_STEP

    strike = atm

    for _ in range(MAX_STEPS):

        strike += direction

        symbol = get_option_symbol(strike, expiry, opt_type)

        if not symbol:
            continue

        ltp_data = kite.ltp([f"NFO:{symbol}"])
        price = ltp_data[f"NFO:{symbol}"]["last_price"]

        iv = implied_volatility(price, spot, strike, T, RISK_FREE_RATE, opt_type)
        delta = abs(bs_delta(spot, strike, T, RISK_FREE_RATE, iv, opt_type))

        if delta >= TARGET_DELTA:
            return symbol

    return None


def select_option(spot, signal):

    expiry = get_nearest_expiry()
    return find_delta_strike(spot, expiry, signal)


# ======================
# ROOT ROUTE (FIXED)
# ======================

@app.route("/")
def root():
    request_token = request.args.get("request_token")

    if request_token:
        try:
            session = kite.generate_session(
                request_token,
                api_secret=API_SECRET
            )

            access_token = session["access_token"]

            print("NEW ACCESS TOKEN:", access_token)

            VPS_URL = "http://168.144.78.119:8000/save_token"

            import requests
            requests.post(VPS_URL, json={"access_token": access_token})

            return "Login successful (forwarded to VPS)"

        except Exception as e:
            return str(e)

    return "Server running"

# ======================
# LOGIN ROUTE
# ======================

@app.route("/login")
def login():
    return redirect(kite.login_url())


# ======================
# WEBHOOK (UNCHANGED)
# ======================

import requests

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        print("Forwarding to VPS:", data)

        VPS_URL = "http://168.144.78.119:8000/webhook"

        response = requests.post(VPS_URL, json=data, timeout=5)

        return jsonify({
            "status": "forwarded",
            "vps_response": response.text
        })

    except Exception as e:
        print("Forward error:", str(e))
        return jsonify({"error": str(e)})
