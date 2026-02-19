from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import os
from datetime import datetime

app = Flask(__name__)

# ===== Environment Variables =====
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

kite = KiteConnect(api_key=API_KEY)

# ===== Generate Access Token Route =====
@app.route("/generate_token")
def generate_token():
    request_token = request.args.get("request_token")

    if not request_token:
        login_url = kite.login_url()
        return f'<a href="{login_url}">Click here to login to Zerodha</a>'

    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = data["access_token"]

        return f"""
        ✅ Access Token Generated Successfully <br><br>
        Copy this token and paste into Railway ACCESS_TOKEN variable:<br><br>
        <b>{access_token}</b>
        """

    except Exception as e:
        return f"Error generating token: {str(e)}"


# ===== Webhook Route =====
@app.route('/webhook', methods=['POST'])
def webhook():
    kite.set_access_token(ACCESS_TOKEN)

    data = request.json

    signal = data.get("signal")
    symbol = data.get("symbol")
    price = data.get("price")
    tv_time = data.get("time")

    print("\n=== DROJUN LIVE EXECUTION ===")
    print("Signal:", signal)
    print("Symbol:", symbol)
    print("Price:", price)
    print("TV Time:", tv_time)
    print("Server Time:", datetime.now())
    print("LIVE_TRADING:", LIVE_TRADING)

    try:
        tradingsymbol = "NIFTY24JANXXXXCE"

        if signal == "PUT":
            tradingsymbol = "NIFTY24JANXXXXPE"

        if not LIVE_TRADING:
            print("⚠ LIVE TRADING DISABLED - Order NOT placed")
            return jsonify({
                "status": "paper_mode",
                "message": "Live trading disabled"
            })

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=65,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )

        print("✅ Order Placed. ID:", order_id)

        return jsonify({
            "status": "order placed",
            "order_id": order_id
        })

    except Exception as e:
        print("❌ Error placing order:", e)
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/")
def home():
    return "DROJUN AUTO LOGIN READY"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
