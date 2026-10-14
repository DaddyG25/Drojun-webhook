import datetime as dt
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import secrets
import threading
import time
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session
from kiteconnect import KiteConnect
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


IST = ZoneInfo("Asia/Kolkata")
LOG = logging.getLogger("drojun")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
BLOCKING_TRADE_STATUSES = ("ENTRY_PENDING", "ACTIVE", "NEEDS_REVIEW")
BLOCKING_SIGNAL_STATUSES = ("QUEUED", "PROCESSING", "NEEDS_REVIEW")
OPEN_PAPER_STATUSES = ("OPEN",)
EXECUTION_DISABLED = 0
EXECUTION_LIVE = 1
EXECUTION_PAPER = 2


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
    gtt_trigger_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ENTRY_PENDING", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), unique=True)
    side: Mapped[str] = mapped_column(String(4))
    symbol: Mapped[str] = mapped_column(String(64))
    option_type: Mapped[str] = mapped_column(String(2))
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    entry_spot: Mapped[float] = mapped_column(Float)
    entry_delta: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    last_spot: Mapped[float] = mapped_column(Float)
    last_price: Mapped[float] = mapped_column(Float)
    exit_spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="OPEN", index=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


def gateway_signature(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    payload = f"{timestamp}\n{method.upper()}\n{path}\n".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@app.before_request
def require_gateway_signature():
    if request.path == "/health" or os.getenv("REQUIRE_GATEWAY_SIGNATURE", "true").lower() != "true":
        return None
    shared_secret = os.getenv("GATEWAY_SHARED_SECRET")
    timestamp = request.headers.get("X-Drojun-Gateway-Timestamp", "")
    provided = request.headers.get("X-Drojun-Gateway-Signature", "")
    try:
        timestamp_number = int(timestamp)
    except ValueError:
        return jsonify(error="invalid gateway signature"), 401
    if not shared_secret or abs(int(time.time()) - timestamp_number) > 30:
        return jsonify(error="invalid gateway signature"), 401
    path = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
    expected = gateway_signature(shared_secret, timestamp, request.method, path, request.get_data(cache=True))
    if not secure_equal(provided, expected):
        return jsonify(error="invalid gateway signature"), 401
    return None


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


def format_ist(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    aware = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return aware.astimezone(IST).strftime("%Y-%m-%d %H:%M")


def paper_pnl(trade: PaperTrade) -> float:
    mark = trade.exit_price if trade.exit_price is not None else trade.last_price
    return (mark - trade.entry_price) * trade.quantity


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


_kite_credentials_lock = threading.Lock()


def kite_credentials() -> tuple[str | None, str | None]:
    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    path = Path(os.getenv("KITE_CREDENTIALS_FILE", "/var/lib/drojun/kite_credentials.json"))
    with _kite_credentials_lock:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved = {}
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Kite credentials file could not be read: {exc}") from exc
    return saved.get("api_key") or api_key, saved.get("api_secret") or api_secret


def save_kite_credentials(api_key: str, api_secret: str) -> None:
    if not (len(api_key) == 16 and api_key.isalnum() and len(api_secret) == 32 and api_secret.isalnum()):
        raise ValueError("Kite API key or secret has an unexpected format")
    path = Path(os.getenv("KITE_CREDENTIALS_FILE", "/var/lib/drojun/kite_credentials.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    payload = json.dumps({"api_key": api_key, "api_secret": api_secret})
    with _kite_credentials_lock:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)


def kite_client(require_token: bool = True) -> KiteConnect:
    api_key, _ = kite_credentials()
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


def option_delta(spot: float, strike: float, years: float, rate: float, volatility: float, option_type: str) -> float:
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * years) / (volatility * math.sqrt(years))
    return norm_cdf(d1) if option_type == "CE" else norm_cdf(d1) - 1.0


def option_price(spot: float, strike: float, years: float, rate: float, volatility: float, option_type: str) -> float:
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * years) / (volatility * math.sqrt(years))
    d2 = d1 - volatility * math.sqrt(years)
    discounted_strike = strike * math.exp(-rate * years)
    if option_type == "CE":
        return spot * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
    return discounted_strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


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


def choose_contract_details(kite: KiteConnect, spot: float, side: str, min_delta: float) -> dict:
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
    now = utcnow().astimezone(IST)
    expiry_at = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=IST)
    years = max((expiry_at - now).total_seconds() / (365.0 * 24 * 60 * 60), 1.0 / (365.0 * 24))
    estimated_iv = float(os.getenv("ESTIMATED_IV", "0.18"))
    if not 0.01 <= estimated_iv <= 5.0:
        raise RuntimeError("ESTIMATED_IV must be between 0.01 and 5.0")
    eligible: list[tuple[float, float, dict, float]] = []
    for item in candidates:
        delta = abs(option_delta(spot, float(item["strike"]), years, 0.06, estimated_iv, option_type))
        if delta >= min_delta:
            eligible.append((delta - min_delta, abs(float(item["strike"]) - spot), item, delta))
    if not eligible:
        raise RuntimeError(f"No ITM option met the configured minimum delta of {min_delta:.2f}")
    eligible.sort(key=lambda row: (row[0], row[1], str(row[2]["tradingsymbol"])))
    item = eligible[0][2]
    delta = eligible[0][3]
    premium = round_tick(option_price(spot, float(item["strike"]), years, 0.06, estimated_iv, option_type))
    return {
        "symbol": str(item["tradingsymbol"]),
        "strike": float(item["strike"]),
        "expiry": expiry,
        "option_type": option_type,
        "delta": delta,
        "estimated_price": premium,
    }


