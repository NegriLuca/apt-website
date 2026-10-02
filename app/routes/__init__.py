from flask import Blueprint

bp = Blueprint('routes', __name__)

from app.routes import admin, api, booking, compliance, finance, helpers, public
