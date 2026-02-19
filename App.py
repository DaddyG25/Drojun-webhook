from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect
import os
from datetime import datetime

app = Flask(__name__)

# ===== Zerodha Credentials =====
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")

kite = KiteConnect(api_key=API_KEY)

# Store token in memory
ACCESS_TOKEN = None

# ===== LOGIN ROUTE =====
@app.route("/login")
def login():
    login_url = kite.login_url()
    return redirect(login_url)


# ===== REDIRECT ROUTE =====
@app.route("/")
def generate_token():
    global ACCESS_TOKEN
    
    request_token = request.args.get("request_token")

    if not request_token:
        return "DROJUN AUTO LOGIN READY"

    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        ACCESS_TOKEN = data["access_token"]
        kite.set_access_token(ACCESS_TOKEN)

        return f"""
        ✅ ACCESS TOKEN GENERATED <br><br>
        Bot is now LIVE.<br><br>
        You can close this page.
        """

    except Exception as e:
        return f"Error generating token: {str(e)}"


# ===== WEBHOOK ROUTE =====
@app.route('/webhook', methods=['POST'])
def webhook():
    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "Login required"}), 401

    data = request.json

    signal = data.get("signal")
    symbol = data.get("symbol")
    price = data.get("price")
    tv_time = data.get("time")

    print("\n=== DROJUN EXECUTION ===")
    print("Signal:", signal)
    print("Symbol:", symbol)
    print("Price:", price)
    print("TV Time:", tv_time)
    print("Server Time:", datetime.now())

    try:
        tradingsymbol = "NIFTY24JANXXXXCE"

        if signal == "PUT":
            tradingsymbol = "NIFTY24JANXXXXPE"

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=65,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )

        return jsonify({
            "status": "order placed",
            "order_id": order_id
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500
