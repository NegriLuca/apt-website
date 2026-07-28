from flask import Blueprint

bp = Blueprint('routes', __name__)

from app.routes import helpers
from app.routes import public
from app.routes import booking
from app.routes import admin
from app.routes import compliance
from app.routes import api
