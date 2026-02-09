from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    signal = data.get("signal")   # CALL or PUT
    symbol = data.get("symbol")   # NIFTY
    price  = data.get("price")
    time   = data.get("time")

    print("\n=== DROJUN PAPER TRADE ===")
    print("Time       :", datetime.now())
    print("Signal     :", signal)
    print("Symbol     :", symbol)
    print("Price      :", price)
    print("TV Time    :", time)

    # ---- PAPER LOGIC ----
    if signal == "CALL":
        trade = {
            "action": "BUY_CALL",
            "strike": "ITM (Δ ≥ 0.7)",
            "lot": 1,
            "sl": "Entry - 40 points",
            "target": "Entry + 80 points"
        }
    elif signal == "PUT":
        trade = {
            "action": "BUY_PUT",
            "strike": "ITM (Δ ≥ 0.7)",
            "lot": 1,
            "sl": "Entry - 40 points",
            "target": "Entry + 80 points"
        }
    else:
        trade = {"error": "Unknown signal"}

    print("Paper Trade :", trade)
    print("==========================")

    return jsonify({"status": "paper_trade_logged"}), 200

@app.route("/", methods=["GET"])
def home():
    return "DROJUN WEBHOOK LIVE"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
