from flask import Flask, render_template, request, session, redirect, flash, abort, jsonify
import pandas as pd
import yfinance as yf
import plotly.graph_objs as go
import plotly.io as pio
import requests
import time
from typing import Any, Optional, Dict, Tuple

# ===================== Alpha Vantage helpers (quotes + MTD) =====================
AV_BASE_URL = "https://www.alphavantage.co/query"
AV_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY") or os.environ.get("ALPHA_VANTAGE_API_KEY")

# very small in-process cache to reduce API calls
_AV_CACHE: Dict[str, Tuple[float, Any]] = {}  # key -> (expires_epoch, value)

def _cache_get(key: str):
    item = _AV_CACHE.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _AV_CACHE.pop(key, None)
        return None
    return val

def _cache_set(key: str, val: Any, ttl_seconds: int):
    _AV_CACHE[key] = (time.time() + ttl_seconds, val)

def _av_get(params: Dict[str, str], ttl_seconds: int = 60) -> Optional[dict]:
    """GET Alpha Vantage JSON with basic caching + error handling."""
    if not AV_API_KEY:
        return None
    params = dict(params)
    params["apikey"] = AV_API_KEY

    cache_key = "av:" + json.dumps(params, sort_keys=True)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(AV_BASE_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    # AV will return "Note" when throttled, or "Error Message" for bad tickers
    if not isinstance(data, dict) or data.get("Note") or data.get("Error Message"):
        _cache_set(cache_key, data, 30)  # short cache to avoid hammering
        return data

    _cache_set(cache_key, data, ttl_seconds)
    return data

def av_global_quote(symbol: str) -> Optional[float]:
    data = _av_get({"function": "GLOBAL_QUOTE", "symbol": symbol}, ttl_seconds=60)
    if not data or not isinstance(data, dict):
        return None
    q = data.get("Global Quote") or {}
    price_str = q.get("05. price") or q.get("05. price ".strip())
    try:
        return float(price_str)
    except Exception:
        return None

def av_month_start_close(symbol: str, month_start: datetime.date) -> Optional[float]:
    """Return the first trading day's close within the given month, else None."""
    month_key = month_start.strftime("%Y-%m")
    cache_key = f"mstart:{symbol}:{month_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _av_get({"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol, "outputsize": "compact"}, ttl_seconds=15*60)
    if not data or not isinstance(data, dict):
        _cache_set(cache_key, None, 5*60)
        return None

    ts = data.get("Time Series (Daily)")
    if not isinstance(ts, dict) or not ts:
        _cache_set(cache_key, None, 5*60)
        return None

    # find first trading day within the month
    dates_in_month = [d for d in ts.keys() if d.startswith(month_key)]
    if not dates_in_month:
        _cache_set(cache_key, None, 30*60)
        return None

    first_day = min(dates_in_month)
    try:
        close_str = ts[first_day].get("4. close")
        val = float(close_str)
    except Exception:
        val = None

    _cache_set(cache_key, val, 6*60*60)
    return val

def compute_mtd_pct(symbol: str, price_now: Optional[float], month_start: datetime.date) -> Optional[float]:
    """
    MTD % = (current_price / first_trading_day_close_in_month - 1) * 100.
    If the month has not had a trading day yet, return 0.0.
    """
    if price_now is None:
        return None
    start_close = av_month_start_close(symbol, month_start)
    if start_close is None or start_close == 0:
        return 0.0
    return (price_now / start_close - 1.0) * 100.0

from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
import os
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError
import threading
from models import db, User, PortfolioTicker, PortfolioSettings, AlertEmail
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv

load_dotenv()  # reads .env into os.environ

# ===============================
# Flask App Initialization
# ===============================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")

database_url = os.environ.get("DATABASE_URL", "sqlite:///portfolio.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,     # ✅ fixes SSL EOF errors
    "pool_recycle": 300,       # recycle connections every 5 minutes
}

# ✅ Bind SQLAlchemy to this app
db.init_app(app)

# ✅ Create tables
with app.app_context():
    db.create_all()

if os.environ.get("RENDER"):
    app.config["ENV"] = "production"

    
# ----------------------------
# Helper function to get live prices
# ----------------------------
def get_live_prices(tickers):
    """
    Returns dict: {symbol: {"price": float|None, "pct": float|None}}
    where pct is Month-to-Date % (equal weight logic uses avg of these).
    """
    month_start = datetime(datetime.now().year, datetime.now().month, 1).date()
    out = {}

    for t in tickers:
        sym = (t or "").strip().upper()
        if not sym:
            continue

        # GLOBAL_QUOTE is the simplest consistent "delayed" quote for US tickers
        price = av_global_quote(sym)
        pct = compute_mtd_pct(sym, price, month_start)

        out[sym] = {"price": price, "pct": pct}

    return out


