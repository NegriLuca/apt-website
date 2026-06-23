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


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)