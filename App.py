import datetime as dt
import hashlib
import hmac
import logging
import math
import os
import secrets
import threading
import time
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template_string, request, session
from kiteconnect import KiteConnect
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger("drojun")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "sqlite:///drojun.db")
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://") and "+psycopg" not in value:
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


class Base(DeclarativeBase):
    pass


class RuntimeState(Base):
    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


class StrategySettings(Base):
    __tablename__ = "strategy_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cci_length: Mapped[int] = mapped_column(Integer, default=50)
    stop_loss_points: Mapped[float] = mapped_column(Float, default=40.0)
    target_points: Mapped[float] = mapped_column(Float, default=80.0)
    min_delta: Mapped[float] = mapped_column(Float, default=0.69)
    lot_size: Mapped[int] = mapped_column(Integer, default=65)
    entry_offset_points: Mapped[float] = mapped_column(Float, default=5.0)
    trading_enabled: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    side: Mapped[str] = mapped_column(String(4))
    spot: Mapped[float] = mapped_column(Float)
    bar_time: Mapped[str] = mapped_column(String(64))
    cci_length: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ENTRY_PENDING", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


engine = create_engine(database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)


def bootstrap_database() -> None:
    with SessionLocal.begin() as db:
        if db.get(RuntimeState, 1) is None:
            db.add(RuntimeState(id=1))
        if db.get(StrategySettings, 1) is None:
            db.add(StrategySettings(id=1, cci_length=50))
        for row in db.scalars(select(Signal).where(Signal.status == "PROCESSING")):
            row.status = "NEEDS_REVIEW"
            row.error = "Service restarted while this signal was processing; verify the broker before retrying."
        for row in db.scalars(select(Trade).where(Trade.status == "ENTRY_PENDING")):
            row.status = "NEEDS_REVIEW"
            row.error = "Service restarted while the entry was pending; verify the broker order book."


bootstrap_database()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Strict")


def secure_equal(left: str | None, right: str | None) -> bool:
    return bool(left and right and hmac.compare_digest(left.encode(), right.encode()))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        expected_user = os.getenv("ADMIN_USERNAME", "admin")
        expected_password = os.getenv("ADMIN_PASSWORD")
        if not expected_password or not auth or not secure_equal(auth.username, expected_user) or not secure_equal(auth.password, expected_password):
            return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Drojun"'})
        return view(*args, **kwargs)

    return wrapped


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def token_is_current(issued_at: dt.datetime | None) -> bool:
    if issued_at is None:
        return False
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=dt.timezone.utc)
    now = utcnow().astimezone(IST)
    issued = issued_at.astimezone(IST)
    expiry_date = issued.date() + dt.timedelta(days=1)
    expiry = dt.datetime.combine(expiry_date, dt.time(6, 0), tzinfo=IST)
    return issued <= now < expiry


def kite_client(require_token: bool = True) -> KiteConnect:
    api_key = os.getenv("KITE_API_KEY")
    if not api_key:
        raise RuntimeError("KITE_API_KEY is not configured")
    kite = KiteConnect(api_key=api_key)
    if require_token:
        with SessionLocal() as db:
            state = db.get(RuntimeState, 1)
            if state is None or not state.access_token or not token_is_current(state.token_issued_at):
                raise RuntimeError("Zerodha login is required for the current trading day")
            kite.set_access_token(state.access_token)
    return kite


def round_tick(value: float) -> float:
    return round(round(max(value, 0.05) / 0.05) * 0.05, 2)


def in_entry_window(now: dt.datetime | None = None) -> bool:
    current = (now or utcnow()).astimezone(IST)
    if current.weekday() >= 5:
        return False
    return dt.time(9, 15) <= current.time() < dt.time(15, 15)


def norm_cdf(value: float) -> float:
    return (1.0 + math.erf(value / math.sqrt(2.0))) / 2.0


def bs_price(spot: float, strike: float, years: float, rate: float, volatility: float, option_type: str) -> float:
    if years <= 0:
        return max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    if option_type == "CE":
        return spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
    return strike * math.exp(-rate * years) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def implied_volatility(price: float, spot: float, strike: float, years: float, rate: float, option_type: str) -> float | None:
    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    if price <= intrinsic or spot <= 0 or strike <= 0:
        return None
    low, high = 0.01, 5.0
    for _ in range(60):
        middle = (low + high) / 2.0
        estimate = bs_price(spot, strike, years, rate, middle, option_type)
        if abs(estimate - price) < 0.01:
            return middle
        if estimate > price:
            high = middle
        else:
            low = middle
    return (low + high) / 2.0


