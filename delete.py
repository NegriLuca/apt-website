from app import create_app, db
from app.models import Reservation

app = create_app()

with app.app_context():
    bad = Reservation.query.filter(
        Reservation.check_out <= Reservation.check_in
    ).all()

    for r in bad:
        print("Deleting:", r.id)
        db.session.delete(r)

    db.session.commit()

