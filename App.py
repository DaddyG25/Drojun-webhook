import os
import datetime
import math
from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect

# ======================
# CONFIG
# ======================

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65
ITM_STEPS = 3
LIVE_TRADING = True

TARGET_DELTA = 0.69
RISK_FREE_RATE = 0.06
VOLATILITY = 0.18

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")

# ======================
# BLACK SCHOLES DELTA
# ======================

def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def option_delta(S, K, T, r, sigma, option_type):

    if T <= 0:
        return 0.5

    d1 = (
        math.log(S / K)
        + (r + 0.5 * sigma ** 2) * T
    ) / (sigma * math.sqrt(T))

    if option_type == "CE":
        return norm_cdf(d1)
    else:
        return norm_cdf(d1) - 1


# ======================
# ROOT ROUTE
# ======================

@app.route("/")
def root():

    request_token = request.args.get("request_token")

    if request_token:

        print("Request token:", request_token)

        try:

            session = kite.generate_session(
                request_token,
                api_secret=API_SECRET
            )

            access_token = session["access_token"]

            print("NEW ACCESS TOKEN:", access_token)

            return """
            <h2>Login Successful</h2>
            <p>Access token generated.</p>
            <p>Check Railway logs and copy the token.</p>
            """

        except Exception as e:

            print("Token error:", str(e))
            return str(e)

    return "Server running"


# ======================
# LOGIN ROUTE
# ======================

@app.route("/login")
def login():

    login_url = kite.login_url()

    return redirect(login_url)


# ======================
# GET NIFTY OPTIONS
# ======================

def get_nifty_options():

    instruments = kite.instruments("NFO")

    nifty = []

    for i in instruments:

        if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT":

            nifty.append(i)

    return nifty


# ======================
# EXPIRY (SKIP 0DTE)
# ======================

def get_nearest_expiry(instruments):

    expiries = sorted(list(set(i["expiry"] for i in instruments)))

    today = datetime.date.today()

    for exp in expiries:

        if exp == today:
            continue

        if exp > today:
            return exp


# ======================
# DELTA BASED STRIKE
# ======================

def find_delta_strike(instruments, spot, expiry, signal):

    today = datetime.date.today()

    days = (expiry - today).days
    T = max(days / 365, 0.01)

    strikes = sorted(list(set(i["strike"] for i in instruments)))

    if signal == "CALL":
        strikes = [s for s in strikes if s <= spot]
        strikes = sorted(strikes, reverse=True)
        opt_type = "CE"
    else:
        strikes = [s for s in strikes if s >= spot]
        strikes = sorted(strikes)
        opt_type = "PE"

    for strike in strikes[:20]:

        delta = option_delta(
            spot,
            strike,
            T,
            RISK_FREE_RATE,
            VOLATILITY,
            opt_type
        )

        if signal == "CALL" and delta >= TARGET_DELTA:
            return strike

        if signal == "PUT" and abs(delta) >= TARGET_DELTA:
            return strike

    # fallback to ITM method
    atm = int(spot / 50) * 50
    step = ITM_STEPS * 50

    if signal == "CALL":
        return atm - step
    else:
        return atm + step


# ======================
# SELECT OPTION
# ======================

def select_option(spot, signal):

    instruments = get_nifty_options()

    expiry = get_nearest_expiry(instruments)

    strike = find_delta_strike(
        instruments,
        spot,
        expiry,
        signal
    )

    if signal == "CALL":
        opt_type = "CE"
    else:
        opt_type = "PE"

    for i in instruments:

        if (
            i["strike"] == strike
            and i["expiry"] == expiry
            and i["instrument_type"] == opt_type
        ):

            return i["tradingsymbol"]


# ======================
# WEBHOOK
# ======================

@app.route("/webhook", methods=["POST"])

def webhook():

    try:

        data = request.json

        print("Webhook received:", data)

        signal = data["signal"]

        spot = float(data["price"])

        symbol = select_option(spot, signal)

        print("Selected symbol:", symbol)

        ltp_data = kite.ltp([f"NFO:{symbol}"])

        ltp = ltp_data[f"NFO:{symbol}"]["last_price"]

        limit_price = max(ltp - 5, 0.5)

        print("Option LTP:", ltp)
        print("Limit order:", limit_price)

        if not LIVE_TRADING:

            return jsonify({"paper_trade": symbol})

        order_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=LOT_SIZE,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=limit_price,
            product=kite.PRODUCT_NRML

        )

        return jsonify({

            "status": "order placed",
            "symbol": symbol,
            "order_id": order_id

        })

    except Exception as e:

        print("Webhook error:", str(e))

        return jsonify({"error": str(e)})


# ======================
# SERVER
# ======================

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=8080)
