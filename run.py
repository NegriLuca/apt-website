import os
import subprocess
from app import create_app, db
from app.models import User, Apartment, Reservation

# ── AUTO-COMPILE BABEL TRANSLATIONS ──
# This builds your binary .mo files directly inside the Railway container on startup
try:
    print("🌐 Compiling application translation catalogs via Babel...")
    # Executes: pybabel compile -d app/translations
    # We target 'app/translations' to match your app.root_path configuration
    result = subprocess.run(
        ["pybabel", "compile", "-d", "translations"],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        print("✅ Translation compilation successful!")
    else:
        print(f"⚠️ Babel compilation returned non-zero setup: {result.stderr}")
except Exception as e:
    print(f"❌ Failed to run pybabel compile programmatically: {e}")


app = create_app()

with app.app_context():
    db.create_all()  # Create tables if they don't exist

    # ── DB SCHEMA PATCH: Force inject missing column if using an existing DB ──
    try:
        engine = db.engine
        with engine.connect() as conn:
            try:
                from sqlalchemy import text
                conn.execute(text("ALTER TABLE reservations ADD COLUMN coupon_code VARCHAR(20) NULL;"))
                conn.commit()
                print("🛠️ Database schema updated: coupon_code column injected into reservations.")
            except Exception:
                pass
    except Exception as e:
        print(f"ℹ️ Schema check skipped or unneeded: {e}")

    # ── AUTO-CREATE & SYNC ADMIN FROM .ENV ──
    env_password = os.environ.get('ADMIN_PASSWORD')
    
    if env_password:
        admin_user = User.query.filter_by(username='admin').first()
        
        if not admin_user:
            print("👤 Admin user not found. Creating a fresh admin account...")
            admin_user = User(
                username='admin',
                is_admin=True 
            )
            db.session.add(admin_user)
        
        admin_user.is_admin = True
        admin_user.set_password(env_password)
        db.session.commit()
        print("🔒 Admin account verified and password securely synchronized!")
    else:
        print("⚠️ Warning: ADMIN_PASSWORD not found in .env file. Admin setup skipped.")

    # ── AUTO-CREATE DEFAULT APARTMENT IF EMPTY ──
    default_apartment = Apartment.query.first()
    if not default_apartment:
        print("🏠 No properties found. Seeding default apartment profile...")
        default_apartment = Apartment(
            name="Lotto 235 Garbatella",
            price_per_night=120.00,
            image_file="apartment/living_room.jpg" 
        )
        db.session.add(default_apartment)
        db.session.commit()
        print("✅ Default apartment successfully seeded!")        

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)