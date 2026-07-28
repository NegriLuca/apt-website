import io
import json

import requests
from flask import Response, current_app, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _

from app import db
from app.forms import ContactForm, TestimonialForm
from app.models import Reservation, Testimonial
from app.routes import bp
from app.routes.helpers import get_apartment, get_testimonials


@bp.route('/')
def home() -> Response | str:
    apartment = get_apartment()
    testimonials = get_testimonials()
    form = TestimonialForm()
    return render_template('apartment.html', apartment=apartment, testimonials=testimonials, form=form)


@bp.route('/faq')
def faq() -> Response | str:
    return render_template('faq.html')


@bp.route('/terms')
def terms() -> Response | str:
    return render_template('policies/terms.html')


@bp.route('/cancellation-policy')
def cancellation_policy() -> Response | str:
    return render_template('policies/cancellation.html')


@bp.route('/refund-policy')
def refund_policy() -> Response | str:
    return render_template('policies/refund.html')


@bp.route('/house-rules')
def house_rules() -> Response | str:
    return render_template('policies/house_rules.html')


@bp.route('/privacy')
def privacy() -> Response | str:
    return render_template('policies/privacy.html')


@bp.route('/food_recommendations')
def food_recommendations() -> Response | str:
    return render_template('food_recommendations.html')


@bp.route('/attractions')
def attractions() -> Response | str:
    return render_template('attractions.html')


@bp.route('/ical/apartment.ics')
def export_ical() -> Response | str:
    from app.routes.helpers import get_apartment

    get_apartment()
    reservations = Reservation.query.filter(Reservation.status != 'cancelled').order_by(Reservation.check_in).all()

    output = io.StringIO()
    output.write('BEGIN:VCALENDAR\r\n')
    output.write('VERSION:2.0\r\n')
    output.write('PRODID:-//Lotto235//Booking Calendar//EN\r\n')
    output.write('CALSCALE:GREGORIAN\r\n')
    output.write('METHOD:PUBLISH\r\n')
    output.write('X-WR-CALNAME:Booking Calendar\r\n')
    output.write('X-WR-TIMEZONE:Europe/Rome\r\n')

    for res in reservations:
        uid = f'booking-{res.id}@lotto235'
        output.write('BEGIN:VEVENT\r\n')
        output.write(f'UID:{uid}\r\n')
        output.write(f'DTSTART;VALUE=DATE:{res.check_in.strftime("%Y%m%d")}\r\n')
        output.write(f'DTEND;VALUE=DATE:{res.check_out.strftime("%Y%m%d")}\r\n')
        summary = f'Reserved: {res.guest_name}'
        output.write(f'SUMMARY:{summary}\r\n')
        output.write(f'DESCRIPTION:Status: {res.status}\\nGuests: {res.num_guests}\\nSource: {res.source}\r\n')
        output.write('END:VEVENT\r\n')

    output.write('END:VCALENDAR\r\n')
    ical_data = output.getvalue()
    output.close()

    return ical_data, 200, {'Content-Type': 'text/calendar; charset=utf-8'}


@bp.route('/set-language/<lang>')
def set_language(lang: str) -> Response | str:
    if lang in ['en', 'it', 'de', 'fr', 'es']:
        session['language'] = lang
    return redirect(request.referrer or url_for('routes.home'))


@bp.route('/contact', methods=['GET', 'POST'])
def contact() -> Response | str:
    form = ContactForm()
    if form.validate_on_submit():
        name = form.name.data
        email = form.email.data
        message_text = form.message.data

        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = 'lotto235roma@gmail.com'
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'

        payload = {
            'sender': {'name': name, 'email': sender_email},
            'to': [{'email': admin_recipient}],
            'replyTo': {'email': email, 'name': name},
            'subject': f'\U0001f4ec Contact Form: {name}',
            'htmlContent': f'<p><strong>Name:</strong> {name}</p><p><strong>Email:</strong> {email}</p><p><strong>Message:</strong><br>{message_text}</p>',
        }

        try:
            url = 'https://api.brevo.com/v3/smtp/email'
            headers = {'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key}
            response = requests.post(url, headers=headers, data=json.dumps(payload))
            if response.status_code in [200, 201, 202]:
                flash(_('Thank you! Your message has been sent.'), 'success')
            else:
                flash(_('Failed to send message. Please try again later.'), 'danger')
        except Exception:
            flash(_('Network error. Please try again later.'), 'danger')

        return redirect(url_for('routes.contact'))

    return render_template('contact.html', form=form)


@bp.route('/testimonial/submit', methods=['GET', 'POST'])
def submit_testimonial() -> Response | str:
    form = TestimonialForm()
    if form.validate_on_submit():
        testimonial = Testimonial(
            guest_name=form.guest_name.data,
            guest_location=form.guest_location.data,
            rating=form.rating.data,
            content=form.content.data,
            stay_date=form.stay_date.data,
            source='direct',
            is_published=False,
        )
        db.session.add(testimonial)
        db.session.commit()
        flash(_('Thank you for your review! It will be published after moderation.'), 'success')
        return redirect(url_for('routes.home'))
    if form.errors:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    return render_template('apartment.html', apartment=get_apartment(), testimonials=get_testimonials(), form=form)


@bp.route('/sitemap.xml', methods=['GET'])
def sitemap() -> Response | str:
    base_url = current_app.config.get('BASE_URL', 'https://www.lotto235garbatella.it').rstrip('/')

    static_pages = [
        ('/', 'weekly', 1.0),
        ('/reserve', 'monthly', 0.9),
        ('/faq', 'monthly', 0.7),
        ('/terms', 'yearly', 0.5),
        ('/cancellation-policy', 'yearly', 0.5),
        ('/refund-policy', 'yearly', 0.5),
        ('/house-rules', 'yearly', 0.5),
        ('/privacy', 'yearly', 0.5),
        ('/food_recommendations', 'monthly', 0.6),
        ('/attractions', 'monthly', 0.6),
        ('/contact', 'monthly', 0.5),
    ]

    urls = []
    for path, changefreq, priority in static_pages:
        urls.append(
            f'        <url><loc>{base_url}{path}</loc><changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>'
        )

    languages = ['en', 'it', 'de', 'fr', 'es']
    main_pages = ['/', '/reserve', '/faq', '/contact']
    for lang in languages:
        if lang != 'en':
            for path in main_pages:
                lang_path = f'/{lang}{path}' if path != '/' else f'/{lang}/'
                urls.append(
                    f'        <url><loc>{base_url}{lang_path}</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>'
                )

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
    </urlset>"""

    return Response(xml_content, mimetype='text/xml')