# ----------------------------
# Send Emails When TP/SL hit
# ----------------------------
# Unified email sender
# ----------------------------
# Send Emails via SendGrid API
# ----------------------------
def send_portfolio_alert_thread(subject, body):
    with app.app_context():
        emails = [e.email for e in AlertEmail.query.all()]
        if not emails:
            return False

        try:
            sg = SendGridAPIClient(os.environ.get("SENDGRID_API_KEY"))

            message = Mail(
                from_email=os.environ.get("ALERT_FROM_EMAIL"),
                to_emails=emails,
                subject=subject,
                plain_text_content=body,
            )

            sg.send(message)
            return True

        except Exception:
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

    # 🔒 CRITICAL FIX
    if portfolio_pct is None:
        return
    

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
    # MONTHLY DASHBOARD
    # ------------------------
    today = pd.Timestamp.today().normalize()
    start_of_month = today.replace(day=1)
    end_of_month = (start_of_month + pd.offsets.MonthEnd(1)).normalize()

    # Full month calendar
    all_days = pd.date_range(start=start_of_month, end=today, freq="D")

    # 1️⃣ Download portfolio historical data
    hist_data = yf.download(
        tickers,
        start=start_of_month,
        end=end_of_month + pd.Timedelta(days=1),
        interval="1d",
        auto_adjust=True,
        progress=False
    )

    if hist_data.empty:
        portfolio_index = pd.Series(index=all_days, dtype=float)
        portfolio_pct = None
    else:
        prices = hist_data["Close"] if isinstance(hist_data.columns, pd.MultiIndex) else hist_data["Close"].to_frame()
        prices.index = pd.to_datetime(prices.index)
        prices = prices[prices.index >= start_of_month]

        if prices.empty:
            portfolio_index = pd.Series(index=all_days, dtype=float)
            portfolio_pct = None
        else:
            # --- Normalize by first trading day of the month ---
            first_trading_day = prices.index.min()
            prices = prices / prices.loc[first_trading_day]

            # --- Reindex to full month calendar, forward-fill ---
            prices = prices.reindex(all_days).ffill()

            # --- Equal-weight portfolio ---
            portfolio_index = prices.mean(axis=1)

            # Portfolio % change relative to first trading day
            portfolio_pct = round((portfolio_index.loc[today] - 1) * 100, 2) if today in portfolio_index.index else None

    # Check alerts
    check_and_send_portfolio_alerts(settings, portfolio_pct)

    # 2️⃣ Download benchmark indices
    raw_benchmarks = yf.download(
        ["^DJI", "^IXIC", "^FTSE"],
        start=start_of_month,
        end=end_of_month + pd.Timedelta(days=1),
        interval="1d",
        auto_adjust=True,
        progress=False
    )

    if raw_benchmarks.empty:
        benchmarks = pd.DataFrame(index=all_days)
    else:
        benchmarks = raw_benchmarks["Close"]
        benchmarks.index = pd.to_datetime(benchmarks.index)
        benchmarks = benchmarks[benchmarks.index >= start_of_month]

        if not benchmarks.empty:
            first_trading_day_benchmark = benchmarks.index.min()
            benchmarks = benchmarks / benchmarks.loc[first_trading_day_benchmark]
            benchmarks = benchmarks.reindex(all_days).ffill()
    # (No need to blank future days since we only plot up to 'today')

    # Check if there is any actual data to display
    message = None
    if portfolio_index.dropna().empty:
        message = "No trading data available yet for this month."
    

    # 3️⃣ Plotly chart
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=portfolio_index.index, y=portfolio_index.values,
                             mode="lines+markers", name="Portfolio"))

    if "^DJI" in benchmarks.columns:
        fig.add_trace(go.Scatter(x=benchmarks.index, y=benchmarks["^DJI"],
                                 mode="lines+markers", name="DOW"))
    if "^IXIC" in benchmarks.columns:
        fig.add_trace(go.Scatter(x=benchmarks.index, y=benchmarks["^IXIC"],
                                 mode="lines+markers", name="NASDAQ"))
    if "^FTSE" in benchmarks.columns:
        fig.add_trace(go.Scatter(x=benchmarks.index, y=benchmarks["^FTSE"],
                                mode="lines+markers", name="FTSE 100"))

    fig.update_layout(
        title="Monthly Portfolio vs DOW & NASDAQ",
        xaxis_title="Date",
        yaxis_title="Index (Start=1)",
        template="plotly_white",
        xaxis=dict(
            tickformat="%Y-%m-%d",
            tickmode="auto",
            nticks=10
        )
    )


    # Override portfolio_pct to be the equal-weight MTD = avg of ticker MTD % values (from tiles)
    _mtd_vals = [t.get("pct") for t in tickers_data if t.get("pct") is not None]
    if _mtd_vals:
        portfolio_pct = round(sum(_mtd_vals) / len(_mtd_vals), 2)
    else:
        portfolio_pct = 0.0
    chart_html = pio.to_html(fig, full_html=False, include_plotlyjs=False)

    return render_template("dashboard.html", 
                            portfolio_settings=settings,
                            chart_html=chart_html, 
                            tickers=tickers_data,
                            portfolio_pct=portfolio_pct,
                            last_updated=last_updated,
                            message=message
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
