from flask import Flask, render_template, request, session, redirect, flash, abort, jsonify
import pandas as pd
import plotly.graph_objs as go
import plotly.io as pio
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
import os
import json
import re
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect, text
import threading
import time
import random
from models import db, User, PortfolioTicker, PortfolioSettings, AlertEmail
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv

load_dotenv()  # reads .env into os.environ

# ===============================
# Flask App Initialization
# ===============================
app = Flask(__name__)

secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    raise RuntimeError("SECRET_KEY must be set.")
app.config["SECRET_KEY"] = secret_key

database_url = os.environ.get("DATABASE_URL")

# In production: require DATABASE_URL
if not database_url:
    # allow local dev fallback only
    if os.environ.get("FLASK_ENV") == "production" or os.environ.get("RENDER") == "true":
        raise RuntimeError("DATABASE_URL is required in production (Render).")
    database_url = "sqlite:///portfolio.db"

# Render/Heroku style fix
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ✅ Bind SQLAlchemy to this app
db.init_app(app)

# ✅ Create tables
with app.app_context():
    db.create_all()

    # Ensure schema compatibility for the alert cooldown column (avoids duplicate alerts on Render)
    insp = inspect(db.engine)
    try:
        cols = [c['name'] for c in insp.get_columns('portfolio_settings')]
        if 'last_alert_check_at' not in cols:
            dialect = db.engine.dialect.name
            col_type = 'TIMESTAMP' if dialect != 'sqlite' else 'DATETIME'
            db.session.execute(text(f'ALTER TABLE portfolio_settings ADD COLUMN last_alert_check_at {col_type}'))
            db.session.commit()
            print('✅ Added missing column: portfolio_settings.last_alert_check_at')
    except Exception as e:
        # If the table doesn't exist yet or ALTER fails, ignore; create_all will handle fresh DBs.
        db.session.rollback()
        print('⚠️ Schema check skipped/failed:', e)




# ----------------------------
# Alpha Vantage helpers (retries + light caching)
# ----------------------------
import urllib.request
import urllib.parse
import urllib.error

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")
if not ALPHA_VANTAGE_API_KEY:
    raise RuntimeError("ALPHA_VANTAGE_API_KEY must be set (Alpha Vantage is required for market data).")


# Debug logging for Alpha Vantage responses.
# Set AV_DEBUG=1 in your environment to enable verbose logging.
AV_DEBUG = os.environ.get("AV_DEBUG", "0") == "1"

def _av_redact_url(url: str) -> str:
    # avoid leaking API key into logs
    return re.sub(r"(apikey=)[^&]+", r"\1***", url)

def _av_dbg(msg: str):
    if AV_DEBUG:
        print(msg)


_AV_CACHE = {
    "quote": {},  # symbol -> (ts_epoch, payload)
    "daily": {},  # symbol -> (ts_epoch, payload)
}

# Simple global rate limiter for Alpha Vantage to avoid burst throttling.
# Default pacing: ~4 requests/sec (0.26s spacing). Override with AV_MIN_INTERVAL_S.
_AV_RATE_LOCK = threading.Lock()
_AV_LAST_CALL_TS = 0.0
_AV_MIN_INTERVAL_S = float(os.environ.get("AV_MIN_INTERVAL_S", "0.26"))

def _now_epoch() -> float:
    return time.time()

