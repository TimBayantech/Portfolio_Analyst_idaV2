from flask import Flask, render_template, request, session, redirect, flash, abort, jsonify
from flask_caching import Cache
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
from models import db, User, PortfolioTicker, PortfolioSettings, AlertEmail, Portfolio
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
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_timeout": 30,
    "max_overflow": 5,
}

# ✅ Bind SQLAlchemy to this app
db.init_app(app)

# ===============================
# ✅ Cache Setup (AFTER dotenv)
# ===============================
from flask_caching import Cache

cache_config = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300
}
cache = Cache(config=cache_config)
cache.init_app(app)

# ✅ Create tables
with app.app_context():
    db.create_all()


# ----------------------------
# Helper function to get live prices
# ----------------------------
@cache.memoize(timeout=60)
def get_live_prices(tickers):
    """
    Fetch live prices for a list of tickers using yfinance.
    Returns a dict: { ticker: {"price": float, "pct": float} }
    
    - Drops any NaN rows (e.g., US tickers before market open)
    - Safely handles tickers with less than 2 valid rows
    - pct = percentage change from previous trading day
    """
    data = yf.download(
        tickers,
        period="5d",  # last 5 days to calculate pct safely
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False
    )

    live_data = {}

    for t in tickers:
        try:
            # Handle multi-index columns (one dataframe per ticker) or single dataframe
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data

            # ================= DEBUGGING =================
            #print("Ticker:", t)
            #print(df["Close"].tail())
            #print("------------------")
            # ============================================

            # Keep only valid Close prices
            close = df["Close"].dropna()

            if len(close) == 0:
                # No data available
                live_data[t] = {"price": None, "pct": None}
                continue
            elif len(close) == 1:
                # Only 1 row -> pct cannot be calculated
                last = close.iloc[-1]
                pct = 0
            else:
                # Normal case: at least 2 valid rows
                last = close.iloc[-1]
                prev = close.iloc[-2]
                pct = (last / prev - 1) * 100 if prev != 0 else 0

            live_data[t] = {
                "price": round(last, 2),
                "pct": round(pct, 2)
            }

        except Exception as e:
            print(f"Error fetching {t}: {e}")
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
            return False

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
            return True

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

    # 🔒 CRITICAL FIX
    if portfolio_pct is None:
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
    print("SL HIT", settings.sl_hit)
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
# Check ticker -10% alert
# ----------------------------
def check_ticker_drawdown_alerts(tickers_data):

    triggered = []

    for t in tickers_data:
        pct = t.get("pct")

        if pct is not None and pct <= -10: # change back to -10%
            triggered.append({
                "ticker": t.get("ticker"),
                "pct": pct
            })

    if not triggered:
        return []

    subject = "🚨 Ticker Alert: -10% Drawdown"
    body = "The following tickers have dropped below -10% today:\n\n"
    body += "\n".join([f"{t['ticker']} ({t['pct']:.2f}%)" for t in triggered])

    send_portfolio_alert_async(subject, body)

    return triggered

