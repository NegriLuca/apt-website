from flask_babel import lazy_gettext as _
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import URL, DataRequired, Email, Length, Optional


class ReservationForm(FlaskForm):
    guest_name: StringField = StringField(_('Name'), validators=[DataRequired()])
    guest_email: StringField = StringField(_('Email'), validators=[DataRequired(), Email()])
    check_in: DateField = DateField(_('Check-in'), validators=[DataRequired()])
    check_out: DateField = DateField(_('Check-out'), validators=[DataRequired()])
    num_guests: SelectField = SelectField(
        _('Guests'), choices=[(1, '1'), (2, '2'), (3, '3'), (4, '4')], coerce=int, validators=[DataRequired()]
    )
    submit: SubmitField = SubmitField(_('Proceed to payment'))


class LoginForm(FlaskForm):
    username: StringField = StringField(_('Username'), validators=[DataRequired(), Length(min=2, max=20)])
    password: PasswordField = PasswordField(_('Password'), validators=[DataRequired()])
    remember: BooleanField = BooleanField(_('Remember Me'))
    submit: SubmitField = SubmitField(_('Login'))


class ContactForm(FlaskForm):
    name: StringField = StringField(_('Your name'), validators=[DataRequired()])
    email: StringField = StringField(_('Email address'), validators=[DataRequired(), Email()])
    message: TextAreaField = TextAreaField(_('Message'), validators=[DataRequired(), Length(min=10)])
    submit: SubmitField = SubmitField(_('Send message'))


class ICalFeedForm(FlaskForm):
    source: SelectField = SelectField(
        _('Platform'),
        choices=[
            ('airbnb', 'Airbnb'),
            ('booking', 'Booking.com'),
            ('vrbo', 'VRBO'),
            ('other', 'Other'),
        ],
        validators=[DataRequired()],
    )
    url: StringField = StringField(_('iCal URL'), validators=[DataRequired(), URL()])
    active: BooleanField = BooleanField(_('Active'), default=True)
    submit: SubmitField = SubmitField(_('Save'))


class TestimonialForm(FlaskForm):
    guest_name: StringField = StringField(_('Your Name'), validators=[DataRequired(), Length(max=100)])
    guest_location: StringField = StringField(_('Location (City, Country)'), validators=[Optional(), Length(max=100)])
    rating: SelectField = SelectField(
        _('Rating'),
        choices=[(5, '5 - Excellent'), (4, '4 - Good'), (3, '3 - Average'), (2, '2 - Poor'), (1, '1 - Terrible')],
        coerce=int,
        validators=[DataRequired()],
    )
    content: TextAreaField = TextAreaField(_('Your Review'), validators=[DataRequired(), Length(min=10, max=2000)])
    stay_date: DateField = DateField(_('Stay Date (optional)'), validators=[Optional()], format='%Y-%m-%d')
    submit: SubmitField = SubmitField(_('Submit Review'))
