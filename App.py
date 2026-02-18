from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import os
from datetime import datetime

# ===== Live Trading Toggle =====
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

app = Flask(__name__)

# ===== Zerodha API Credentials =====
API_KEY = os.environ.get("API_KEY")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)


@app.route('/webhook', methods=['POST'])
def webhook():
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
        # Temporary dummy symbol (Strike automation next step)
        tradingsymbol = "NIFTY24JANXXXXCE"

        if signal == "PUT":
            tradingsymbol = "NIFTY24JANXXXXPE"

        # ===== SAFETY SWITCH =====
        if not LIVE_TRADING:
            print("⚠ LIVE TRADING DISABLED - Order NOT placed")
            return jsonify({
                "status": "paper_mode",
                "message": "Live trading disabled"
            })

        # ===== Place Real Order =====
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=75,
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


@app.route("/", methods=["GET"])
def home():
    return "DROJUN LIVE EXECUTION READY"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
