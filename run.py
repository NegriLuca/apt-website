import os
import subprocess
import time  # Importiamo time per la pausa di attesa

from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.models import Apartment, User

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
    # ── CONNESSIONE RESILIENTE AL DB (Anti-Crash per rete interna) ──
    db_connected = False
    retries = 5
    while not db_connected and retries > 0:
        try:
            print(f"🔌 Tentativo di connessione al database... (Rimasti: {retries})")
            db.create_all()
            from flask_migrate import upgrade
            upgrade(directory='migrations', revision='head')
            db_connected = True
            print("💾 Database connesso con successo sulla rete interna privata!")
        except OperationalError as e:
            retries -= 1
            if retries == 0:
                print("❌ Impossibile connettersi al database dopo 5 tentativi. Crash programmato.")
                raise e
            print("⏳ Il database interno non è ancora pronto. Attendo 3 secondi...")
            time.sleep(3)

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
            price_per_night=130.00,
            image_file="apartment/living_room.jpg",
            cin_code=os.environ.get('CIN_CODE', 'IT058091C2TXZ44TA6'),
            cir_code=os.environ.get('CIR_CODE', '058091-LOC-19856'),
        )
        db.session.add(default_apartment)
        db.session.commit()
        print("✅ Default apartment successfully seeded!")

    # ── SYNC NUKI CONFIG FROM ENV VARS ──
    # Overrides the Smart Access page settings on every startup when set.
    nuki_id = os.environ.get('NUKI_SMARTLOCK_ID', '').strip()
    nuki_token = os.environ.get('NUKI_WEB_TOKEN', '').strip()
    if nuki_id or nuki_token:
        print("🔑 Syncing Nuki smart lock config from env vars...")
        if default_apartment is None:
            default_apartment = Apartment.query.first()
        default_apartment.nuki_smartlock_id = nuki_id or default_apartment.nuki_smartlock_id
        default_apartment.nuki_web_token = nuki_token or default_apartment.nuki_web_token
        default_apartment.nuki_web_base_url = os.environ.get('NUKI_WEB_BASE_URL', '').strip() or default_apartment.nuki_web_base_url or 'https://api.nuki.io'
        default_apartment.nuki_unlock_action = os.environ.get('NUKI_UNLOCK_ACTION', '').strip() or 'unlatch'
        default_apartment.nuki_enabled = bool(nuki_id and nuki_token)
        db.session.commit()
        print(f"✅ Nuki config synced (enabled={default_apartment.nuki_enabled}, action={default_apartment.nuki_unlock_action}).")

    # ── SYNC WIFI CONFIG FROM ENV VARS ──
    # Overrides the Wi-Fi settings page on every startup when set.
    from app.services.wifi_qr import sync_wifi_from_env

    sync_wifi_from_env()

    # ── SYNC BOILER SHELLY CONFIG FROM ENV VARS ──
    # Seeds/keeps the boiler device configured when env var is set (same cloud account as gate).
    boiler_device = os.environ.get('SHELLY_BOILER_DEVICE_ID', '').strip()
    boiler_channel = os.environ.get('SHELLY_BOILER_CHANNEL', '').strip()
    boiler_host = os.environ.get('SHELLY_BOILER_HOST', '').strip()
    if boiler_device or boiler_host:
        print(f"🔥 Syncing boiler Shelly from env vars (device={boiler_device or '—'} ch={boiler_channel or '0'})...")
        if default_apartment is None:
            default_apartment = Apartment.query.first()
        if default_apartment is not None:
            if boiler_device:
                default_apartment.boiler_shelly_device_id = boiler_device
            if boiler_channel != '':
                try:
                    default_apartment.boiler_shelly_channel = int(boiler_channel)
                except ValueError:
                    pass
            if boiler_host:
                default_apartment.boiler_shelly_host = boiler_host
            # Auto-enable if device is present and not explicitly disabled
            if boiler_device and not default_apartment.boiler_shelly_enabled:
                default_apartment.boiler_shelly_enabled = True
            db.session.commit()
            print(f"✅ Boiler Shelly synced (enabled={default_apartment.boiler_shelly_enabled}, device={default_apartment.boiler_shelly_device_id}, ch={default_apartment.boiler_shelly_channel}).")

    # ── SYNC HOST / RICEVUTA CONFIG FROM ENV VARS (Railway) ──
    # Env vars override DB on every startup so Railway redeploys stay consistent.
    host_name = os.environ.get('HOST_FULL_NAME', '').strip()
    host_cf = os.environ.get('HOST_CODICE_FISCALE', '').strip()
    host_addr = os.environ.get('HOST_ADDRESS', '').strip()
    # keep reference to apartment created above
    apt_for_host = Apartment.query.first()
    if apt_for_host is not None and (host_name or host_cf or host_addr):
        updated = False
        if host_name:
            apt_for_host.host_full_name = host_name
            updated = True
        if host_cf:
            apt_for_host.host_codice_fiscale = host_cf
            updated = True
        if host_addr:
            apt_for_host.host_address = host_addr
            updated = True
        if host_name or host_cf:
            # default address if still empty
            if not apt_for_host.host_address:
                apt_for_host.host_address = os.environ.get('HOST_ADDRESS', 'Via Lotto 235, 00153 Roma')
                updated = True
        if updated:
            db.session.commit()
            print(f"🧾 Host ricevuta synced from env: {apt_for_host.host_full_name or '—'} CF {apt_for_host.host_codice_fiscale or '—'}")
    # ensure CIN/CIR also synced from env if changed
    if apt_for_host is not None:
        env_cin = os.environ.get('CIN_CODE', '').strip()
        env_cir = os.environ.get('CIR_CODE', '').strip()
        if env_cin and apt_for_host.cin_code != env_cin:
            apt_for_host.cin_code = env_cin
            db.session.commit()
        if env_cir and apt_for_host.cir_code != env_cir:
            apt_for_host.cir_code = env_cir
            db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