def av_get_json(params: dict, retries: int = 3, base_sleep: float = 1.0):
    """Call Alpha Vantage and return parsed JSON (dict) or None on failure.

    Debug:
      - set AV_DEBUG=1 to print URL (redacted), status/bytes, top-level keys,
        and any throttle/error messages (Note/Information/Error Message).
    """
    q = urllib.parse.urlencode(params)
    url = f"https://www.alphavantage.co/query?{q}"

    func = params.get("function")
    sym = params.get("symbol")

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # Global pacing to avoid Alpha Vantage "Burst pattern detected".
            global _AV_LAST_CALL_TS
            with _AV_RATE_LOCK:
                now_ts = time.time()
                wait_s = _AV_MIN_INTERVAL_S - (now_ts - _AV_LAST_CALL_TS)
                if wait_s > 0:
                    time.sleep(wait_s + random.uniform(0, 0.05))
                _AV_LAST_CALL_TS = time.time()

            _av_dbg(f"🛰️ AV request: function={func} symbol={sym} attempt={attempt}/{retries} url={_av_redact_url(url)}")

            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    status = getattr(resp, "status", None)
                    raw_bytes = resp.read()
                raw = raw_bytes.decode("utf-8", errors="replace")
                _av_dbg(f"✅ AV HTTP {status} bytes={len(raw_bytes)} head={raw[:180].replace(chr(10),' ')[:180]}")
            except urllib.error.HTTPError as he:
                body = he.read().decode("utf-8", errors="replace")
                _av_dbg(f"❌ AV HTTPError {he.code} function={func} symbol={sym} body_head={body[:220].replace(chr(10),' ')[:220]}")
                raise
            except urllib.error.URLError as ue:
                _av_dbg(f"❌ AV URLError function={func} symbol={sym} err={ue}")
                raise

            data = json.loads(raw)
            if isinstance(data, dict):
                keys = list(data.keys())
                _av_dbg(f"🔎 AV keys: {keys}")

                # Alpha Vantage rate limit / throttling / errors
                if "Note" in data:
                    note = str(data.get("Note", ""))[:240]
                    _av_dbg(f"⚠️ AV THROTTLED Note: {note}")
                    raise RuntimeError(data.get("Note"))
                if "Information" in data:
                    info = str(data.get("Information", ""))[:240]
                    _av_dbg(f"⚠️ AV Information: {info}")
                    raise RuntimeError(data.get("Information"))
                if "Error Message" in data:
                    em = str(data.get("Error Message", ""))[:240]
                    _av_dbg(f"❌ AV Error Message: {em}")
                    raise RuntimeError(data.get("Error Message"))

            return data

        except Exception as e:
            last_err = e
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            print(f"⚠️ Alpha Vantage call failed (attempt {attempt}/{retries}) function={func} symbol={sym}: {e}")
            if attempt < retries:
                time.sleep(sleep_s)
    print(f"❌ Alpha Vantage call failed after {retries} attempts function={func} symbol={sym}: {last_err}")
    return None


def av_global_quote(symbol: str, cache_seconds: int = 60):
    """Return (price, prev_close) floats or (None, None) if unavailable."""
    symbol = symbol.strip().upper()
    _av_dbg(f"📌 GLOBAL_QUOTE {symbol} cache_seconds={cache_seconds}")
    cached = _AV_CACHE["quote"].get(symbol)
    now = _now_epoch()
    if cached and (now - cached[0]) < cache_seconds:
        _av_dbg(f"🟢 quote cache hit {symbol}")
        payload = cached[1]
    else:
        _av_dbg(f"🟠 quote cache miss {symbol} -> calling AV")
        payload = av_get_json({
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": ALPHA_VANTAGE_API_KEY
        })
        # Only cache successful payloads; do NOT poison the cache with None/invalid responses
        if payload and isinstance(payload, dict) and payload.get("Global Quote"):
            _AV_CACHE["quote"][symbol] = (now, payload)
        else:
            payload = None

    if not payload or "Global Quote" not in payload:
        return None, None

    q = payload.get("Global Quote", {}) or {}
    try:
        price = float(q.get("05. price"))
        prev = float(q.get("08. previous close"))
        return price, prev
    except Exception:
        return None, None

def av_daily_adjusted(symbol: str, cache_seconds: int = 900):
    """Return dict date->adjusted_close (strings) or None."""
    symbol = symbol.strip().upper()
    cached = _AV_CACHE["daily"].get(symbol)
    now = _now_epoch()
    if cached and (now - cached[0]) < cache_seconds:
        _av_dbg(f"🟢 daily cache hit {symbol}")
        payload = cached[1]
    else:
        _av_dbg(f"🟠 daily cache miss {symbol} -> calling AV")
        payload = av_get_json({
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": symbol,
            "outputsize": "compact",
            "apikey": ALPHA_VANTAGE_API_KEY
        })
        # Only cache successful payloads; do NOT poison the cache with None/invalid responses
        if payload and isinstance(payload, dict) and payload.get("Time Series (Daily)"):
            _AV_CACHE["daily"][symbol] = (now, payload)
        else:
            payload = None

    if not payload:
        return None

    ts = payload.get("Time Series (Daily)")
    if not isinstance(ts, dict):
        return None
    return ts


