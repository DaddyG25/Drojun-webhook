from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect
import os
from datetime import datetime

app = Flask(__name__)

# ===== ENV VARIABLES =====
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

# ===== LOT CONFIG =====
LOT_SIZE = 65
LOTS = 1

# ===== KITE INIT =====
kite = KiteConnect(api_key=API_KEY)

ACCESS_TOKEN = None


# ===============================
# LOGIN ROUTE (Daily Token Refresh)
# ===============================
@app.route("/login")
def login():
    login_url = kite.login_url()
    return redirect(login_url)


@app.route("/callback")
def callback():
    global ACCESS_TOKEN

    request_token = request.args.get("request_token")

    if not request_token:
        return "Login failed. No request token."

    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        kite.set_access_token(ACCESS_TOKEN)

        return "✅ Login successful. Access token refreshed for today."

    except Exception as e:
        return f"❌ Error generating access token: {str(e)}"


# ===============================
# WEBHOOK ROUTE
# ===============================
@app.route("/webhook", methods=["POST"])
def webhook():
    global ACCESS_TOKEN

    data = request.json

    signal = data.get("signal")
    symbol = data.get("symbol")
    price = data.get("price")
    tv_time = data.get("time")

    print("\n=== DROJUN LIVE EXECUTION ===")
    print("Signal:", signal)
    print("Price:", price)
    print("Server Time:", datetime.now())
    print("LIVE_TRADING:", LIVE_TRADING)

    if ACCESS_TOKEN is None:
        return jsonify({"status": "error", "message": "Not logged in today"}), 400

    try:
        tradingsymbol = "NIFTY24JANXXXXCE"

        if signal == "PUT":
            tradingsymbol = "NIFTY24JANXXXXPE"

        if not LIVE_TRADING:
            print("⚠ LIVE TRADING DISABLED - Order NOT placed")
            return jsonify({"status": "paper_mode"})

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=LOT_SIZE * LOTS,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )

        print("✅ Order Placed:", order_id)

        return jsonify({"status": "order placed", "order_id": order_id})

    except Exception as e:
        print("❌ Order error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/")
def home():
    return "DROJUN AUTO LOGIN READY"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
