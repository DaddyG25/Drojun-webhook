import os
import datetime
import math
from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect

# ======================
# CONFIG
# ======================

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

LOT_SIZE = 65
LIVE_TRADING = True

TARGET_DELTA = 0.70
RISK_FREE_RATE = 0.06
STRIKE_STEP = 50
SCAN_RANGE = 10

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")

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

    print("NIFTY options loaded:", len(NIFTY_OPTIONS))


# preload instruments immediately
load_instruments()

# ======================
# NORMAL DISTRIBUTION
# ======================

def norm_cdf(x):
    return (1 + math.erf(x / math.sqrt(2))) / 2


# ======================
# BLACK SCHOLES PRICE
# ======================

def bs_price(S, K, T, r, sigma, option_type):

    if T <= 0:
        return max(0, S-K) if option_type == "CE" else max(0, K-S)

    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)

    if option_type == "CE":
        return S*norm_cdf(d1)-K*math.exp(-r*T)*norm_cdf(d2)
    else:
        return K*math.exp(-r*T)*norm_cdf(-d2)-S*norm_cdf(-d1)


# ======================
# BLACK SCHOLES DELTA
# ======================

def bs_delta(S, K, T, r, sigma, option_type):

    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))

    if option_type == "CE":
        return norm_cdf(d1)
    else:
        return norm_cdf(d1)-1


# ======================
# IMPLIED VOLATILITY
# ======================

def implied_volatility(price, S, K, T, r, option_type):

    sigma = 0.3

    for _ in range(20):

        price_est = bs_price(S, K, T, r, sigma, option_type)

        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))

        vega = S * math.sqrt(T) * (1/math.sqrt(2*math.pi)) * math.exp(-0.5*d1*d1)

        if vega == 0:
            break

        sigma = sigma - (price_est-price)/vega

        if sigma <= 0:
            sigma = 0.01

    return sigma


# ======================
# FIND NEAREST EXPIRY
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
# FIND DELTA STRIKE
# ======================

def find_delta_strike(spot, expiry, signal):

    today = datetime.date.today()

    T = max((expiry - today).days / 365, 0.01)

    atm = int(spot / STRIKE_STEP) * STRIKE_STEP

    if signal == "CALL":
        opt_type = "CE"
        strikes = [atm - (i * STRIKE_STEP) for i in range(1, SCAN_RANGE)]
    else:
        opt_type = "PE"
        strikes = [atm + (i * STRIKE_STEP) for i in range(1, SCAN_RANGE)]

    symbols = []
    strike_map = {}

    for s in strikes:

        for ins in NIFTY_OPTIONS:

            if ins["strike"] == s and ins["expiry"] == expiry and ins["instrument_type"] == opt_type:

                sym = "NFO:" + ins["tradingsymbol"]

                symbols.append(sym)
                strike_map[sym] = s

    if not symbols:
        return None

    ltp_data = kite.ltp(symbols)

    selected = None
    best_delta = 0

    for sym in symbols:

        price = ltp_data[sym]["last_price"]

        strike = strike_map[sym]

        iv = implied_volatility(price, spot, strike, T, RISK_FREE_RATE, opt_type)

        delta = abs(bs_delta(spot, strike, T, RISK_FREE_RATE, iv, opt_type))

        if delta >= TARGET_DELTA and delta > best_delta:

            best_delta = delta
            selected = sym

    if selected:
        return selected.split(":")[1]

    # fallback: choose highest delta if none >= target
    best_sym = None
    best_delta = 0

    for sym in symbols:

        price = ltp_data[sym]["last_price"]
        strike = strike_map[sym]

        iv = implied_volatility(price, spot, strike, T, RISK_FREE_RATE, opt_type)
        delta = abs(bs_delta(spot, strike, T, RISK_FREE_RATE, iv, opt_type))

        if delta > best_delta:

            best_delta = delta
            best_sym = sym

    return best_sym.split(":")[1]


# ======================
# SELECT OPTION
# ======================

def select_option(spot, signal):

    expiry = get_nearest_expiry()

    symbol = find_delta_strike(spot, expiry, signal)

    return symbol


# ======================
# ROOT ROUTE
# ======================

@app.route("/")
def root():

    request_token = request.args.get("request_token")

    if request_token:

        try:

            session = kite.generate_session(
                request_token,
                api_secret=API_SECRET
            )

            access_token = session["access_token"]

            print("NEW ACCESS TOKEN:", access_token)

            return "Login successful"

        except Exception as e:

            print("Token error:", str(e))
            return str(e)

    return "Server running"


# ======================
# LOGIN
# ======================

@app.route("/login")
def login():

    return redirect(kite.login_url())


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
