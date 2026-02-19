from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import os
from datetime import datetime, timedelta
import math

app = Flask(__name__)

# =========================
# ENV
# =========================
API_KEY = os.environ.get("API_KEY")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

LOT_SIZE = 65
RISK_FREE_RATE = 0.06  # 6% annual assumption
TARGET_DELTA = 0.69

# =========================
# UTILITIES
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

def get_next_expiry():
    today = datetime.now().date()
    weekday = today.weekday()  # Monday=0, Thursday=3
    days_ahead = 3 - weekday
    if days_ahead < 0:
        days_ahead += 7
    expiry = today + timedelta(days=days_ahead)
    if weekday == 3:
        expiry += timedelta(days=7)
    return expiry

def get_time_to_expiry(expiry_date):
    now = datetime.now()
    expiry_datetime = datetime.combine(expiry_date, datetime.min.time()) + timedelta(hours=15, minutes=30)
    diff = expiry_datetime - now
    return max(diff.total_seconds() / (365 * 24 * 60 * 60), 0.0001)

def get_atm_iv(spot, expiry):
    # Use ATM CE price to approximate IV
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
        return 0.2  # fallback IV

# =========================
# DELTA STRIKE SELECTION
# =========================
def select_strike_by_delta(spot, signal):
    expiry = get_next_expiry()
    expiry_str = expiry.strftime("%d%b").upper()
    T = get_time_to_expiry(expiry)
    sigma = get_atm_iv(spot, expiry)

    # Scan strikes ±1000 around ATM
    atm = round(spot / 50) * 50
    strikes = range(atm - 1000, atm + 1000, 50)

    best_symbol = None
    best_delta = None

    for strike in strikes:
        option_type = "CE" if signal == "CALL" else "PE"
        delta = bs_delta(spot, strike, T, RISK_FREE_RATE, sigma, option_type)

        if signal == "CALL" and delta >= TARGET_DELTA:
            best_symbol = f"NIFTY{expiry_str}{strike}CE"
            best_delta = delta
            break

        if signal == "PUT" and delta <= -TARGET_DELTA:
            best_symbol = f"NIFTY{expiry_str}{strike}PE"
            best_delta = delta
            break

    return best_symbol, best_delta

# =========================
# WEBHOOK
# =========================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    signal = data.get("signal")
    spot = float(data.get("price"))

    print("\n=== DROJUN DELTA EXECUTION ===")
    print("Signal:", signal)
    print("Spot:", spot)
    print("LIVE_TRADING:", LIVE_TRADING)

    try:
        tradingsymbol, delta = select_strike_by_delta(spot, signal)

        if not tradingsymbol:
            return jsonify({"status": "error", "message": "No suitable strike found"})

        print("Selected:", tradingsymbol, "Delta:", delta)

        if not LIVE_TRADING:
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

        return jsonify({
            "status": "order placed",
            "order_id": order_id,
            "tradingsymbol": tradingsymbol,
            "delta": delta
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/")
def home():
    return "DROJUN DELTA ENGINE READY"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
