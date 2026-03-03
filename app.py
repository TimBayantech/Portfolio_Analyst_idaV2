from flask import Flask, render_template, request, session, redirect, flash, abort, jsonify
import pandas as pd
import yfinance as yf
import plotly.graph_objs as go
import plotly.io as pio
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
import requests
import time

load_dotenv()  # reads .env into os.environ

ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY") or os.environ.get("ALPHAVANTAGE_API_KEY")

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


# ----------------------------
# Health Check (Render)
# ----------------------------
@app.get("/health")
def health():
    return "ok", 200

# ✅ Create tables
with app.app_context():
    db.create_all()

if os.environ.get("RENDER"):
    app.config["ENV"] = "production"

    
# ----------------------------
# Helper function to get live prices
# ----------------------------
# ----------------------------
# Quotes for tiles
# - Prefer Alpha Vantage (GLOBAL_QUOTE) when ALPHA_VANTAGE_API_KEY is set
# - Fallback to yfinance locally if AV key is missing
# ----------------------------

_AV_QUOTE_CACHE = {}  # symbol -> (ts_epoch, {"price": float|None, "pct": float|None})
_AV_QUOTE_TTL_SECONDS = int(os.environ.get("AV_QUOTE_TTL_SECONDS", "120"))  # 2 mins default

def _get_live_prices_yf(tickers):
    data = yf.download(
        tickers,
        period="2d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False
    )
    live_data = {}
    for t in tickers:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            last = float(df["Close"].iloc[-1])
            prev = float(df["Close"].iloc[-2])
            pct = (last / prev - 1) * 100
            live_data[t] = {"price": round(last, 2), "pct": round(pct, 2)}
        except Exception:
            live_data[t] = {"price": None, "pct": None}
    return live_data


def _av_global_quote(symbol: str, attempt: int = 1, max_attempts: int = 3):
    """Fetch a single GLOBAL_QUOTE from Alpha Vantage. Returns (price, prev_close) floats or (None, None)."""
    if not ALPHA_VANTAGE_API_KEY:
        return None, None

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "GLOBAL_QUOTE",
        "symbol": symbol,
        "apikey": ALPHA_VANTAGE_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if attempt < max_attempts:
            time.sleep(0.7 * attempt)
            return _av_global_quote(symbol, attempt + 1, max_attempts)
        return None, None

    # Rate limit / error payloads
    if isinstance(data, dict) and (data.get("Note") or data.get("Information")):
        msg = data.get("Note") or data.get("Information")
        print(f"⚠️ Alpha Vantage note for {symbol}: {msg}")
        if attempt < max_attempts:
            # Back off a bit more when AV tells us to slow down
            time.sleep(2.0 * attempt)
            return _av_global_quote(symbol, attempt + 1, max_attempts)
        return None, None

    q = (data or {}).get("Global Quote") or {}
    price_s = q.get("05. price") or q.get("05. Price")
    prev_s = q.get("08. previous close") or q.get("08. Previous close") or q.get("08. Previous Close")

    try:
        price = float(price_s) if price_s not in (None, "", "None") else None
        prev = float(prev_s) if prev_s not in (None, "", "None") else None
    except Exception:
        return None, None

    return price, prev


def _get_live_prices_av(tickers):
    now = time.time()
    out = {}

    # Use cache first
    to_fetch = []
    for sym in tickers:
        cached = _AV_QUOTE_CACHE.get(sym)
        if cached and (now - cached[0]) <= _AV_QUOTE_TTL_SECONDS:
            out[sym] = cached[1]
        else:
            to_fetch.append(sym)

    # Throttle to avoid burst (keep <= 5 req/sec)
    # 0.25s spacing -> 4 req/sec
    for i, sym in enumerate(to_fetch):
        price, prev = _av_global_quote(sym)
        if price is None or prev is None or prev == 0:
            payload = {"price": None, "pct": None}
        else:
            pct = (price / prev - 1) * 100
            payload = {"price": round(price, 2), "pct": round(pct, 2)}

        _AV_QUOTE_CACHE[sym] = (time.time(), payload)
        out[sym] = payload

        # Spread requests evenly (except after last)
        if i < len(to_fetch) - 1:
            time.sleep(0.25)

    return out


def get_live_prices(tickers):
    """Unified tile quotes getter."""
    # Prefer Alpha Vantage for production stability
    if ALPHA_VANTAGE_API_KEY:
        return _get_live_prices_av(tickers)
    return _get_live_prices_yf(tickers)


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
    all_days = pd.date_range(start=start_of_month, end=end_of_month, freq="D")

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

    # Optional: blank future days
    portfolio_index.loc[portfolio_index.index > today] = None
    if not benchmarks.empty:
        benchmarks.loc[benchmarks.index > today] = None

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

    chart_html = fig.to_html(full_html=False, include_plotlyjs=False)

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
    app.run(debug=True)