def av_last_two_adjusted_closes(symbol: str):
    """Return (last_close, prev_close) from the daily adjusted series."""
    ts = av_daily_adjusted(symbol)
    if not ts:
        return None, None

    dates = sorted(ts.keys(), reverse=True)
    closes = []
    for d in dates:
        vals = ts.get(d) or {}
        try:
            closes.append(float(vals.get("5. adjusted close")))
        except Exception:
            continue
        if len(closes) >= 2:
            break

    if len(closes) < 2:
        return None, None
    return closes[0], closes[1]

def av_close_series(symbol: str, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.Series:
    """Return a pandas Series of adjusted close indexed by date for [start_date, end_date]."""
    ts = av_daily_adjusted(symbol)
    if not ts:
        return pd.Series(dtype="float64")

    # Alpha Vantage dates are 'YYYY-MM-DD' strings
    rows = []
    for d, vals in ts.items():
        try:
            dt = pd.to_datetime(d)
        except Exception:
            continue
        if dt < start_date or dt > end_date:
            continue
        try:
            adj = float(vals.get("5. adjusted close"))
        except Exception:
            continue
        rows.append((dt.normalize(), adj))

    if not rows:
        return pd.Series(dtype="float64")

    s = pd.Series(dict(rows))
    s = s.sort_index()
    return s



def build_month_close_series(symbol: str, month_start: pd.Timestamp, today: pd.Timestamp, all_days: pd.DatetimeIndex) -> pd.Series:
    """Build a month-to-date *calendar-day* close series.

    - Uses AV daily adjusted series (cached) for trading days in [month_start, today]
    - Reindexes to calendar days and forward-fills (so weekends show)
    - If there are *no* trading days yet in the current month (e.g., month starts on weekend/holiday),
      returns a flat series seeded with the last available close <= today.
    """
    ts = av_daily_adjusted(symbol)
    if not ts:
        return pd.Series(dtype="float64")

    rows = []
    for ds, vals in ts.items():
        try:
            d = pd.to_datetime(ds).normalize()
            if d > today:
                continue
            # Prefer adjusted close; fall back to close if needed
            close = float((vals or {}).get("5. adjusted close") or (vals or {}).get("4. close"))
            rows.append((d, close))
        except Exception:
            continue

    if not rows:
        return pd.Series(dtype="float64")

    rows.sort(key=lambda x: x[0])
    last_close = rows[-1][1]

    in_month = [(d, c) for d, c in rows if month_start <= d <= today]
    if not in_month:
        # No trading days yet this month
        return pd.Series([last_close] * len(all_days), index=all_days, dtype="float64")

    s = pd.Series({d: c for d, c in in_month}).sort_index()
    s = s.reindex(all_days)

    # Fill initial NaNs (before first trading day) with last_close from previous month
    first_valid = s.first_valid_index()
    if first_valid is None:
        return pd.Series([last_close] * len(all_days), index=all_days, dtype="float64")

    if pd.isna(s.iloc[0]):
        s.loc[:first_valid] = s.loc[:first_valid].fillna(last_close)

    # Fill the rest forward (weekends / missing days)
    s = s.ffill()

    return s


# ----------------------------
# Helper function to get live prices
# ----------------------------

def get_live_prices(tickers):
    """Fetch latest price + **MTD % change** for each ticker.

    Tile % should match the dashboard's Month-to-Date logic:
      MTD% = (last trading close of current month / first trading close of current month - 1) * 100

    On the first day of the month when it's a non-trading day (weekend/holiday),
    we show MTD% = 0.00 for all tickers (and still show the latest available price).
    """
    live_data = {}

    today = pd.Timestamp.today().normalize()
    start_of_month = today.replace(day=1)

    for t in tickers:
        try:
            # Prefer MTD series from daily adjusted (cached)
            s = av_close_series(t, start_date=start_of_month, end_date=today)

            if s is not None and not s.empty:
                first = float(s.iloc[0])
                last = float(s.iloc[-1])
                price = last
                pct = (last / first - 1) * 100 if first else 0.0
            else:
                # No month data yet (common on month-start weekend/holiday) or API issue.
                # Still show the latest available close, but set MTD% to 0 on month-start non-trading days.
                last_close, prev_close = av_last_two_adjusted_closes(t)
                if last_close is None:
                    last_close, prev_close = av_global_quote(t)

                if last_close is None:
                    raise ValueError("No data returned")

                price = float(last_close)
                pct = 0.0

            live_data[t] = {"price": round(price, 2), "pct": None if pct is None else round(float(pct), 2)}
        except Exception as e:
            print(f"⚠️ Live price failed for {t}: {e}")
            live_data[t] = {"price": None, "pct": None}

    return live_data



# ----------------------------
# Send Emails When TP/SL hit
# ----------------------------
# Unified email sender
# ----------------------------
# Send Emails via SendGrid API
# ----------------------------
def send_portfolio_alert_thread(subject, body):
    """Send alert to all configured emails using SendGrid API."""
    with app.app_context():
        emails = [e.email for e in AlertEmail.query.all()]
        if not emails:
            print("No alert emails configured.")
            return

        try:
            sg = SendGridAPIClient(os.environ.get("SENDGRID_API_KEY"))

            message = Mail(
                from_email=os.environ.get("ALERT_FROM_EMAIL"),
                to_emails=emails,
                subject=subject,
                plain_text_content=body,
            )
            response = sg.send(message)
            print("SendGrid status:", response.status_code)
            print("Email sent to:", emails)
            return response.status_code == 202
        except Exception as e:
            print("SendGrid email failed:", str(e))
            return False


def send_portfolio_alert_async(subject, body):
    """Run the alert in a background thread."""
    thread = threading.Thread(target=send_portfolio_alert_thread, args=(subject, body))
    thread.start()


def send_test_email_async():
    """Send a test email to all recipients."""
    subject = "📈 Test Alert – Portfolio Dashboard"
    body = "This is a test alert from your portfolio dashboard."
    send_portfolio_alert_async(subject, body)



# ----------------------------
# Check TP/SL
# ----------------------------
def check_and_send_portfolio_alerts(settings, portfolio_pct):
    if not settings:
        return

    #debugging
    print("------ ALERT DEBUG ------")
    print("Portfolio:", portfolio_pct, type(portfolio_pct))
    print("TP1:", settings.tp1, type(settings.tp1))
    print("TP2:", settings.tp2)
    print("TP3:", settings.tp3)
    print("SL:", settings.stop_loss)
    print("TP1 HIT:", settings.tp1_hit)
    print("TP2 HIT:", settings.tp2_hit)
    print("TP3 HIT:", settings.tp3_hit)
    print("-------------------------")

    # TP1
    if settings.tp1 and not settings.tp1_hit and portfolio_pct >= settings.tp1:
        send_portfolio_alert_async(
            subject="📈 Portfolio TP1 Hit",
            body=f"Portfolio has reached TP1 at {portfolio_pct:.2f}%."
        )
        settings.tp1_hit = True

    # TP2
    if settings.tp2 and not settings.tp2_hit and portfolio_pct >= settings.tp2:
        send_portfolio_alert_async(
            subject="📈 Portfolio TP2 Hit",
            body=f"Portfolio has reached TP2 at {portfolio_pct:.2f}%."
        )
        settings.tp2_hit = True

    # TP3
    if settings.tp3 and not settings.tp3_hit and portfolio_pct >= settings.tp3:
        send_portfolio_alert_async(
            subject="📈 Portfolio TP3 Hit",
            body=f"Portfolio has reached TP3 at {portfolio_pct:.2f}%."
        )
        settings.tp3_hit = True

    # Stop Loss
    if settings.stop_loss and not settings.sl_hit and portfolio_pct <= settings.stop_loss:
        send_portfolio_alert_async(
            subject="🚨 Portfolio Stop Loss Hit",
            body=f"Portfolio has hit Stop Loss at {portfolio_pct:.2f}%."
        )
        settings.sl_hit = True

    db.session.commit()



# ----------------------------
# Alert check cooldown guard (prevents duplicate alert checks across web workers)
# ----------------------------
def should_run_alert_check(settings, cooldown_seconds=60):
    """Return True if we should run the TP/SL check now.

    We store a timestamp on the PortfolioSettings row. The first worker to commit wins;
    others will see the recent timestamp and skip within the cooldown window.
    """
    if not settings:
        return False

    now = datetime.utcnow()
    last = getattr(settings, "last_alert_check_at", None)
    if last and (now - last).total_seconds() < cooldown_seconds:
        return False

    # Commit BEFORE sending emails to create a lock visible to other workers
    settings.last_alert_check_at = now
    db.session.commit()
    return True


# ----------------------------
# Main dashboard route
# ----------------------------
@app.route("/")
def dashboard():
    chart_html = None
    tickers_data = []
    portfolio_pct = None
    last_updated = None

    settings = PortfolioSettings.query.first()
    db_tickers = PortfolioTicker.query.all()

    if not db_tickers:
        return render_template("dashboard.html", 
                                message="No portfolio loaded", 
                                portfolio_pct=None,
                                last_updated=None,
                                chart_html=None,
                                tickers=[]
                            )

    tickers = [t.ticker for t in db_tickers]
    live_prices = get_live_prices(tickers)

    tickers_data = []
    for t in db_tickers:
        lp = live_prices.get(t.ticker, {})
        tickers_data.append({
            "ticker": t.ticker,
            "index": t.index,
            "price": lp.get("price"),
            "pct": lp.get("pct")
        })

    last_updated = datetime.now().strftime("%H:%M")

    # ------------------------
    # 📊 CHART SECTION
    # ------------------------

    # --- Month-to-date chart ---
    today = pd.Timestamp.today().normalize()
    start_of_month = today.replace(day=1)
    all_days = pd.date_range(start=start_of_month, end=today, freq="D")

    # 1️⃣ Equal-weight portfolio (Alpha Vantage daily adjusted closes)
    start_date = start_of_month
    end_date = today

    price_cols = {}
    for sym in tickers:
        s = build_month_close_series(sym, month_start=start_of_month, today=today, all_days=all_days)
        if s is not None and not s.empty:
            price_cols[sym] = s

    _av_dbg(f"📈 CHART month={start_date.date()}..{end_date.date()} series_ok={len(price_cols)}/{len(tickers)} missing={[s for s in tickers if s not in price_cols]}")

    # If we still have nothing, it's a genuine data/API issue (or AV key missing/blocked)
    if not price_cols:
        _av_dbg("🚫 CHART: no daily series available; rendering unavailable state")
        return render_template(
            "dashboard.html",
            message="Market data temporarily unavailable (Alpha Vantage). Please refresh later.",
            portfolio_settings=settings,
            portfolio_pct=None,
            last_updated=last_updated,
            chart_html=None,
            tickers=tickers_data,
        )

    # Build equal-weight index over *calendar days* (weekends included)
    prices = pd.DataFrame(price_cols).reindex(all_days)

    # Any remaining gaps are forward-filled by build_month_close_series; this is just safety
    prices = prices.ffill()

    returns = prices.pct_change().fillna(0)
    portfolio_index = (1 + returns.mean(axis=1)).cumprod()

    fv = portfolio_index.first_valid_index()
    if fv is None:
        portfolio_index = pd.Series([1.0] * len(all_days), index=all_days)
        portfolio_pct = 0.00
    else:
        portfolio_index = portfolio_index / float(portfolio_index.loc[fv])
        portfolio_pct = round((float(portfolio_index.iloc[-1]) - 1) * 100, 2)

# TP/SL alerts: guard with cooldown to prevent duplicate sends on multi-worker deployments
    if portfolio_pct is not None and settings and should_run_alert_check(settings, cooldown_seconds=60):
        check_and_send_portfolio_alerts(settings, portfolio_pct)

    # 2️⃣ Benchmarks (ETF proxies via Alpha Vantage): DIA (Dow proxy) & QQQ (Nasdaq-100 proxy)
    try:
        bench_cols = {}
        dia = build_month_close_series("DIA", month_start=start_of_month, today=today, all_days=all_days)
        qqq = build_month_close_series("QQQ", month_start=start_of_month, today=today, all_days=all_days)
        if not dia.empty:
            bench_cols["DIA"] = dia
        if not qqq.empty:
            bench_cols["QQQ"] = qqq

        benchmarks = pd.DataFrame(index=all_days)
        if bench_cols:
            close = pd.DataFrame(bench_cols).reindex(all_days).ffill()

            # Normalize safely even if the first row is NaN (common on non-trading month start).
            for c in list(close.columns):
                fv = close[c].first_valid_index()
                if fv is None:
                    close[c] = 1.0
                else:
                    close[c] = close[c] / float(close.loc[fv, c])

            benchmarks = close
    except Exception as e:
        print(f"⚠️ Benchmark download failed: {e}. Using empty DataFrame instead.")
        benchmarks = pd.DataFrame(index=all_days)

    # 3️⃣ Plotly chart
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=portfolio_index.index,
            y=portfolio_index.values,
            mode="lines+markers",
            name="Equal Weight Portfolio",
        )
    )

    if "DIA" in benchmarks.columns:
        fig.add_trace(
            go.Scatter(
                x=benchmarks.index,
                y=benchmarks["DIA"],
                mode="lines+markers",
                name="DIA",
            )
        )

    if "QQQ" in benchmarks.columns:
        fig.add_trace(
            go.Scatter(
                x=benchmarks.index,
                y=benchmarks["QQQ"],
                mode="lines+markers",
                name="QQQ",
            )
        )

    # Compute end of month for proper x-axis range
    end_of_month = (start_of_month + pd.offsets.MonthEnd(1)).normalize()

    fig.update_layout(
        title="Month-to-Date: Equal Weight Portfolio vs DIA & QQQ",
        xaxis_title="Date",
        yaxis_title="Index (Start=1)",
        template="plotly_white",
    )

    fig.update_xaxes(
        range=[start_of_month, end_of_month],  # show full month range
        dtick="D1",  # daily ticks
        tickformat="%d %b"  # 01 Mar
    )

    chart_html = pio.to_html(fig, full_html=False, include_plotlyjs=False)

    print(f"[DEBUG] chart_html_bytes={len(chart_html) if chart_html else 0}")

    return render_template(
        "dashboard.html",
        portfolio_settings=settings,
        chart_html=chart_html,
        tickers=tickers_data,
        portfolio_pct=portfolio_pct,
        last_updated=last_updated,
    )