def option_delta(spot: float, strike: float, years: float, rate: float, volatility: float, option_type: str) -> float:
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * years) / (volatility * math.sqrt(years))
    return norm_cdf(d1) if option_type == "CE" else norm_cdf(d1) - 1.0


_instrument_lock = threading.Lock()
_instrument_cache: tuple[dt.date, list[dict]] | None = None


def nifty_options(kite: KiteConnect) -> list[dict]:
    global _instrument_cache
    today = utcnow().astimezone(IST).date()
    with _instrument_lock:
        if _instrument_cache and _instrument_cache[0] == today:
            return _instrument_cache[1]
        instruments = [
            item for item in kite.instruments("NFO")
            if item.get("name") == "NIFTY" and item.get("segment") == "NFO-OPT"
        ]
        _instrument_cache = (today, instruments)
        return instruments


def choose_contract(kite: KiteConnect, spot: float, side: str, min_delta: float) -> str:
    options = nifty_options(kite)
    today = utcnow().astimezone(IST).date()
    expiries = sorted({item["expiry"] for item in options if item["expiry"] > today})
    if not expiries:
        raise RuntimeError("No non-expiring-today NIFTY option expiry is available")
    expiry = expiries[0]
    option_type = "CE" if side == "CALL" else "PE"
    candidates = [
        item for item in options
        if item["expiry"] == expiry
        and item["instrument_type"] == option_type
        and ((option_type == "CE" and item["strike"] <= spot) or (option_type == "PE" and item["strike"] >= spot))
        and abs(item["strike"] - spot) <= 1000
    ]
    if not candidates:
        raise RuntimeError("No eligible ITM NIFTY option contracts were found")
    symbols = [f'NFO:{item["tradingsymbol"]}' for item in candidates]
    quotes = kite.ltp(symbols)
    years = max((expiry - today).days / 365.0, 1.0 / 365.0)
    eligible: list[tuple[float, float, str]] = []
    for item in candidates:
        symbol = item["tradingsymbol"]
        quote = quotes.get(f"NFO:{symbol}")
        if not quote or float(quote.get("last_price", 0)) <= 0:
            continue
        iv = implied_volatility(float(quote["last_price"]), spot, float(item["strike"]), years, 0.06, option_type)
        if iv is None:
            continue
        delta = abs(option_delta(spot, float(item["strike"]), years, 0.06, iv, option_type))
        if delta >= min_delta:
            eligible.append((delta - min_delta, abs(float(item["strike"]) - spot), symbol))
    if not eligible:
        raise RuntimeError(f"No ITM option met the configured minimum delta of {min_delta:.2f}")
    eligible.sort()
    return eligible[0][2]


def order_by_id(kite: KiteConnect, order_id: str) -> dict | None:
    return next((row for row in kite.orders() if str(row.get("order_id")) == str(order_id)), None)


def wait_for_entry(kite: KiteConnect, order_id: str, timeout_seconds: int = 30) -> dict | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        order = order_by_id(kite, order_id)
        if order and order.get("status") == "COMPLETE":
            return order
        if order and order.get("status") in {"CANCELLED", "REJECTED"}:
            raise RuntimeError(f'Entry order {order.get("status", "failed").lower()}: {order.get("status_message", "")}'.strip())
        time.sleep(1)
    kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
    return None


def place_stop_with_retry(kite: KiteConnect, symbol: str, quantity: int, trigger: float, limit_price: float) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return str(kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=symbol,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=quantity,
                product=kite.PRODUCT_NRML,
                order_type=kite.ORDER_TYPE_SL,
                trigger_price=trigger,
                price=limit_price,
            ))
        except Exception as exc:
            last_error = exc
            time.sleep(attempt + 1)
    raise RuntimeError(f"Protective stop could not be placed after three attempts: {last_error}")


def claim_signal() -> int | None:
    with SessionLocal.begin() as db:
        active = db.scalar(select(Trade.id).where(Trade.status.in_(["ENTRY_PENDING", "ACTIVE"])).limit(1))
        if active is not None:
            return None
        signal = db.scalar(select(Signal).where(Signal.status == "QUEUED").order_by(Signal.id).with_for_update(skip_locked=True).limit(1))
        if signal is None:
            return None
        signal.status = "PROCESSING"
        signal.updated_at = utcnow()
        return signal.id


