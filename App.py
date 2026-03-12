import os
import datetime
import math
import gspread
from oauth2client.service_account import ServiceAccountCredentials
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
MAX_STEPS = 15

SL_POINTS = 40
TARGET_POINTS = 80

SPREADSHEET_ID = "1Sv7ir2VaGVbcSqYSQ1JZes6fiINdYfTiMtxQGyLp2Ug"

app = Flask(__name__)

kite = KiteConnect(api_key=API_KEY)

if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)
    print("Access token loaded")

# ======================
# GOOGLE JOURNAL
# ======================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name(
    "service_account.json", scope
)

client = gspread.authorize(creds)

sheet = client.open_by_key(SPREADSHEET_ID).sheet1

print("Trading journal connected")

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

    sigma = 0.30

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
# NEXT EXPIRY
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

    today = datetime.date.today()
    T = max((expiry - today).days / 365, 0.01)

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

        ltp_data = kite.ltp([f"NFO:{symbol}"])

        price = ltp_data[f"NFO:{symbol}"]["last_price"]

        iv = implied_volatility(price, spot, strike, T, RISK_FREE_RATE, opt_type)

        delta = abs(bs_delta(spot, strike, T, RISK_FREE_RATE, iv, opt_type))

        if delta >= TARGET_DELTA:

            return symbol

    return None

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

        session = kite.generate_session(
            request_token,
            api_secret=API_SECRET
        )

        access_token = session["access_token"]

        print("NEW ACCESS TOKEN:", access_token)

        return "Login successful"

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

        entry_price = max(ltp - 5, 0.5)

        sl_price = entry_price - SL_POINTS
        target_price = entry_price + TARGET_POINTS

        print("Entry:", entry_price)
        print("SL:", sl_price)
        print("Target:", target_price)

        if not LIVE_TRADING:
            return jsonify({"paper_trade": symbol})

        order_id = kite.place_order(

            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=LOT_SIZE,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=entry_price,
            product=kite.PRODUCT_NRML

        )

        # STOP LOSS
        kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=LOT_SIZE,
            order_type=kite.ORDER_TYPE_SL,
            price=sl_price,
            trigger_price=sl_price,
            product=kite.PRODUCT_NRML
        )

        # TARGET
        kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=LOT_SIZE,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=target_price,
            product=kite.PRODUCT_NRML
        )

        # JOURNAL ENTRY
        sheet.append_row([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            signal,
            symbol,
            entry_price,
            sl_price,
            target_price
        ])

        return jsonify({
            "status": "order placed",
            "symbol": symbol,
            "entry": entry_price,
            "sl": sl_price,
            "target": target_price
        })

    except Exception as e:

        print("Webhook error:", str(e))

        return jsonify({"error": str(e)})
