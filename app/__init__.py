import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, current_app, jsonify, render_template, request, session
from flask_babel import Babel, format_date, format_datetime
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_moment import Moment
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect

from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()
mail = Mail()
bcrypt = Bcrypt()
babel = Babel()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=['200 per day', '50 per hour'],
    storage_uri='memory://',
)


login_manager.login_view = 'routes.login'
login_manager.login_message_category = 'info'


def get_locale():
    # Elenco delle lingue supportate dall'applicazione
    supported_languages = ['en', 'it', 'de', 'fr', 'es']

    # 1. Controlla se l'utente ha richiesto esplicitamente un cambio lingua tramite URL (?lang=fr)
    lang_override = request.args.get('lang')
    if lang_override in supported_languages:
        session['language'] = lang_override
        return lang_override

    # 2. Controlla se la lingua è già stata memorizzata nella sessione dell'utente
    if 'language' in session:
        return session['language']

    # 3. Altrimenti, si affida alle impostazioni di default del browser dell'ospite
    return request.accept_languages.best_match(supported_languages) or 'en'


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    if not Config.SECRET_KEY or Config.SECRET_KEY == 'dev-only-change-in-production':
        import warnings

        warnings.warn('SECRET_KEY is set to an insecure default. Set a strong SECRET_KEY in .env for production.')

    if not app.debug:
        handler = RotatingFileHandler('app.log', maxBytes=1024 * 1024, backupCount=5)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(module)s: %(message)s'))
        app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)
        app.logger.info('Apt_Website starting')

    # Core Babel Config Setup Engine Parameters
    app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    app.config['LANGUAGES'] = ['en', 'it', 'de', 'fr', 'es']
    app.config['BABEL_TRANSLATION_DIRECTORIES'] = os.path.join(app.root_path, '..', 'translations')

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    mail.init_app(app)
    bcrypt.init_app(app)
    Moment(app)
    limiter.init_app(app)

    # ── Babel ──────────────────────────────────────────────────────────────────
    babel.init_app(app, locale_selector=get_locale)
    babel.init_app(app, locale_selector=get_locale)
    app.jinja_env.filters['format_date'] = format_date
    app.jinja_env.filters['format_datetime'] = format_datetime

    from app.routes import bp

    app.register_blueprint(bp)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return render_template('csrf_error.html', reason=e.description), 400

    @app.route('/health')
    def health():
        return jsonify({'status': 'ok', 'version': '1.0'})

    @app.context_processor
    def inject_apartment():
        from app.models import Apartment

        apartment = Apartment.query.first()
        return dict(apartment=apartment)

    @app.context_processor
    def inject_notifications():
        from app.models import Notification

        unread = Notification.query.filter_by(is_read=False).count()
        return dict(unread_notifications=unread)

    @app.context_processor
    def inject_locale():
        return dict(get_locale=get_locale)

    @app.context_processor
    def inject_now():
        from datetime import datetime

        return dict(now=datetime.utcnow)

    # ── Start APScheduler for periodic iCal sync ──────────────────────────────
    _start_scheduler(app)

    return app


def _start_scheduler(app: Flask):
    """
    Start a background APScheduler that calls sync_all_feeds() every N minutes.
    Only starts when NOT inside a Flask CLI command or when Werkzeug reloader
    is active (to avoid double-start in debug mode).
    """
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'false':
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        app.logger.warning('APScheduler not installed — iCal auto-sync disabled. Run: pip install apscheduler')
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