def process_signal(signal_id: int) -> None:
    with SessionLocal() as db:
        signal = db.get(Signal, signal_id)
        settings = db.get(StrategySettings, 1)
        if signal is None or settings is None:
            return
        created_at = signal.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        max_age = int(os.getenv("SIGNAL_MAX_AGE_SECONDS", "300"))
        if (utcnow() - created_at).total_seconds() > max_age:
            signal.status = "REJECTED"
            signal.error = f"Signal was older than {max_age} seconds"
            db.commit()
            return
        if not settings.trading_enabled:
            signal.status = "REJECTED"
            signal.error = "Trading is disabled"
            db.commit()
            return
        if not in_entry_window():
            signal.status = "REJECTED"
            signal.error = "Signal arrived outside the 09:15-15:15 IST entry window"
            db.commit()
            return
        kite = kite_client()
        symbol = choose_contract(kite, signal.spot, signal.side, settings.min_delta)
        quote = kite.ltp([f"NFO:{symbol}"])[f"NFO:{symbol}"]
        entry_limit = round_tick(float(quote["last_price"]) - settings.entry_offset_points)
        trade = Trade(signal_id=signal.id, symbol=symbol, quantity=settings.lot_size)
        db.add(trade)
        db.commit()
        entry_order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=settings.lot_size,
            product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=entry_limit,
        )
        trade.entry_order_id = str(entry_order_id)
        trade.updated_at = utcnow()
        db.commit()
        filled = wait_for_entry(kite, str(entry_order_id))
        if filled is None:
            trade.status = "ENTRY_CANCELLED"
            signal.status = "COMPLETE"
            signal.error = "Entry was not filled within 30 seconds and was cancelled"
            db.commit()
            return
        entry_price = float(filled["average_price"])
        stop_price = round_tick(entry_price - settings.stop_loss_points)
        stop_limit = round_tick(stop_price - 1.0)
        target_price = round_tick(entry_price + settings.target_points)
        stop_id = place_stop_with_retry(kite, symbol, settings.lot_size, stop_price, stop_limit)
        trade.entry_price = entry_price
        trade.stop_order_id = stop_id
        trade.stop_price = stop_price
        trade.status = "ACTIVE"
        trade.updated_at = utcnow()
        signal.status = "ACTIVE"
        signal.updated_at = utcnow()
        db.commit()
        try:
            target_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=symbol,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=settings.lot_size,
                product=kite.PRODUCT_NRML,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=target_price,
            )
            trade.target_order_id = str(target_id)
            trade.target_price = target_price
            trade.updated_at = utcnow()
            db.commit()
        except Exception as exc:
            trade.error = f"Target order failed; protective stop remains active: {str(exc)[:500]}"
            signal.error = trade.error
            db.commit()


def mark_signal_error(signal_id: int, message: str) -> None:
    safe_message = message[:1000]
    LOG.exception("Signal %s failed: %s", signal_id, safe_message)
    with SessionLocal.begin() as db:
        signal = db.get(Signal, signal_id)
        if signal:
            signal.status = "ERROR"
            signal.error = safe_message
            signal.updated_at = utcnow()
        trade = db.scalar(select(Trade).where(Trade.signal_id == signal_id))
        if trade and trade.status == "ENTRY_PENDING":
            trade.status = "NEEDS_REVIEW"
            trade.error = safe_message
            trade.updated_at = utcnow()


