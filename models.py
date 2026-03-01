from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ----- Users -----
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

# ----- Portfolio Tickers -----
class PortfolioTicker(db.Model):
    __tablename__ = "portfolio_tickers"

    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(10), nullable=False, unique=True)
    index = db.Column(db.String(10))

# ----- Portfolio Settings -----
class PortfolioSettings(db.Model):
    __tablename__ = "portfolio_settings"

    id = db.Column(db.Integer, primary_key=True)

    # Portfolio-level targets (percent)
    tp1 = db.Column(db.Float)
    tp2 = db.Column(db.Float)
    tp3 = db.Column(db.Float)
    stop_loss = db.Column(db.Float)

    # State flags
    tp1_hit = db.Column(db.Boolean, default=False)
    tp2_hit = db.Column(db.Boolean, default=False)
    tp3_hit = db.Column(db.Boolean, default=False)
    sl_hit = db.Column(db.Boolean, default=False)

    # Alert check cooldown (prevents duplicate checks across multiple web workers)
    last_alert_check_at = db.Column(db.DateTime, nullable=True)

    updated_at = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now())

# ----- Alert Emails -----
class AlertEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)

    #def __repr__(self):
        #return f"<User {self.email}>"