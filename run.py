import os
from app import create_app, db
from app.models import User, Apartment, Reservation

app = create_app()

with app.app_context():
    db.create_all()  # Create tables if they don't exist

    # ── DB SCHEMA PATCH: Force inject missing column if using an existing DB ──
    try:
        # Check if coupon_code already exists, if not, add it dynamically
        engine = db.engine
        with engine.connect() as conn:
            # For PostgreSQL / SQLite: Safely try to alter the table
            # Wrap in try/except so it doesn't fail if the column is already there
            try:
                from sqlalchemy import text
                conn.execute(text("ALTER TABLE reservations ADD COLUMN coupon_code VARCHAR(20) NULL;"))
                conn.commit()
                print("🛠️ Database schema updated: coupon_code column injected into reservations.")
            except Exception:
                # If it fails, the column likely already exists, which is perfect
                pass
    except Exception as e:
        print(f"ℹ️ Schema check skipped or unneeded: {e}")

    # ── AUTO-CREATE & SYNC ADMIN FROM .ENV ──
    env_password = os.environ.get('ADMIN_PASSWORD')
    
    if env_password:
        # Query using the exact 'username' field on your model
        admin_user = User.query.filter_by(username='admin').first()
        
        if not admin_user:
            print("👤 Admin user not found. Creating a fresh admin account...")
            admin_user = User(
                username='admin',
                # Ensure they are granted admin privileges in the database
                is_admin=True 
            )
            db.session.add(admin_user)
        
        # Explicitly make sure an existing account is flagged as admin just in case
        admin_user.is_admin = True
        
        # Safely hash the password using your model's native method
        admin_user.set_password(env_password)
        
        db.session.commit()
        print("🔒 Admin account verified and password securely synchronized!")
    else:
        print("⚠️ Warning: ADMIN_PASSWORD not found in .env file. Admin setup skipped.")

    # ── 2. AUTO-CREATE DEFAULT APARTMENT IF EMPTY ──
    default_apartment = Apartment.query.first()
    if not default_apartment:
        print("🏠 No properties found. Seeding default apartment profile...")
        default_apartment = Apartment(
            name="My Cozy Suite",
            description="Welcome to our beautiful, fully equipped rental property.", 
            price_per_night=120.00,
            image_file="apartment/living_room.jpg" 
        )
        db.session.add(default_apartment)
        db.session.commit()
        print("✅ Default apartment successfully seeded!")        

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)