@app.route("/tickers-refresh")
def tickers_refresh():

    ticker_records = PortfolioTicker.query.all()

    if not ticker_records:
        return ""

    tickers = [t.ticker for t in ticker_records]
    live_prices = get_live_prices(tickers)

    tickers_data = []
    current_prices = []

    for t in ticker_records:
        lp = live_prices.get(t.ticker, {})

        tickers_data.append({
            "ticker": t.ticker,
            "index": t.index,
            "price": lp.get("price"),
            "pct": lp.get("pct")
        })

        if lp.get("price") is not None:
            current_prices.append(lp["price"])

    # Equal-weight calculation (simple live)
    last_updated = datetime.now().strftime("%H:%M")

    return render_template(
        "tickers_partial.html",
        tickers=tickers_data,
        last_updated=last_updated,
    )



# -------------------------------
# ADMIN PANEL
# -------------------------------

@app.route("/admin") # just added
def admin():
    if not session.get("admin_logged_in"):
        return redirect("/")

    tickers = PortfolioTicker.query.all()
    emails = AlertEmail.query.all()
    settings = PortfolioSettings.query.first()  # current TP/SL

    #if request.args.get("modal") == "1":
        # Only return the admin HTML fragment for modal
    return render_template("admin.html", 
                           tickers=tickers, 
                           emails=emails, 
                           settings=settings
                           )
    
    

