from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect
import os
from datetime import datetime, timedelta
import math

app = Flask(__name__)

# =========================
# ENV
# =========================

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

kite = KiteConnect(api_key=API_KEY)

ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded from environment.")
else:
    print("No access token found. Login required.")

LOT_SIZE = 65
RISK_FREE_RATE = 0.06
TARGET_DELTA = 0.69


# =========================
# LOGIN ROUTES
# =========================

@app.route("/login")
def login():
    return redirect(kite.login_url())


@app.route("/")
def generate_token():

    global ACCESS_TOKEN

    request_token = request.args.get("request_token")

    if not request_token:
        return "DROJUN DELTA ENGINE READY"

    try:

        data = kite.generate_session(request_token, api_secret=API_SECRET)

        ACCESS_TOKEN = data["access_token"]

        kite.set_access_token(ACCESS_TOKEN)

        print("ACCESS TOKEN GENERATED:", ACCESS_TOKEN)

        return """
        ✅ ACCESS TOKEN GENERATED <br><br>
        Bot is now LIVE for today.<br><br>
        Check Railway logs to copy ACCESS TOKEN and paste into variables.
        """

    except Exception as e:
        print("Token generation error:", str(e))
        return f"Error generating token: {str(e)}"


# =========================
# MATH FUNCTIONS
# =========================

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_delta(S, K, T, r, sigma, option_type):

    if T <= 0 or sigma <= 0:
        return 0

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))

    if option_type == "CE":
        return norm_cdf(d1)
    else:
        return norm_cdf(d1) - 1


# =========================
# EXPIRY
# =========================

def get_next_expiry():

    today = datetime.now().date()
    weekday = today.weekday()

    days_ahead = 3 - weekday

    if days_ahead < 0:
        days_ahead += 7

    expiry = today + timedelta(days=days_ahead)

    if weekday == 3:
        expiry += timedelta(days=7)

    return expiry


def get_time_to_expiry(expiry_date):

    now = datetime.now()

    expiry_datetime = datetime.combine(
        expiry_date,
        datetime.min.time()
    ) + timedelta(hours=15, minutes=30)

    diff = expiry_datetime - now

    return max(diff.total_seconds() / (365 * 24 * 60 * 60), 0.0001)


# =========================
# IV ESTIMATION
# =========================

def get_atm_iv(spot, expiry):

    strike = round(spot / 50) * 50
    expiry_str = expiry.strftime("%d%b").upper()

    tradingsymbol = f"NIFTY{expiry_str}{strike}CE"

    try:

        ltp = kite.ltp([f"NFO:{tradingsymbol}"])

        option_price = ltp[f"NFO:{tradingsymbol}"]["last_price"]

        T = get_time_to_expiry(expiry)

        intrinsic = max(spot - strike, 0)

        time_value = max(option_price - intrinsic, 1)

        approx_iv = math.sqrt(2 * math.pi / T) * (time_value / spot)

        return max(approx_iv, 0.1)

    except:
        return 0.2


# =========================
# STRIKE SELECTION
# =========================

def select_strike_by_delta(spot, signal):

    expiry = get_next_expiry()

    expiry_str = expiry.strftime("%d%b").upper()

    T = get_time_to_expiry(expiry)

    sigma = get_atm_iv(spot, expiry)

    atm = round(spot / 50) * 50

    strikes = range(atm - 1000, atm + 1000, 50)

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


# =========================
# WEBHOOK
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.json

    print("Webhook received:", data)

    if not ACCESS_TOKEN:
        print("No access token available.")
        return jsonify({
            "status": "error",
            "message": "Login required via /login"
        }), 401

    try:

        signal = data.get("signal")

        spot = float(data.get("price"))

        tradingsymbol, delta = select_strike_by_delta(spot, signal)

        if not tradingsymbol:

            return jsonify({
                "status": "error",
                "message": "No suitable strike found"
            })

        if not LIVE_TRADING:

            print("Paper trade:", tradingsymbol, delta)

            return jsonify({
                "status": "paper_mode",
                "tradingsymbol": tradingsymbol,
                "delta": delta
            })

        order_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=LOT_SIZE,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML

        )

        print("Order placed:", order_id)

        return jsonify({
            "status": "order placed",
            "order_id": order_id,
            "tradingsymbol": tradingsymbol,
            "delta": delta
        })

    except Exception as e:

        print("Webhook error:", str(e))

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# =========================
# SERVER START
# =========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
