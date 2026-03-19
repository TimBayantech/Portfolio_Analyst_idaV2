from app import db, User, app
from werkzeug.security import generate_password_hash

# Create admin user
with app.app_context():
    admin = User(
        username="admin",
        password_hash=generate_password_hash("MoreBlessings")
    )

    db.session.add(admin)
    db.session.commit()
    print("Admin user created!")

# to delete after installation - not for public

