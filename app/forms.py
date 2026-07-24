from flask_wtf import FlaskForm
from wtforms import (
    StringField, SubmitField, SelectField, TextAreaField,
    DateField, PasswordField, BooleanField, URLField
)
from wtforms.validators import DataRequired, Email, Length, URL, Optional

# CRITICAL IMPORT: Utilizziamo lazy_get_text per gli oggetti globali come i form
from flask_babel import lazy_gettext as _

class ReservationForm(FlaskForm):
    guest_name  = StringField(_('Name'),  validators=[DataRequired()])
    guest_email = StringField(_('Email'), validators=[DataRequired(), Email()])
    check_in    = DateField(_('Check-in'),  validators=[DataRequired()])
    check_out   = DateField(_('Check-out'), validators=[DataRequired()])
    num_guests  = SelectField(
        _("Guests"),
        choices=[(1, "1"), (2, "2"), (3, "3"), (4, "4")],
        coerce=int,
        validators=[DataRequired()]
    )
    submit = SubmitField(_('Proceed to payment'))

class LoginForm(FlaskForm):
    username = StringField(_('Username'), validators=[DataRequired(), Length(min=2, max=20)])
    password = PasswordField(_('Password'), validators=[DataRequired()])
    remember = BooleanField(_('Remember Me'))
    submit   = SubmitField(_('Login'))

class ContactForm(FlaskForm):
    name    = StringField(_('Your name'),       validators=[DataRequired()])
    email   = StringField(_('Email address'),   validators=[DataRequired(), Email()])
    message = TextAreaField(_('Message'),       validators=[DataRequired(), Length(min=10)])
    submit  = SubmitField(_('Send message'))

class ICalFeedForm(FlaskForm):
    source = SelectField(
        _('Platform'),
        choices=[
            ('airbnb',  'Airbnb'),
            ('booking', 'Booking.com'),
            ('vrbo',    'VRBO'),
            ('other',   'Other'),
        ],
        validators=[DataRequired()]
    )
    url    = StringField(_('iCal URL'), validators=[DataRequired(), URL()])
    active = BooleanField(_('Active'), default=True)
    submit = SubmitField(_('Save'))


class TestimonialForm(FlaskForm):
    guest_name     = StringField(_('Your Name'), validators=[DataRequired(), Length(max=100)])
    guest_location = StringField(_('Location (City, Country)'), validators=[Optional(), Length(max=100)])
    rating         = SelectField(_('Rating'), choices=[(5, '5 - Excellent'), (4, '4 - Good'), (3, '3 - Average'), (2, '2 - Poor'), (1, '1 - Terrible')], coerce=int, validators=[DataRequired()])
    content        = TextAreaField(_('Your Review'), validators=[DataRequired(), Length(min=10, max=2000)])
    stay_date      = DateField(_('Stay Date (optional)'), validators=[Optional()], format='%Y-%m-%d')
    submit         = SubmitField(_('Submit Review'))