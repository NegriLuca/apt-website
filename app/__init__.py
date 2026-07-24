import os
from flask import Flask, request, session, current_app
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_mail import Mail
from flask_bcrypt import Bcrypt
from flask_babel import Babel, format_date, format_datetime
from flask_moment import Moment
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
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
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
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


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

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
    moment = Moment(app)
    limiter.init_app(app)

    # Initialize Babel with the context selector function configuration
    babel.init_app(app, locale_selector=get_locale)
    app.jinja_env.filters['format_date'] = format_date
    app.jinja_env.filters['format_datetime'] = format_datetime

    from app import routes
    app.register_blueprint(routes.bp)

    @app.context_processor
    def inject_apartment():
        from app.models import Apartment
        # Recupera l'appartamento una sola volta per qualsiasi richiesta
        apartment = Apartment.query.first()
        return dict(apartment=apartment)

    @app.context_processor
    def inject_locale():
        # Rendiamo disponibile la stringa della lingua corrente a Jinja2
        return dict(get_locale=get_locale)

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