import os
from app import db, User, app
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

# Read from environment
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise ValueError("ADMIN_USERNAME and ADMIN_PASSWORD must be set in environment")

with app.app_context():

    existing = User.query.filter_by(username=ADMIN_USERNAME).first()
    if existing:
        print("Admin user already exists. Skipping creation.")
    else:
        admin = User(
            username=ADMIN_USERNAME,
            password_hash=generate_password_hash(ADMIN_PASSWORD)
        )

        db.session.add(admin)
        db.session.commit()
        print("Admin user created successfully!")