def choose_contract(kite: KiteConnect, spot: float, side: str, min_delta: float) -> str:
    return str(choose_contract_details(kite, spot, side, min_delta)["symbol"])


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


def place_exit_gtt_with_retry(
    kite: KiteConnect,
    symbol: str,
    quantity: int,
    last_price: float,
    stop_trigger: float,
    stop_limit: float,
    target_price: float,
) -> str:
    orders = [
        {
            "transaction_type": kite.TRANSACTION_TYPE_SELL,
            "quantity": quantity,
            "order_type": kite.ORDER_TYPE_LIMIT,
            "product": kite.PRODUCT_NRML,
            "price": stop_limit,
        },
        {
            "transaction_type": kite.TRANSACTION_TYPE_SELL,
            "quantity": quantity,
            "order_type": kite.ORDER_TYPE_LIMIT,
            "product": kite.PRODUCT_NRML,
            "price": target_price,
        },
    ]
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            result = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_OCO,
                tradingsymbol=symbol,
                exchange=kite.EXCHANGE_NFO,
                trigger_values=[stop_trigger, target_price],
                last_price=last_price,
                orders=orders,
            )
            trigger_id = result.get("trigger_id") if isinstance(result, dict) else result
            if trigger_id is None:
                raise RuntimeError("Kite did not return a GTT trigger ID")
            return str(trigger_id)
        except Exception as exc:
            last_error = exc
            time.sleep(attempt + 1)
    raise RuntimeError(f"GTT OCO exit could not be placed after three attempts: {last_error}")


def parse_bar_time(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000.0, tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return utcnow()
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def paper_mark_price(trade: PaperTrade, spot: float, marked_at: dt.datetime) -> float:
    expiry = dt.date.fromisoformat(trade.expiry)
    expiry_at = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=IST)
    current = marked_at.astimezone(IST)
    if current >= expiry_at:
        intrinsic = max(spot - trade.strike, 0.0) if trade.option_type == "CE" else max(trade.strike - spot, 0.0)
        return round_tick(intrinsic)
    years = max((expiry_at - current).total_seconds() / (365.0 * 24 * 60 * 60), 1.0 / (365.0 * 24))
    estimated_iv = float(os.getenv("ESTIMATED_IV", "0.18"))
    return round_tick(option_price(spot, trade.strike, years, 0.06, estimated_iv, trade.option_type))


def mark_paper_journal(spot: float, bar_time: str) -> dict:
    marked_at = parse_bar_time(bar_time)
    updated = 0
    closed = 0
    with SessionLocal.begin() as db:
        trades = list(db.scalars(select(PaperTrade).where(PaperTrade.status.in_(OPEN_PAPER_STATUSES))))
        for trade in trades:
            price = paper_mark_price(trade, spot, marked_at)
            trade.last_spot = spot
            trade.last_price = price
            trade.updated_at = marked_at
            expiry_at = dt.datetime.combine(dt.date.fromisoformat(trade.expiry), dt.time(15, 30), tzinfo=IST)
            final_status = None
            if price <= trade.stop_price:
                final_status = "STOPPED"
            elif price >= trade.target_price:
                final_status = "TARGET_HIT"
            elif marked_at.astimezone(IST) >= expiry_at:
                final_status = "EXPIRED"
            if final_status:
                trade.status = final_status
                trade.exit_spot = spot
                trade.exit_price = price
                trade.closed_at = marked_at
                signal = db.get(Signal, trade.signal_id)
                if signal:
                    signal.status = "COMPLETE"
                    signal.updated_at = marked_at
                closed += 1
            updated += 1
    return {"paper_trades_updated": updated, "paper_trades_closed": closed}


