import os
from app import create_app, db
from app.models import User, Apartment, Reservation

app = create_app()

with app.app_context():
    db.create_all()  # Create tables if they don't exist

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
            description="Welcome to our beautiful, fully equipped rental property.", # Added to fix NOT NULL error
            price_per_night=120.00,
            image_file="apartment/living_room.jpg" # Safe fallback asset name
        )
        db.session.add(default_apartment)
        db.session.commit()
        print("✅ Default apartment successfully seeded!")        

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)