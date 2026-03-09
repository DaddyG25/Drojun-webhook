import os
import math
import datetime
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
from scipy.stats import norm

# =========================
# CONFIG
# =========================

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65
TARGET_DELTA = 0.69
RISK_FREE_RATE = 0.05
LIVE_TRADING = True

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")
else:
    print("No access token found")


# =========================
# EXPIRY CALCULATION
# =========================

def get_next_expiry():

    today = datetime.date.today()

    days_ahead = 3 - today.weekday()

    if days_ahead <= 0:
        days_ahead += 7

    return today + datetime.timedelta(days_ahead)


def get_time_to_expiry(expiry):

    now = datetime.datetime.now()

    expiry_dt = datetime.datetime.combine(
        expiry,
        datetime.time(15,30)
    )

    diff = expiry_dt - now

    return max(diff.total_seconds()/(365*24*3600),0.0001)


# =========================
# BLACK SCHOLES DELTA
# =========================

def bs_delta(S,K,T,r,sigma,option_type):

    d1 = (
        math.log(S/K)
        + (r + sigma**2 / 2)*T
    )/(sigma*math.sqrt(T))

    if option_type == "CE":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1


# =========================
# IV ESTIMATION
# =========================

def get_atm_iv(spot, expiry):

    strike = round(spot/50)*50

    year = expiry.strftime("%y")
    month = str(expiry.month)
    day = expiry.strftime("%d")

    expiry_str = f"{year}{month}{day}"

    tradingsymbol = f"NIFTY{expiry_str}{strike}CE"

    try:

        ltp = kite.ltp([f"NFO:{tradingsymbol}"])

        option_price = ltp[f"NFO:{tradingsymbol}"]["last_price"]

        T = get_time_to_expiry(expiry)

        intrinsic = max(spot - strike,0)

        time_value = max(option_price - intrinsic,1)

        approx_iv = math.sqrt(2*math.pi/T)*(time_value/spot)

        return max(approx_iv,0.1)

    except:

        return 0.2


# =========================
# DELTA STRIKE SELECTION
# =========================

def select_strike_by_delta(spot, signal):

    expiry = get_next_expiry()

    year = expiry.strftime("%y")
    month = str(expiry.month)
    day = expiry.strftime("%d")

    expiry_str = f"{year}{month}{day}"

    T = get_time_to_expiry(expiry)

    sigma = get_atm_iv(spot,expiry)

    atm = round(spot/50)*50

    strikes = list(range(atm-1000,atm+1000,50))

    best_strike = None
    best_delta = None
    best_diff = 999

    for strike in strikes:

        option_type = "CE" if signal=="CALL" else "PE"

        delta = bs_delta(
            spot,
            strike,
            T,
            RISK_FREE_RATE,
            sigma,
            option_type
        )

        abs_delta = abs(delta)

        if abs_delta >= TARGET_DELTA:

            diff = abs(abs_delta - TARGET_DELTA)

            if diff < best_diff:

                best_diff = diff
                best_strike = strike
                best_delta = delta

    if not best_strike:
        return None,None

    if signal=="CALL":
        symbol = f"NIFTY{expiry_str}{best_strike}CE"
    else:
        symbol = f"NIFTY{expiry_str}{best_strike}PE"

    return symbol,best_delta


# =========================
# WEBHOOK
# =========================

@app.route("/webhook",methods=["POST"])
def webhook():

    print("Webhook received:",request.json)

    if not ACCESS_TOKEN:

        return jsonify({
            "status":"error",
            "message":"Login required"
        }),401

    try:

        data = request.json

        signal = data.get("signal")

        spot = float(data.get("price"))

        tradingsymbol,delta = select_strike_by_delta(
            spot,
            signal
        )

        if not tradingsymbol:

            return jsonify({
                "status":"error",
                "message":"No strike found"
            })

        print("Selected:",tradingsymbol,"Delta:",delta)

        if not LIVE_TRADING:

            return jsonify({
                "status":"paper",
                "symbol":tradingsymbol,
                "delta":delta
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

            "status":"order placed",

            "order_id":order_id,

            "symbol":tradingsymbol,

            "delta":delta

        })

    except Exception as e:

        print("Webhook error:",str(e))

        return jsonify({

            "status":"error",

            "message":str(e)

        }),500


# =========================
# SERVER START
# =========================

if _name=="main_":

    app.run(host="0.0.0.0",port=8080)