def monitor_trades() -> None:
    with SessionLocal() as db:
        trades = list(db.scalars(select(Trade).where(Trade.status == "ACTIVE")))
    if not trades:
        return
    kite = kite_client()
    orders = {str(row.get("order_id")): row for row in kite.orders()}
    for snapshot in trades:
        stop = orders.get(str(snapshot.stop_order_id))
        target = orders.get(str(snapshot.target_order_id))
        stop_complete = bool(stop and stop.get("status") == "COMPLETE")
        target_complete = bool(target and target.get("status") == "COMPLETE")
        if stop and stop.get("status") in {"CANCELLED", "REJECTED"} and not target_complete:
            with SessionLocal.begin() as db:
                trade = db.get(Trade, snapshot.id)
                signal = db.get(Signal, trade.signal_id) if trade else None
                if trade:
                    trade.status = "NEEDS_REVIEW"
                    trade.error = f'Protective stop is {stop.get("status")}; verify the open position immediately'
                    trade.updated_at = utcnow()
                if signal:
                    signal.status = "NEEDS_REVIEW"
                    signal.error = trade.error
                    signal.updated_at = utcnow()
            continue
        if not stop_complete and not target_complete:
            continue
        if stop_complete and not target_complete and target and target.get("status") not in {"CANCELLED", "REJECTED"}:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=snapshot.target_order_id)
        elif target_complete and not stop_complete and stop and stop.get("status") not in {"CANCELLED", "REJECTED"}:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=snapshot.stop_order_id)
        with SessionLocal.begin() as db:
            trade = db.get(Trade, snapshot.id)
            signal = db.get(Signal, trade.signal_id) if trade else None
            if trade:
                trade.status = "OVER_EXIT" if stop_complete and target_complete else ("STOPPED" if stop_complete else "TARGET_HIT")
                trade.updated_at = utcnow()
            if signal:
                signal.status = "COMPLETE"
                signal.updated_at = utcnow()


def worker_loop() -> None:
    while True:
        try:
            monitor_trades()
            signal_id = claim_signal()
            if signal_id is not None:
                try:
                    process_signal(signal_id)
                except Exception as exc:
                    mark_signal_error(signal_id, str(exc))
        except Exception as exc:
            LOG.error("Worker cycle failed: %s", exc)
        time.sleep(2)


@app.get("/health")
def health():
    with SessionLocal() as db:
        state = db.get(RuntimeState, 1)
        settings = db.get(StrategySettings, 1)
        return jsonify(
            status="ok",
            trading_enabled=bool(settings and settings.trading_enabled),
            kite_login_current=bool(state and token_is_current(state.token_issued_at)),
        )