@app.route("/admin/upload", methods=["POST"])
def upload_portfolio():
    if not session.get("admin_logged_in"):
        return redirect("/")
        #return redirect("/login")

    file = request.files.get("file")
    df = pd.read_excel(file)

    df.columns = df.columns.str.strip().str.lower()

    required = {"ticker", "index"}
    if not required.issubset(set(df.columns)):
        flash("Upload must contain columns: ticker and index", "danger")
        return redirect("/")

    # Normalize column names
    df.columns = (
        df.columns
        .str.strip()      # remove spaces
        .str.lower()      # make lowercase
    )

    PortfolioTicker.query.delete()

    for _, row in df.iterrows():
        db.session.add(
            PortfolioTicker(
                ticker=row["ticker"].upper(),
                index=row["index"].upper()
            )
        )

    # 2️⃣ Reset portfolio state
    settings = PortfolioSettings.query.first()
    if settings:
        settings.tp1_hit = False
        settings.tp2_hit = False
        settings.tp3_hit = False
        settings.sl_hit = False

    db.session.commit()
    return redirect("/")


#++++++++++++++++ SET TPs and SL

@app.route("/admin/set_targets", methods=["POST"]) # just added
def admin_set_targets():
    if not session.get("admin_logged_in"):
        return redirect("/")

    tp1 = float(request.form.get("tp1"))
    tp2 = float(request.form.get("tp2"))
    tp3 = float(request.form.get("tp3"))
    stop_loss = float(request.form.get("stop_loss"))

    # validation
    if not (tp1 <= tp2 <= tp3):
        flash("TP must be ascending: TP1 ≤ TP2 ≤ TP3", "danger")
        return redirect("/")
    if stop_loss > tp1:
        flash("Stop loss must be ≤ TP1", "danger")
        return redirect("/")

    settings = PortfolioSettings.query.first()
    if not settings:
        settings = PortfolioSettings(
            tp1=tp1, 
            tp2=tp2, 
            tp3=tp3, 
            stop_loss=stop_loss)
        db.session.add(settings)
    else:
        settings.tp1 = tp1
        settings.tp2 = tp2
        settings.tp3 = tp3
        settings.stop_loss = stop_loss
        # reset hit flags
        settings.tp1_hit = False
        settings.tp2_hit = False
        settings.tp3_hit = False
        settings.sl_hit = False

    db.session.commit()
    flash("Portfolio TP/SL updated successfully", "success")
    return redirect("/")




