from flask import Flask, request, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_mail import Mail
from flask_bcrypt import Bcrypt
from flask_babel import Babel

from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()
mail = Mail()
bcrypt = Bcrypt()
babel = Babel()

login_manager.login_view = 'routes.login'
login_manager.login_message_category = 'info'

def get_locale():
    # 1. Check if the user manually switched language and stored it in the session
    if 'language' in session:
        return session['language']
    
    # 2. Otherwise, fall back to the user's browser settings
    return request.accept_languages.best_match(['en', 'it', 'de', 'fr', 'es'])

def create_app():
    app = Flask(__name__)
    

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    app.config['BABEL_TRANSLATION_DIRECTORIES'] = 'translations'

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    mail.init_app(app)
    bcrypt.init_app(app)

    # Initialize Babel with the selector function
    babel.init_app(app, locale_selector=get_locale)

    from app import routes
    app.register_blueprint(routes.bp)

    # ── Start APScheduler for periodic iCal sync ──────────────────────────────
    _start_scheduler(app)

    return app


def _start_scheduler(app: Flask):
    """
    Start a background APScheduler that calls sync_all_feeds() every N minutes.
    Only starts when NOT inside a Flask CLI command or when Werkzeug reloader
    is active (to avoid double-start in debug mode).
    """
    import os
    # Werkzeug starts two processes in debug mode; only schedule in the child.
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'false':
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        app.logger.warning(
            'APScheduler not installed — iCal auto-sync disabled. '
            'Run: pip install apscheduler'
        )
        return

    interval = app.config.get('ICAL_SYNC_INTERVAL_MINUTES', 30)

    def _sync_job():
        with app.app_context():
            from app.services.ical_sync import sync_all_feeds
            added, cancelled, errors = sync_all_feeds()
            if errors:
                app.logger.warning('iCal sync errors: %s', errors)
            else:
                app.logger.info('iCal auto-sync: +%d / -%d', added, cancelled)

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _sync_job,
        trigger='interval',
        minutes=interval,
        id='ical_sync',
        replace_existing=True,
    )
    scheduler.start()
    app.logger.info('iCal scheduler started (every %d min)', interval)