ADMIN_PAGE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drojun control panel</title><style>
body{font:16px system-ui;max-width:760px;margin:40px auto;padding:0 20px;color:#18212f}form{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:grid;gap:5px}input,select,button{font:inherit;padding:9px}button{grid-column:1/-1}.card{padding:20px;border:1px solid #ccd3dc;border-radius:12px;margin:18px 0}.ok{color:#087830}.bad{color:#b42318}small{color:#56606f}</style></head><body>
<h1>Drojun</h1><div class="card"><b>Kite session:</b> <span class="{{ 'ok' if token_current else 'bad' }}">{{ 'ready' if token_current else 'login required' }}</span><br><br><a href="/kite/login">Log in to Zerodha</a></div>
<div class="card"><h2>Strategy settings</h2><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>CCI length<input type="number" name="cci_length" min="2" max="500" value="{{ s.cci_length }}"></label>
<label>Minimum delta<input type="number" name="min_delta" min="0.01" max="0.99" step="0.01" value="{{ s.min_delta }}"></label>
<label>Stop loss points<input type="number" name="stop_loss_points" min="0.05" step="0.05" value="{{ s.stop_loss_points }}"></label>
<label>Target points<input type="number" name="target_points" min="0.05" step="0.05" value="{{ s.target_points }}"></label>
<label>Quantity<input type="number" name="lot_size" min="1" value="{{ s.lot_size }}"></label>
<label>Entry offset points<input type="number" name="entry_offset_points" min="0" step="0.05" value="{{ s.entry_offset_points }}"></label>
<label>Live trading<select name="trading_enabled"><option value="0" {{ 'selected' if not s.trading_enabled else '' }}>Disabled</option><option value="1" {{ 'selected' if s.trading_enabled else '' }}>Enabled</option></select></label>
<button type="submit">Save settings</button></form><p><small>After changing CCI length, recreate the TradingView alert with the same value.</small></p></div>
<div class="card"><h2>Recent signals</h2><ul>{% for x in signals %}<li>{{ x.created_at }} — {{ x.side }} — {{ x.status }}{% if x.error %}: {{ x.error }}{% endif %}</li>{% else %}<li>No signals yet</li>{% endfor %}</ul></div>
<div class="card"><h2>Recent trades</h2><ul>{% for x in trades %}<li>{{ x.created_at }} — {{ x.symbol }} — {{ x.status }}{% if x.error %}: {{ x.error }}{% endif %}</li>{% else %}<li>No trades yet</li>{% endfor %}</ul></div>
</body></html>
"""


@app.route("/", methods=["GET", "POST"])
@admin_required
def admin():
    with SessionLocal() as db:
        settings = db.get(StrategySettings, 1)
        state = db.get(RuntimeState, 1)
        if request.method == "POST":
            if not secure_equal(request.form.get("csrf"), session.get("csrf")):
                return "Invalid form token", 400
            settings.cci_length = int(request.form["cci_length"])
            settings.min_delta = float(request.form["min_delta"])
            settings.stop_loss_points = float(request.form["stop_loss_points"])
            settings.target_points = float(request.form["target_points"])
            settings.lot_size = int(request.form["lot_size"])
            settings.entry_offset_points = float(request.form["entry_offset_points"])
            settings.trading_enabled = int(request.form["trading_enabled"])
            if not (2 <= settings.cci_length <= 500 and 0.01 <= settings.min_delta <= 0.99 and settings.stop_loss_points > 0 and settings.target_points > 0 and settings.lot_size > 0 and settings.entry_offset_points >= 0):
                return "Invalid settings", 400
            settings.updated_at = utcnow()
            db.commit()
            return redirect("/")
        csrf = secrets.token_urlsafe(24)
        session["csrf"] = csrf
        recent = list(db.scalars(select(Signal).order_by(Signal.id.desc()).limit(10)))
        recent_trades = list(db.scalars(select(Trade).order_by(Trade.id.desc()).limit(10)))
        return render_template_string(ADMIN_PAGE, s=settings, signals=recent, trades=recent_trades, csrf=csrf, token_current=bool(state and token_is_current(state.token_issued_at)))


@app.get("/kite/login")
@admin_required
def kite_login():
    return redirect(kite_client(require_token=False).login_url())


@app.get("/kite/callback")
def kite_callback():
    request_token = request.args.get("request_token")
    if request.args.get("status") != "success" or not request_token:
        return "Kite login did not succeed", 400
    api_secret = os.getenv("KITE_API_SECRET")
    if not api_secret:
        return "KITE_API_SECRET is not configured", 500
    kite = kite_client(require_token=False)
    result = kite.generate_session(request_token, api_secret=api_secret)
    expected_user = os.getenv("KITE_USER_ID")
    if not expected_user:
        return "KITE_USER_ID is not configured", 500
    if not secure_equal(str(result.get("user_id", "")), expected_user):
        return "This Kite account is not authorized for Drojun", 403
    with SessionLocal.begin() as db:
        state = db.get(RuntimeState, 1)
        state.access_token = result["access_token"]
        state.token_issued_at = utcnow()
        state.updated_at = utcnow()
    return redirect("/")


@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}
    if not secure_equal(str(data.get("secret", "")), os.getenv("WEBHOOK_SECRET")):
        return jsonify(error="unauthorized"), 401
    try:
        side = str(data["signal"]).upper()
        spot = float(data["price"])
        cci_length = int(data["cci_length"])
        bar_time = str(data["bar_time"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="signal, price, cci_length and bar_time are required"), 400
    if side not in {"CALL", "PUT"} or spot <= 0 or not bar_time:
        return jsonify(error="invalid signal payload"), 400
    with SessionLocal() as db:
        settings = db.get(StrategySettings, 1)
        if not settings.trading_enabled:
            return jsonify(error="trading is disabled"), 409
        if cci_length != settings.cci_length:
            return jsonify(error=f"CCI length mismatch; expected {settings.cci_length}"), 409
        if not in_entry_window():
            return jsonify(error="outside the 09:15-15:15 IST entry window"), 409
        active_trade = db.scalar(select(Trade.id).where(Trade.status.in_(["ENTRY_PENDING", "ACTIVE"])).limit(1))
        pending_signal = db.scalar(select(Signal.id).where(Signal.status.in_(["QUEUED", "PROCESSING"])).limit(1))
        if active_trade is not None or pending_signal is not None:
            return jsonify(error="another signal or trade is already in progress"), 409
        raw_key = f"{side}|{bar_time}|{cci_length}"
        row = Signal(
            dedupe_key=hashlib.sha256(raw_key.encode()).hexdigest(),
            side=side,
            spot=spot,
            bar_time=bar_time,
            cci_length=cci_length,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return jsonify(status="duplicate_ignored"), 200
        return jsonify(status="queued", signal_id=row.id), 202


_worker_started = False
_worker_start_lock = threading.Lock()


def start_worker_once() -> None:
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=worker_loop, name="drojun-worker", daemon=True)
        thread.start()
        _worker_started = True


start_worker_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