def record_paper_trade(db, signal: Signal, settings: StrategySettings) -> None:
    kite = kite_client(require_token=False)
    contract = choose_contract_details(kite, signal.spot, signal.side, settings.min_delta)
    entry_price = float(contract["estimated_price"])
    trade = PaperTrade(
        signal_id=signal.id,
        side=signal.side,
        symbol=str(contract["symbol"]),
        option_type=str(contract["option_type"]),
        strike=float(contract["strike"]),
        expiry=contract["expiry"].isoformat(),
        quantity=settings.lot_size,
        entry_spot=signal.spot,
        entry_delta=float(contract["delta"]),
        entry_price=entry_price,
        stop_price=round_tick(entry_price - settings.stop_loss_points),
        target_price=round_tick(entry_price + settings.target_points),
        last_spot=signal.spot,
        last_price=entry_price,
    )
    db.add(trade)
    signal.status = "PAPER_ACTIVE"
    signal.updated_at = utcnow()
    db.commit()


def claim_signal() -> int | None:
    with SessionLocal.begin() as db:
        settings = db.get(StrategySettings, 1)
        execution_mode = int(settings.trading_enabled) if settings else EXECUTION_DISABLED
        active = db.scalar(select(Trade.id).where(Trade.status.in_(BLOCKING_TRADE_STATUSES)).limit(1)) if execution_mode == EXECUTION_LIVE else None
        paper_active = db.scalar(select(PaperTrade.id).where(PaperTrade.status.in_(OPEN_PAPER_STATUSES)).limit(1)) if execution_mode == EXECUTION_PAPER else None
        if active is not None or paper_active is not None:
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
        execution_mode = int(settings.trading_enabled)
        if execution_mode == EXECUTION_DISABLED:
            signal.status = "REJECTED"
            signal.error = "Execution is disabled"
            db.commit()
            return
        if not in_entry_window():
            signal.status = "REJECTED"
            signal.error = "Signal arrived outside the 09:15-15:15 IST entry window"
            db.commit()
            return
        if execution_mode == EXECUTION_PAPER:
            record_paper_trade(db, signal, settings)
            return
        if execution_mode != EXECUTION_LIVE:
            signal.status = "REJECTED"
            signal.error = "Unknown execution mode"
            db.commit()
            return
        kite = kite_client()
        symbol = choose_contract(kite, signal.spot, signal.side, settings.min_delta)
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
            order_type=kite.ORDER_TYPE_MARKET,
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
        trade.entry_price = entry_price
        trade.stop_price = stop_price
        trade.target_price = target_price
        try:
            trade.gtt_trigger_id = place_exit_gtt_with_retry(
                kite,
                symbol,
                settings.lot_size,
                entry_price,
                stop_price,
                stop_limit,
                target_price,
            )
        except Exception as gtt_error:
            trade.stop_order_id = place_stop_with_retry(kite, symbol, settings.lot_size, stop_price, stop_limit)
            trade.status = "NEEDS_REVIEW"
            trade.error = f"GTT OCO failed; a day-only emergency stop was placed: {str(gtt_error)[:500]}"
            trade.updated_at = utcnow()
            signal.status = "NEEDS_REVIEW"
            signal.error = trade.error
            signal.updated_at = utcnow()
            db.commit()
            return
        trade.status = "ACTIVE"
        trade.updated_at = utcnow()
        signal.status = "ACTIVE"
        signal.updated_at = utcnow()
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
    position_quantities = {
        str(row.get("tradingsymbol")): int(row.get("quantity", 0))
        for row in (kite.positions().get("net") or [])
        if row.get("exchange") == "NFO"
    }
    for snapshot in trades:
        if not snapshot.gtt_trigger_id:
            continue
        trigger = kite.get_gtt(int(snapshot.gtt_trigger_id))
        trigger_status = str(trigger.get("status", "")).lower()
        if trigger_status == "active":
            if position_quantities.get(snapshot.symbol, 0) <= 0:
                kite.delete_gtt(int(snapshot.gtt_trigger_id))
                with SessionLocal.begin() as db:
                    trade = db.get(Trade, snapshot.id)
                    signal = db.get(Signal, trade.signal_id) if trade else None
                    if trade:
                        trade.status = "CLOSED_EXTERNALLY"
                        trade.error = "Broker position was closed outside Drojun; the active GTT was deleted"
                        trade.updated_at = utcnow()
                    if signal:
                        signal.status = "COMPLETE"
                        signal.error = trade.error
                        signal.updated_at = utcnow()
            continue
        final_status = None
        error = None
        if trigger_status == "triggered":
            triggered_leg = None
            for index, leg in enumerate(trigger.get("orders") or []):
                if leg.get("result") is not None:
                    triggered_leg = (index, leg)
                    break
            if triggered_leg is None:
                error = "GTT is triggered but Kite returned no triggered exit leg"
            else:
                index, leg = triggered_leg
                order_result = (leg.get("result") or {}).get("order_result") or {}
                if order_result.get("status") != "success" or not order_result.get("order_id"):
                    error = f'GTT exit order failed: {order_result.get("rejection_reason", "unknown reason")}'
                else:
                    broker_order = order_by_id(kite, str(order_result["order_id"]))
                    if broker_order and broker_order.get("status") == "COMPLETE":
                        final_status = "STOPPED" if index == 0 else "TARGET_HIT"
                    elif broker_order and broker_order.get("status") in {"CANCELLED", "REJECTED"}:
                        error = f'GTT exit order is {broker_order.get("status")}: {broker_order.get("status_message", "")}'.strip()
                    elif broker_order is None and position_quantities.get(snapshot.symbol, 0) <= 0:
                        final_status = "STOPPED" if index == 0 else "TARGET_HIT"
                    elif broker_order is None:
                        error = "GTT exit was triggered but the broker position remains open; verify it immediately"
                    else:
                        continue
        else:
            error = f"GTT OCO is {trigger_status or 'in an unknown state'}; verify the open position immediately"
        with SessionLocal.begin() as db:
            trade = db.get(Trade, snapshot.id)
            signal = db.get(Signal, trade.signal_id) if trade else None
            if trade:
                trade.status = final_status or "NEEDS_REVIEW"
                trade.error = error
                trade.updated_at = utcnow()
            if signal:
                signal.status = "COMPLETE" if final_status else "NEEDS_REVIEW"
                signal.error = error
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
        execution_mode = int(settings.trading_enabled) if settings else EXECUTION_DISABLED
        return jsonify(
            status="ok",
            execution_mode={EXECUTION_DISABLED: "disabled", EXECUTION_PAPER: "paper", EXECUTION_LIVE: "live"}.get(execution_mode, "unknown"),
            paper_trading_enabled=execution_mode == EXECUTION_PAPER,
            trading_enabled=execution_mode == EXECUTION_LIVE,
            kite_login_current=bool(state and token_is_current(state.token_issued_at)),
        )


