from datetime import datetime
from app import db, login_manager, bcrypt
from flask_login import UserMixin

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(db.Model, UserMixin):
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(20), unique=True, nullable=False)
    password   = db.Column(db.String(128), nullable=False)   # stores bcrypt hash
    is_admin   = db.Column(db.Boolean, default=False)

    def set_password(self, raw_password: str):
        """Hash and store a new password."""
        self.password = bcrypt.generate_password_hash(raw_password).decode('utf-8')

    def check_password(self, raw_password: str) -> bool:
        """Return True if raw_password matches the stored hash."""
        return bcrypt.check_password_hash(self.password, raw_password)

class Apartment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price_per_night = db.Column(db.Float, nullable=False)
    image_file = db.Column(db.String(20), nullable=False, default='default.jpg')


class Reservation(db.Model):
    @staticmethod
    def overlaps(check_in, check_out):
        return Reservation.query.filter(
            Reservation.status == "confirmed",
            Reservation.check_in < check_out,
            Reservation.check_out > check_in,
        )
    
    __tablename__ = "reservation"

    id = db.Column(db.Integer, primary_key=True)
    guest_name = db.Column(db.String(100), nullable=False)
    guest_email = db.Column(db.String(120), nullable=False)
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    num_guests = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20),nullable=False, default='pending')
    created_at = db.Column(db.DateTime,default=datetime.utcnow)
    cancel_token = db.Column(db.String(128), unique=True, index=True)
    source = db.Column(db.String(20), default="direct")
    external_uid = db.Column(db.String(128), unique=True, index=True)
    __table_args__ = (
        db.CheckConstraint("check_out > check_in", name="ck_dates_valid"),
        db.CheckConstraint("num_guests >= 1 AND num_guests <= 4", name="ck_guests_range"),
        db.UniqueConstraint("external_uid", name="uq_reservation_external_uid"),
        db.UniqueConstraint("cancel_token", name="uq_reservation_cancel_token"),
        db.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled')",
            name="ck_reservation_status"
        ),
    )

class ICalFeed(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20))  # airbnb / vrbo / booking
    url = db.Column(db.Text, nullable=False)
    last_synced_at = db.Column(db.DateTime)
    active = db.Column(db.Boolean, default=True)