from flask_wtf import FlaskForm
from wtforms import (
    StringField, SubmitField, SelectField, TextAreaField,
    DateField, PasswordField, BooleanField, URLField
)
from wtforms.validators import DataRequired, Email, Length, URL, Optional

class ReservationForm(FlaskForm):
    guest_name  = StringField('Name',  validators=[DataRequired()])
    guest_email = StringField('Email', validators=[DataRequired(), Email()])
    check_in    = DateField('Check-in',  validators=[DataRequired()])
    check_out   = DateField('Check-out', validators=[DataRequired()])
    num_guests  = SelectField(
        "Guests",
        choices=[(1, "1"), (2, "2"), (3, "3"), (4, "4")],
        coerce=int,
        validators=[DataRequired()]
    )
    submit = SubmitField('Proceed to payment')

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit   = SubmitField('Login')

class ContactForm(FlaskForm):
    name    = StringField('Your name',       validators=[DataRequired()])
    email   = StringField('Email address',   validators=[DataRequired(), Email()])
    message = TextAreaField('Message',       validators=[DataRequired(), Length(min=10)])
    submit  = SubmitField('Send message')


class ICalFeedForm(FlaskForm):
    source = SelectField(
        'Platform',
        choices=[
            ('airbnb',  'Airbnb'),
            ('booking', 'Booking.com'),
            ('vrbo',    'VRBO'),
            ('other',   'Other'),
        ],
        validators=[DataRequired()]
    )
    url    = StringField('iCal URL', validators=[DataRequired(), URL()])
    active = BooleanField('Active', default=True)
    submit = SubmitField('Save')