@app.get("/broker/status")
@admin_required
def broker_status():
    kite = kite_client()
    positions = [
        {
            "exchange": row.get("exchange"),
            "symbol": row.get("tradingsymbol"),
            "quantity": int(row.get("quantity", 0)),
            "product": row.get("product"),
        }
        for row in (kite.positions().get("net") or [])
        if int(row.get("quantity", 0)) != 0
    ]
    open_statuses = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED"}
    orders = [
        {
            "order_id": str(row.get("order_id", "")),
            "exchange": row.get("exchange"),
            "symbol": row.get("tradingsymbol"),
            "transaction_type": row.get("transaction_type"),
            "quantity": int(row.get("quantity", 0)),
            "status": row.get("status"),
        }
        for row in kite.orders()
        if str(row.get("status", "")).upper() in open_statuses
    ]
    gtts = [
        {
            "trigger_id": str(row.get("id", "")),
            "symbol": row.get("tradingsymbol"),
            "status": row.get("status"),
        }
        for row in kite.get_gtts()
        if str(row.get("status", "")).lower() in {"active", "triggered"}
    ]
    return jsonify(
        status="ok",
        open_positions=positions,
        open_orders=orders,
        active_gtts=gtts,
    )


@app.get("/journal.csv")
@admin_required
def paper_journal_csv():
    with SessionLocal() as db:
        rows = list(db.scalars(select(PaperTrade).order_by(PaperTrade.id)))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "opened_at_ist", "closed_at_ist", "side", "symbol", "expiry", "strike", "quantity",
        "entry_spot", "entry_delta", "entry_price", "stop_price", "target_price", "last_spot", "last_price",
        "status", "exit_spot", "exit_price", "pnl",
    ])
    for row in rows:
        opened = row.opened_at.replace(tzinfo=row.opened_at.tzinfo or dt.timezone.utc).astimezone(IST)
        closed = row.closed_at.replace(tzinfo=row.closed_at.tzinfo or dt.timezone.utc).astimezone(IST) if row.closed_at else None
        pnl = paper_pnl(row)
        writer.writerow([
            row.id, opened.isoformat(), closed.isoformat() if closed else "", row.side, row.symbol, row.expiry,
            row.strike, row.quantity, row.entry_spot, row.entry_delta, row.entry_price, row.stop_price,
            row.target_price, row.last_spot, row.last_price, row.status,
            row.exit_spot if row.exit_spot is not None else "",
            row.exit_price if row.exit_price is not None else "", pnl,
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=drojun-paper-journal.csv"},
    )


ADMIN_PAGE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drojun control panel</title><style>
body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#18212f}form{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:grid;gap:5px}input,select,button{font:inherit;padding:9px}button{grid-column:1/-1}.card{padding:20px;border:1px solid #ccd3dc;border-radius:12px;margin:18px 0}.ok{color:#087830}.bad{color:#b42318}small{color:#56606f}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e7ec;white-space:nowrap}.scroll{overflow:auto}</style></head><body>
<h1>Drojun</h1><div class="card"><b>Kite session:</b> <span class="{{ 'ok' if token_current else 'bad' }}">{{ 'ready' if token_current else 'login required' }}</span><br><br><a href="/kite/login">Log in to Zerodha</a></div>
<div class="card"><h2>Kite app credentials</h2><p>Current API key: <code>{{ kite_key or 'not configured' }}</code></p><form method="post" action="/kite/credentials"><input type="hidden" name="csrf" value="{{ csrf }}"><label>API key<input name="api_key" minlength="16" maxlength="16" required></label><label>API secret<input type="password" name="api_secret" minlength="32" maxlength="32" required></label><button type="submit">Save Kite credentials</button></form></div>
<div class="card"><h2>Strategy settings</h2><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>CCI length<input type="number" name="cci_length" min="2" max="500" value="{{ s.cci_length }}"></label>
<label>Minimum delta<input type="number" name="min_delta" min="0.01" max="0.99" step="0.01" value="{{ s.min_delta }}"></label>
<label>Stop loss points<input type="number" name="stop_loss_points" min="0.05" step="0.05" value="{{ s.stop_loss_points }}"></label>
<label>Target points<input type="number" name="target_points" min="0.05" step="0.05" value="{{ s.target_points }}"></label>
<label>Quantity<input type="number" name="lot_size" min="1" value="{{ s.lot_size }}"></label>
<label>Execution mode<select name="execution_mode"><option value="0" {{ 'selected' if s.trading_enabled == 0 else '' }}>Disabled</option><option value="2" {{ 'selected' if s.trading_enabled == 2 else '' }}>Paper trading</option><option value="1" {{ 'selected' if s.trading_enabled == 1 else '' }}>Live trading</option></select></label>
<button type="submit">Save settings</button></form><p><small>Contract delta is estimated from the TradingView spot price and configured ESTIMATED_IV, so the free Kite Personal API is sufficient. After changing CCI length, recreate the TradingView alert with the same value.</small></p></div>
<div class="card"><h2>Paper trade journal</h2><p><a href="/journal.csv">Download CSV</a></p><div class="scroll"><table><thead><tr><th>Opened (IST)</th><th>Side</th><th>Contract</th><th>Qty</th><th>Spot</th><th>Delta</th><th>Entry</th><th>Mark</th><th>Stop</th><th>Target</th><th>Status</th><th>P&amp;L</th></tr></thead><tbody>{% for x in paper_journal %}<tr><td>{{ format_ist(x.opened_at) }}</td><td>{{ x.side }}</td><td>{{ x.symbol }}</td><td>{{ x.quantity }}</td><td>{{ '%.2f'|format(x.entry_spot) }}</td><td>{{ '%.3f'|format(x.entry_delta) }}</td><td>{{ '%.2f'|format(x.entry_price) }}</td><td>{{ '%.2f'|format(x.exit_price if x.exit_price is not none else x.last_price) }}</td><td>{{ '%.2f'|format(x.stop_price) }}</td><td>{{ '%.2f'|format(x.target_price) }}</td><td>{{ x.status }}</td><td>{{ '%.2f'|format(paper_pnl(x)) }}</td></tr>{% else %}<tr><td colspan="12">No paper trades yet</td></tr>{% endfor %}</tbody></table></div><p><small>Paper option prices are Black-Scholes estimates using ESTIMATED_IV. Hourly TradingView bar updates mark open paper trades and close them when the estimated stop or target is reached.</small></p></div>
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
            settings.trading_enabled = int(request.form["execution_mode"])
            if not (2 <= settings.cci_length <= 500 and 0.01 <= settings.min_delta <= 0.99 and settings.stop_loss_points > 0 and settings.target_points > 0 and settings.lot_size > 0 and settings.trading_enabled in {EXECUTION_DISABLED, EXECUTION_PAPER, EXECUTION_LIVE}):
                return "Invalid settings", 400
            settings.updated_at = utcnow()
            db.commit()
            return redirect("/")
        csrf = secrets.token_urlsafe(24)
        session["csrf"] = csrf
        recent = list(db.scalars(select(Signal).order_by(Signal.id.desc()).limit(10)))
        recent_trades = list(db.scalars(select(Trade).order_by(Trade.id.desc()).limit(10)))
        paper_journal = list(db.scalars(select(PaperTrade).order_by(PaperTrade.id.desc()).limit(100)))
        kite_key, _ = kite_credentials()
        return render_template_string(ADMIN_PAGE, s=settings, signals=recent, trades=recent_trades, paper_journal=paper_journal, format_ist=format_ist, paper_pnl=paper_pnl, csrf=csrf, token_current=bool(state and token_is_current(state.token_issued_at)), kite_key=kite_key)


@app.post("/kite/credentials")
@admin_required
def update_kite_credentials():
    if not secure_equal(request.form.get("csrf"), session.get("csrf")):
        return "Invalid form token", 400
    try:
        save_kite_credentials(request.form.get("api_key", "").strip(), request.form.get("api_secret", "").strip())
    except ValueError as exc:
        return str(exc), 400
    with SessionLocal.begin() as db:
        state = db.get(RuntimeState, 1)
        state.access_token = None
        state.token_issued_at = None
        state.updated_at = utcnow()
    return redirect("/")


@app.get("/kite/login")
@admin_required
def kite_login():
    return redirect(kite_client(require_token=False).login_url())


@app.get("/kite/callback")
def kite_callback():
    request_token = request.args.get("request_token")
    if request.args.get("status") != "success" or not request_token:
        return "Kite login did not succeed", 400
    _, api_secret = kite_credentials()
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
        side = str(data.get("signal", "NONE")).upper()
        spot = float(data["price"])
        cci_length = int(data["cci_length"])
        bar_time = str(data["bar_time"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="price, cci_length and bar_time are required"), 400
    if side not in {"CALL", "PUT", "NONE"} or spot <= 0 or not bar_time:
        return jsonify(error="invalid signal payload"), 400
    with SessionLocal() as db:
        settings = db.get(StrategySettings, 1)
        execution_mode = int(settings.trading_enabled)
        expected_cci_length = settings.cci_length
    if execution_mode == EXECUTION_DISABLED:
        return jsonify(error="execution is disabled"), 409
    if cci_length != expected_cci_length:
        return jsonify(error=f"CCI length mismatch; expected {expected_cci_length}"), 409
    paper_mark = mark_paper_journal(spot, bar_time) if execution_mode == EXECUTION_PAPER else {}
    if side == "NONE":
        return jsonify(status="bar_recorded", execution_mode="paper" if execution_mode == EXECUTION_PAPER else "live", **paper_mark), 200
    if not in_entry_window():
        return jsonify(error="outside the 09:15-15:15 IST entry window"), 409
    with SessionLocal() as db:
        active_trade = db.scalar(select(Trade.id).where(Trade.status.in_(BLOCKING_TRADE_STATUSES)).limit(1)) if execution_mode == EXECUTION_LIVE else None
        active_paper = db.scalar(select(PaperTrade.id).where(PaperTrade.status.in_(OPEN_PAPER_STATUSES)).limit(1)) if execution_mode == EXECUTION_PAPER else None
        pending_signal = db.scalar(select(Signal.id).where(Signal.status.in_(BLOCKING_SIGNAL_STATUSES)).limit(1))
        if active_trade is not None or active_paper is not None or pending_signal is not None:
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
        return jsonify(status="queued", signal_id=row.id, execution_mode="paper" if execution_mode == EXECUTION_PAPER else "live", **paper_mark), 202


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



