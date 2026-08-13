import logging
import os
import sys
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

    # ── Logging ─────────────────────────────────────────────────────────────────
    # All loggers write to stdout so Railway / Gunicorn always sees errors
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(module)s: %(message)s',
        stream=sys.stdout,
        force=True,
    )
    for name in ('flask.app', 'flask.wtf', 'werkzeug', 'app', __name__):
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        log.propagate = True

    if not app.debug:
        try:
            fh = RotatingFileHandler('app.log', maxBytes=1024 * 1024, backupCount=5)
            fh.setLevel(logging.INFO)
            fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(module)s: %(message)s'))
            logging.getLogger().addHandler(fh)
        except Exception:
            pass  # non-fatal — file logging may fail on Railway ephemeral FS

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
    app.jinja_env.filters['format_date'] = format_date
    app.jinja_env.filters['format_datetime'] = format_datetime

    # ── Auto-compile translations on startup ───────────────────────────────────
    _compile_translations(app)

    # ── Run pending DB migrations on startup ───────────────────────────────────
    _run_migrations(app)

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


def _compile_translations(app: Flask):
    try:
        import subprocess
        translations_dir = os.path.join(app.root_path, '..', 'translations')
        if os.path.isdir(translations_dir):
            result = subprocess.run(
                ['pybabel', 'compile', '-d', translations_dir],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                app.logger.info('Translations compiled')
            else:
                app.logger.warning('Translation compile: %s', result.stderr[:200])
    except Exception as e:
        app.logger.warning('Translation compile skipped: %s', e)


def _run_migrations(app: Flask):
    try:
        with app.app_context():
            from flask_migrate import upgrade
            upgrade(directory=os.path.join(app.root_path, '..', 'migrations'), revision='head')
            app.logger.info('DB migrations up to date')
    except Exception as e:
        app.logger.warning('DB migration skipped: %s', e)


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

    def _balance_invoice_job():
        with app.app_context():
            from app.routes.helpers import send_balance_invoice_reminders

            result = send_balance_invoice_reminders()
            if result.get('failed'):
                app.logger.warning('Balance invoice reminders: sent=%s failed=%s', result.get('sent'), result.get('failed'))
            elif result.get('sent'):
                app.logger.info('Balance invoice reminders sent: %s', result.get('sent'))

    def _cleanup_external_job():
        with app.app_context():
            from app.services.ical_sync import cleanup_past_external_reservations

            deleted = cleanup_past_external_reservations()
            if deleted:
                app.logger.info('Cleaned up %d past external reservation(s)', deleted)

    def _revoke_keypad_job():
        with app.app_context():
            from app.routes.helpers import get_apartment
            from app.services.smart_lock import revoke_expired_keypad_codes

            revoked = revoke_expired_keypad_codes(get_apartment())
            if revoked:
                app.logger.info('Revoked %d expired Nuki keypad code(s)', revoked)

    def _questura_daily_job():
        with app.app_context():
            from app.tasks.compliance import run_daily_questura

            try:
                result = run_daily_questura()
            except Exception:
                app.logger.exception('Questura daily job crashed')
                return
            if not result:
                return
            if result.get('submitted'):
                app.logger.info(
                    'Questura daily: submitted=%s failed=%s not_ready=%s',
                    result.get('submitted'),
                    result.get('failed'),
                    result.get('not_ready'),
                )
            if result.get('errors'):
                app.logger.warning('Questura daily errors: %s', result.get('errors'))

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _sync_job,
        trigger='interval',
        minutes=interval,
        id='ical_sync',
        replace_existing=True,
    )
    scheduler.add_job(
        _balance_invoice_job,
        trigger='cron',
        hour=9,
        minute=0,
        id='balance_invoice_reminder',
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        _cleanup_external_job,
        trigger='cron',
        hour=4,
        minute=30,
        id='cleanup_external_reservations',
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        _revoke_keypad_job,
        trigger='cron',
        hour=12,
        minute=0,
        id='revoke_expired_keypad_codes',
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        _questura_daily_job,
        trigger='cron',
        hour=8,
        minute=0,
        id='questura_daily_submission',
        replace_existing=True,
        coalesce=True,
    )
    scheduler.start()
    app.logger.info('iCal scheduler started (every %d min)', interval)
