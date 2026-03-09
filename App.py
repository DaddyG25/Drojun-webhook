import os
import datetime
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect

# ======================
# CONFIG
# ======================

API_KEY = os.environ.get("API_KEY")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65
TARGET_DELTA_ITM_STEPS = 3   # ~0.65-0.7 delta
LIVE_TRADING = True

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")

# ======================
# GET NEAREST THURSDAY
# ======================

def get_next_expiry():

    today = datetime.date.today()

    days = (3 - today.weekday()) % 7

    expiry = today + datetime.timedelta(days=days)

    return expiry

# ======================
# STRIKE SELECTION
# ======================

def select_strike(spot, signal):

    expiry = get_next_expiry()

    expiry_str = expiry.strftime("%d%b").upper()

    atm = round(spot / 50) * 50

    step = TARGET_DELTA_ITM_STEPS * 50

    if signal == "CALL":

        strike = atm - step
        symbol = f"NIFTY{expiry_str}{strike}CE"

    else:

        strike = atm + step
        symbol = f"NIFTY{expiry_str}{strike}PE"

    return symbol

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

        symbol = select_strike(spot, signal)

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