@cache.memoize(timeout=300)
def get_dashboard_data(current_month):

    chart_html = None
    tickers_data = []
    portfolio_pct = None
    last_updated = None

    settings = PortfolioSettings.query.first()
    today = pd.Timestamp.today().normalize()
    portfolio = Portfolio.query.filter_by(month=current_month).first()

    if not portfolio:
        return {
            "message": f"No portfolio uploaded for {today.strftime('%B %Y')} yet.",
            "chart_html": None,
            "tickers": [],
            "portfolio_pct": None,
            "last_updated": None,
            "settings": settings
        }

    db_tickers = PortfolioTicker.query.filter_by(portfolio_id=portfolio.id).all()
    tickers = [t.ticker for t in db_tickers]

    if not tickers:
        return {
            "message": "Portfolio uploaded but contains no tickers.",
            "chart_html": None,
            "tickers": [],
            "portfolio_pct": None,
            "last_updated": None,
            "settings": settings
        }

    # ===============================
    # 👇 EVERYTHING BELOW IS YOUR ORIGINAL CODE (UNCHANGED)
    # ===============================

    first_portfolio_day = min((t.date_bought for t in db_tickers if t.date_bought),
                              default=portfolio.start_date)

    live_prices = get_live_prices(tickers)

    for t in db_tickers:
        lp = live_prices.get(t.ticker, {})
        tickers_data.append({
            "ticker": t.ticker,
            "index": t.index,
            "price": lp.get("price"),
            "pct": lp.get("pct"),
            "sold": bool(t.date_sold)
        })

    #check_ticker_drawdown_alerts(tickers_data)
    last_updated = datetime.now().strftime("%H:%M")

    start_of_month = pd.to_datetime(first_portfolio_day)
    end_of_month = (start_of_month + pd.offsets.MonthEnd(1)).normalize()
    all_days = pd.date_range(start=start_of_month, end=end_of_month, freq="D")

    hist_data = yf.download(
        tickers,
        start=first_portfolio_day,
        end=end_of_month + pd.Timedelta(days=1),
        interval="1d",
        auto_adjust=True,
        progress=False
    )

    portfolio_index = pd.Series(index=all_days, dtype=float)

    if not hist_data.empty:
        if isinstance(hist_data.columns, pd.MultiIndex):
            prices = hist_data["Close"].copy()
        else:
            prices = hist_data[["Close"]].rename(columns={"Close": tickers[0]})
        prices.index = pd.to_datetime(prices.index)

        for t in db_tickers:
            if t.ticker not in prices.columns:
                continue

            buy_day = pd.to_datetime(t.date_bought)
            if buy_day not in prices.index:
                buy_day = prices.index[prices.index.get_loc(buy_day, method="bfill")]

            end_day = pd.to_datetime(t.date_sold) if t.date_sold else prices.index[-1]
            mask = (prices.index >= buy_day) & (prices.index <= end_day)

            prices.loc[prices.index < buy_day, t.ticker] = None
            prices.loc[prices.index > end_day, t.ticker] = None

            buy_price = t.buy_price or prices.loc[buy_day, t.ticker]
            if pd.isna(buy_price):
                continue

            prices.loc[mask, t.ticker] = prices.loc[mask, t.ticker] / buy_price
            prices.loc[mask, t.ticker] = prices.loc[mask, t.ticker].ffill()
            prices.loc[buy_day, t.ticker] = 1

        prices = prices.reindex(all_days)

        active_counts = prices.notna().sum(axis=1).replace(0, None)
        portfolio_index = prices.sum(axis=1) / active_counts
        portfolio_index = portfolio_index.ffill()
        portfolio_index = portfolio_index.replace([float("inf"), -float("inf")], None)

    latest_day = portfolio_index.last_valid_index()
    portfolio_pct = round((portfolio_index.loc[latest_day] - 1) * 100, 2) if latest_day else None
    #check_and_send_portfolio_alerts(settings, portfolio_pct)
    print("🔥 CACHE MISS - recalculating")

    raw_benchmarks = yf.download(
        ["^DJI", "^IXIC", "^FTSE"],
        start=first_portfolio_day,
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
        first_day = benchmarks.index.min()
        benchmarks = benchmarks / benchmarks.loc[first_day]
        benchmarks = benchmarks.reindex(all_days).ffill()

    portfolio_index.loc[portfolio_index.index > today] = None
    if not benchmarks.empty:
        benchmarks.loc[benchmarks.index > today] = None

    message = None
    if portfolio_index.dropna().empty:
        message = "No trading data available yet for this month."

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
                                 

    # ✅ Add chart 
    fig.update_layout(
        title={
            'text': f"Portfolio Performance for {first_portfolio_day.strftime('%B %Y')}",  # Full month name
            'x': 0.5,
            'xanchor': 'center',
            'yanchor': 'top',
            'font': dict(
                size=20,       # font size
                family="Arial, sans-serif",
                color="black"  # optional
                # weight not directly supported, use a bolder font family if needed
            )
        },
        xaxis_title="Date",
        yaxis_title="Index (Start=1)",
        template="plotly_white",
        xaxis=dict(tickformat="%d-%m", tickmode="auto", nticks=10)
    )

    # Set y-axis range with slight padding
    all_values = pd.concat([
        portfolio_index.dropna(),
        benchmarks.stack().dropna() if not benchmarks.empty else pd.Series()
    ])
    if not all_values.empty:
        ymin = all_values.min()
        ymax = all_values.max()
        padding = 0.02
        fig.update_layout(yaxis=dict(range=[ymin - padding, ymax + padding]))    
            
    chart_html = pio.to_html(fig, full_html=False, include_plotlyjs="cdn")

    return {
        "chart_html": chart_html,
        "tickers": tickers_data,
        "portfolio_pct": portfolio_pct,
        "last_updated": last_updated,
        "message": message,
        "settings": settings
    }






# ----------------------------
# Main dashboard route
# ----------------------------
@app.route("/")
def dashboard():
    today = pd.Timestamp.today().normalize()
    current_month = today.strftime("%Y-%m")

    data = get_dashboard_data(current_month)

    # ✅ RUN ALERTS OUTSIDE CACHE
    check_ticker_drawdown_alerts(data["tickers"])
    check_and_send_portfolio_alerts(data["settings"], data["portfolio_pct"])

    return render_template(
        "dashboard.html",
        portfolio_settings=data["settings"],
        chart_html=data["chart_html"],
        tickers=data["tickers"],
        portfolio_pct=data["portfolio_pct"],
        last_updated=data["last_updated"],
        message=data["message"]
    )

@app.route("/tickers-refresh")
def tickers_refresh():
    today = pd.Timestamp.today().normalize()
    current_month = today.strftime("%Y-%m")

    portfolio = Portfolio.query.filter_by(month=current_month).first()

    if not portfolio:
        return render_template("tickers_partial.html", tickers=[], last_updated=None)

    ticker_records = PortfolioTicker.query.filter_by(portfolio_id=portfolio.id).all()

    if not ticker_records:
        return render_template("tickers_partial.html", tickers=[], last_updated=None)

    tickers = [t.ticker for t in ticker_records]
    live_prices = get_live_prices(tickers)

    tickers_data = []

    for t in ticker_records:
        lp = live_prices.get(t.ticker, {})

        tickers_data.append({
            "ticker": t.ticker,
            "index": t.index,
            "price": lp.get("price"),
            "pct": lp.get("pct"),
            "sold": bool(t.date_sold)
        })

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

    print(df.isnull().sum())
    print(df.head())

    # Normalize column names
    df.columns = (
        df.columns
        .str.strip()      # remove spaces
        .str.lower()      # make lowercase
    )

    print("****DEBUG")
    print(df.columns)

    # strip spaces just in case
    df["ticker"] = df["ticker"].str.strip()
    # Convert date columns properly
    df["date_bought"] = pd.to_datetime(df["date_bought"]).dt.date
    # Convert date_sold if present
    df["date_sold"] = df["date_sold"].replace({pd.NaT: None, float("nan"): None})     

    # Replace NaN / NaT with None
    df = df.where(pd.notnull(df), None)

    # Remove blank rows
    df = df.dropna(subset=["ticker"])

    # Determine portfolio month and start date
    today = pd.Timestamp.today().normalize()
    portfolio_month = today.strftime("%Y-%m")

    # Optional: check if portfolio already exists for month
    portfolio = Portfolio.query.filter_by(month=portfolio_month).first()
    if not portfolio:
        # Assume first trading day = today; could enhance by checking yfinance for first trading day
        portfolio = Portfolio(month=portfolio_month, start_date=today)
        db.session.add(portfolio)
        db.session.commit()

    # Clear existing tickers for this month
    PortfolioTicker.query.filter_by(portfolio_id=portfolio.id).delete()

    # Add new tickers for current month
    for _, row in df.iterrows():
        db.session.add(
            PortfolioTicker(
                ticker=row["ticker"].upper(),
                index=row["index"].upper(),
                portfolio_id=portfolio.id,
                buy_price=row["buy_price"],  # optional column in Excel
                date_bought=row["date_bought"],
                date_sold=row.get("date_sold") if pd.notna(row["date_sold"]) else None
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
    # ✅ CLEAR CACHE
    cache.delete_memoized(get_dashboard_data)

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

    # ✅ CLEAR CACHE
    cache.delete_memoized(get_dashboard_data)

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
        # ✅ CLEAR CACHE
        cache.delete_memoized(get_dashboard_data)

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

    # ✅ CLEAR CACHE
    cache.delete_memoized(get_dashboard_data)


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
