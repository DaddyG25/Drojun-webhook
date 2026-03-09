import os
import math
import datetime
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
from scipy.stats import norm

# ==============================
# CONFIG
# ==============================

API_KEY = os.environ.get("API_KEY")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65
TARGET_DELTA = 0.69
RISK_FREE_RATE = 0.05
LIVE_TRADING = True

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")
else:
    print("No access token found")

# ==============================
# GET NEXT EXPIRY (THURSDAY)
# ==============================

def get_next_expiry():

    today = datetime.date.today()

    days_ahead = 3 - today.weekday()

    if days_ahead <= 0:
        days_ahead += 7

    expiry = today + datetime.timedelta(days=days_ahead)

    return expiry

# ==============================
# TIME TO EXPIRY
# ==============================

def get_time_to_expiry(expiry):

    now = datetime.datetime.now()

    expiry_datetime = datetime.datetime.combine(
        expiry,
        datetime.time(15, 30)
    )

    seconds = (expiry_datetime - now).total_seconds()

    return max(seconds / (365 * 24 * 60 * 60), 0.00001)

# ==============================
# BLACK SCHOLES DELTA
# ==============================

def bs_delta(S, K, T, r, sigma, option_type):

    if T <= 0:
        return 0

    d1 = (math.log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma * math.sqrt(T))

    if option_type == "CE":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1

# ==============================
# ATM IV APPROXIMATION
# ==============================

def get_atm_iv(spot, expiry):

    try:

        strike = round(spot/50) * 50

        expiry_str = expiry.strftime("%y%b").upper()

        symbol = f"NIFTY{expiry_str}{strike}CE"

        ltp = kite.ltp([f"NFO:{symbol}"])

        option_price = ltp[f"NFO:{symbol}"]["last_price"]

        T = get_time_to_expiry(expiry)

        intrinsic = max(spot - strike, 0)

        time_value = max(option_price - intrinsic, 1)

        approx_iv = math.sqrt(2 * math.pi / T) * (time_value / spot)

        return max(approx_iv, 0.1)

    except:

        return 0.2

# ==============================
# SELECT STRIKE BY DELTA
# ==============================

def select_strike_by_delta(spot, signal):

    expiry = get_next_expiry()

    expiry_str = expiry.strftime("%y%b").upper()

    T = get_time_to_expiry(expiry)

    sigma = get_atm_iv(spot, expiry)

    atm = round(spot/50) * 50

    strikes = range(atm-1000, atm+1000, 50)

    for strike in strikes:

        option_type = "CE" if signal == "CALL" else "PE"

        delta = bs_delta(
            spot,
            strike,
            T,
            RISK_FREE_RATE,
            sigma,
            option_type
        )

        if signal == "CALL" and delta >= TARGET_DELTA:
            return f"NIFTY{expiry_str}{strike}CE", delta

        if signal == "PUT" and delta <= -TARGET_DELTA:
            return f"NIFTY{expiry_str}{strike}PE", delta

    return None, None

# ==============================
# WEBHOOK
# ==============================

@app.route("/webhook", methods=["POST"])

def webhook():

    print("Webhook received:", request.json)

    try:

        data = request.json

        signal = data.get("signal")

        spot = float(data.get("price"))

        symbol, delta = select_strike_by_delta(spot, signal)

        if not symbol:

            return jsonify({
                "status": "error",
                "message": "No strike found"
            })

        if not LIVE_TRADING:

            return jsonify({
                "status": "paper trade",
                "symbol": symbol,
                "delta": delta
            })

        order_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,

            exchange=kite.EXCHANGE_NFO,

            tradingsymbol=symbol,

            transaction_type=kite.TRANSACTION_TYPE_BUY,

            quantity=LOT_SIZE,

            order_type=kite.ORDER_TYPE_MARKET,

            product=kite.PRODUCT_NRML
        )

        return jsonify({

            "status": "order placed",

            "symbol": symbol,

            "delta": delta,

            "order_id": order_id
        })

    except Exception as e:

        print("Webhook error:", str(e))

        return jsonify({

            "status": "error",

            "message": str(e)

        }), 500

# ==============================
# START SERVER
# ==============================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=8080
)