#++++++++++++++ ADD EMAILS

@app.route("/admin/add_emails", methods=["POST"])
def admin_add_emails():
    if not session.get("admin_logged_in"):
        return jsonify({"flash": "Unauthorized", "category": "danger"}), 403

    added = 0
    duplicates = 0

    for i in range(5):
        email = request.form.get(f"email_{i}")
        if email:
            exists = AlertEmail.query.filter_by(email=email).first()
            if exists:
                duplicates += 1
            else:
                db.session.add(AlertEmail(email=email))
                added += 1

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({
            "flash": "Database error while saving emails",
            "category": "danger"
        }), 500

    # 🧠 Clear, user-friendly feedback
    if added and duplicates:
        msg = f"{added} email(s) added, {duplicates} duplicate(s) ignored."
        cat = "warning"
    elif added:
        msg = "Emails saved successfully."
        cat = "success"
    elif duplicates:
        msg = "All emails already exist."
        cat = "info"
    else:
        msg = "No emails submitted."
        cat = "secondary"

    return jsonify({"flash": msg, "category": cat})


#+++++++++++++DELETE EMAILS

@app.route("/admin/delete_email/<int:email_id>", methods=["POST"])
def admin_delete_email(email_id):
    if not session.get("admin_logged_in"):
        return jsonify({"flash": "Unauthorized", "category": "danger"}), 403

    email = AlertEmail.query.get(email_id)
    if not email:
        return jsonify({"flash": "Email not found", "category": "warning"}), 404

    db.session.delete(email)
    db.session.commit()

    return jsonify({
        "flash": f"{email.email} removed",
        "category": "success",
        "deleted_id": email_id
    })

# -------------------------------
# LOGIN (admin)
# -------------------------------

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")

    user = User.query.filter_by(username=username).first()

    if user and check_password_hash(user.password_hash, password):
        session["admin_logged_in"] = True
        return redirect("/")  # dashboard

    # ❌ wrong login
    flash("Wrong username or password", "danger")
    return redirect("/")

# -------------------------------
# LOGOUT
# -------------------------------

@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect("/")





#*****************************
# TEST EMAIL=============
@app.route("/admin/test-email", methods=["POST"])
def admin_test_email():
    if not session.get("admin_logged_in"):
        abort(403)

    emails = AlertEmail.query.all()
    if not emails:
        flash("No alert emails configured.", "warning")
        return redirect("/admin")

    subject = "📈 Test Alert – Portfolio Dashboard"
    body = "This is a SendGrid test email from your Portfolio Dashboard."

    success = send_portfolio_alert_thread(subject, body)

    if success:
        flash("✅ Test email sent successfully via SendGrid!", "success")
    else:
        flash("❌ Failed to send test email. Check logs and API key.", "danger")

    return redirect("/admin")


if __name__ == "__main__":
    app.run(debug=True,use_reloader=False)
