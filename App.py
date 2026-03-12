import os
import datetime
import math
import time
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect

# ======================
# CONFIG
# ======================

API_KEY = os.environ.get("API_KEY")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65

TARGET_DELTA = 0.70
STRIKE_STEP = 50
MAX_STEPS = 15

STOP_LOSS_POINTS = 40
TARGET_POINTS = 80

LIVE_TRADING = True

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

kite.set_access_token(ACCESS_TOKEN)

# ======================
# INSTRUMENT CACHE
# ======================

NIFTY_OPTIONS = []

def load_instruments():

    global NIFTY_OPTIONS

    print("Downloading instruments...")

    instruments = kite.instruments("NFO")

    NIFTY_OPTIONS = [
        i for i in instruments
        if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"
    ]

    print("Loaded:", len(NIFTY_OPTIONS))


load_instruments()

# ======================
# NORMAL CDF
# ======================

def norm_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


# ======================
# DELTA
# ======================

def bs_delta(S, K, T, r, sigma, option_type):

    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))

    if option_type == "CE":
        return norm_cdf(d1)
    else:
        return norm_cdf(d1)-1


# ======================
# EXPIRY
# ======================

def get_nearest_expiry():

    expiries = sorted(list(set(i["expiry"] for i in NIFTY_OPTIONS)))

    today = datetime.date.today()

    for exp in expiries:

        if exp == today:
            continue

        if exp > today:
            return exp


# ======================
# FIND OPTION SYMBOL
# ======================

def get_option_symbol(strike, expiry, opt_type):

    for ins in NIFTY_OPTIONS:

        if (
            ins["strike"] == strike
            and ins["expiry"] == expiry
            and ins["instrument_type"] == opt_type
        ):
            return ins["tradingsymbol"]

    return None


# ======================
# DELTA STRIKE SEARCH
# ======================

def find_delta_strike(spot, expiry, signal):

    atm = int(spot / STRIKE_STEP) * STRIKE_STEP

    if signal == "CALL":
        opt_type = "CE"
        direction = -STRIKE_STEP
    else:
        opt_type = "PE"
        direction = STRIKE_STEP

    strike = atm

    for _ in range(MAX_STEPS):

        strike += direction

        symbol = get_option_symbol(strike, expiry, opt_type)

        if not symbol:
            continue

        ltp = kite.ltp([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]

        delta = 0.7  # simplified assumption

        if delta >= TARGET_DELTA:
            return symbol

    return None


# ======================
# OCO MONITOR
# ======================

def monitor_exit(target_order, sl_order):

    while True:

        orders = kite.orders()

        target_status = None
        sl_status = None

        for o in orders:

            if o["order_id"] == target_order:
                target_status = o["status"]

            if o["order_id"] == sl_order:
                sl_status = o["status"]

        if target_status == "COMPLETE":

            kite.cancel_order(
                variety=kite.VARIETY_REGULAR,
                order_id=sl_order
            )

            print("Target hit, SL cancelled")

            break

        if sl_status == "COMPLETE":

            kite.cancel_order(
                variety=kite.VARIETY_REGULAR,
                order_id=target_order
            )

            print("SL hit, target cancelled")

            break

        time.sleep(2)


# ======================
# WEBHOOK
# ======================

@app.route("/webhook", methods=["POST"])
def webhook():

    try:

        data = request.json

        signal = data["signal"]
        spot = float(data["price"])

        expiry = get_nearest_expiry()

        symbol = find_delta_strike(spot, expiry, signal)

        ltp = kite.ltp([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]

        entry = round(ltp - 5, 1)

        # ENTRY
        entry_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=LOT_SIZE,
            order_type="LIMIT",
            price=entry,
            product="NRML"

        )

        time.sleep(2)

        target = entry + TARGET_POINTS
        sl = entry - STOP_LOSS_POINTS

        # TARGET
        target_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type="SELL",
            quantity=LOT_SIZE,
            order_type="LIMIT",
            price=target,
            product="NRML"

        )

        # STOPLOSS
        sl_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type="SELL",
            quantity=LOT_SIZE,
            order_type="SL",
            trigger_price=sl,
            price=sl-1,
            product="NRML"

        )

        monitor_exit(target_id, sl_id)

        return jsonify({"status": "trade placed"})

    except Exception as e:

        return jsonify({"error": str(e)})
