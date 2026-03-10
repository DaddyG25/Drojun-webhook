import os
import datetime
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

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")

# ======================
# LOGIN ROUTE
# ======================

@app.route("/login")

def login():

    login_url = kite.login_url()

    return redirect(login_url)

# ======================
# TOKEN CALLBACK
# ======================

@app.route("/callback")

def callback():

    try:

        request_token = request.args.get("request_token")

        print("Request token:", request_token)

        session = kite.generate_session(

            request_token,
            api_secret=API_SECRET

        )

        access_token = session["access_token"]

        print("NEW ACCESS TOKEN:", access_token)

        return f"""
        <h2>Login Successful</h2>
        <p>Access token generated.</p>
        <p>Check Railway logs and copy the token.</p>
        """

    except Exception as e:

        print("Token error:", str(e))

        return str(e)

# ======================
# GET NIFTY INSTRUMENTS
# ======================

def get_nifty_options():

    instruments = kite.instruments("NFO")

    nifty = []

    for i in instruments:

        if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT":

            nifty.append(i)

    return nifty

# ======================
# FIND NEAREST EXPIRY
# ======================

def get_nearest_expiry(instruments):

    expiries = sorted(list(set(i["expiry"] for i in instruments)))

    today = datetime.date.today()

    for exp in expiries:

        if exp >= today:
            return exp

# ======================
# SELECT STRIKE
# ======================

def select_option(spot, signal):

    instruments = get_nifty_options()

    expiry = get_nearest_expiry(instruments)

    atm = round(spot/50)*50

    step = ITM_STEPS * 50

    if signal == "CALL":
        strike = atm - step
        opt_type = "CE"
    else:
        strike = atm + step
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
