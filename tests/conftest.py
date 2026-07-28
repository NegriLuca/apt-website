import pytest

from app import create_app
from app import db as _db
from app.models import Apartment, User
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    SECRET_KEY = 'test-secret'
    STRIPE_SECRET_KEY = ''
    STRIPE_PUBLISHABLE_KEY = ''
    MAIL_SUPPRESS_SEND = True
    RATELIMIT_ENABLED = False


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        _db.create_all()
        _seed_data()
        yield app
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


def _seed_data():
    admin = User(username='admin', is_admin=True)
    admin.set_password('admin123')
    _db.session.add(admin)

    apt = Apartment(
        name='Test Apartment',
        price_per_night=130.00,
        image_file='default.jpg',
        cin_code='IT000000C2TXZ44TA6',
        cir_code='000000-LOC-00000',
    )
    _db.session.add(apt)
    _db.session.commit()


def login_admin(client):
    return client.post(
        '/login',
        data={
            'username': 'admin',
            'password': 'admin123',
        },
        follow_redirects=True,
    )
