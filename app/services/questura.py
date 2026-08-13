"""
Questura (Italian Police) guest registration service.
Implements the AlloggiatiWeb SOAP protocol (GenerateToken + Send operations).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.models import ComplianceConfig, QuesturaLog, Reservation

logger = logging.getLogger(__name__)


@dataclass
class QuesturaGuest:
    """Guest data structure for Questura submission"""

    surname: str
    first_name: str
    birth_date: date
    birth_place: str
    birth_country: str
    nationality: str
    document_type: str  # passaporto, carta_identita, patente, altro
    document_number: str
    document_expiry: date
    document_country: str  # ISO 3166-1 alpha-3
    gender: str  # M/F
    check_in: date
    check_out: date
    reservation_id: int
    guest_email: str | None = None
    guest_phone: str | None = None


class QuesturaService:
    """Service for submitting guest data to Questura via the AlloggiatiWeb SOAP API."""

    USERNAME_KEY = 'questura_username'
    PASSWORD_KEY = 'questura_password'
    WS_KEY_KEY = 'questura_ws_key'
    PROTOCOL_KEY = 'questura_protocol_number'
    WSDL_URL_KEY = 'questura_wsdl_url'

    # Legacy certificate-based config (old WCF endpoint) — kept for reference.
    CERT_PATH_KEY = 'questura_cert_path'
    CERT_PASSWORD_KEY = 'questura_cert_password'

    # AlloggiatiWeb ASMX endpoints
    DEFAULT_ENDPOINT = 'https://alloggiatiweb.poliziadistato.it/service/service.asmx'
    TEST_ENDPOINT = 'https://alloggiatiwebtest.poliziadistato.it/service/service.asmx'

    SOAP_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
    SERVICE_NS = 'AlloggiatiService'

    def __init__(self, test_mode: bool = False):
        self.test_mode = test_mode
        self.session = self._create_session()
        self._config_cache = {}

    def _create_session(self) -> requests.Session:
        """Create requests session with retry strategy"""
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=['HEAD', 'GET', 'OPTIONS', 'POST'],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def _get_config(self, key: str) -> str | None:
        """Get config value with caching"""
        if key not in self._config_cache:
            self._config_cache[key] = ComplianceConfig.get(key)
        return self._config_cache[key]

    def _get_username(self) -> str | None:
        import os
        return os.environ.get('QUESTURA_USERNAME') or self._get_config(self.USERNAME_KEY)

    def _get_password(self) -> str | None:
        import os
        return os.environ.get('QUESTURA_PASSWORD') or self._get_config(self.PASSWORD_KEY)

    def _get_ws_key(self) -> str | None:
        import os
        return os.environ.get('QUESTURA_WS_KEY') or self._get_config(self.WS_KEY_KEY)

    def _get_protocol(self) -> str | None:
        import os
        return os.environ.get('QUESTURA_PROTOCOL_NUMBER') or self._get_config(self.PROTOCOL_KEY)

    def is_configured(self) -> bool:
        """Check if all required config is present"""
        required = [self._get_username(), self._get_password(), self._get_ws_key(), self._get_protocol()]
        return all(required)

    def _get_endpoint(self) -> str:
        import os
        endpoint = os.environ.get('QUESTURA_ENDPOINT') or self._get_config(self.WSDL_URL_KEY)
        if endpoint:
            return endpoint
        return self.TEST_ENDPOINT if self.test_mode else self.DEFAULT_ENDPOINT

    # ── Schedina XML building ────────────────────────────────────────────────

    def build_guest_xml(self, guest: QuesturaGuest) -> str:
        """Build the AlloggiatiWeb schedina XML for a single guest."""

        def fmt_date(d: date) -> str:
            return d.strftime('%d/%m/%Y')

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Alloggiati xmlns="http://www.poliziadistato.it/AlloggiatiWeb">
    <Struttura>
        <Protocollo>{self._escape_xml(self._get_protocol())}</Protocollo>
    </Struttura>
    <Ospite>
        <Cognome>{self._escape_xml(guest.surname)}</Cognome>
        <Nome>{self._escape_xml(guest.first_name)}</Nome>
        <DataNascita>{fmt_date(guest.birth_date)}</DataNascita>
        <LuogoNascita>{self._escape_xml(guest.birth_place)}</LuogoNascita>
        <StatoNascita>{self._escape_xml(guest.birth_country)}</StatoNascita>
        <Cittadinanza>{self._escape_xml(guest.nationality)}</Cittadinanza>
        <TipoDocumento>{self._map_doc_type(guest.document_type)}</TipoDocumento>
        <NumeroDocumento>{self._escape_xml(guest.document_number)}</NumeroDocumento>
        <ScadenzaDocumento>{fmt_date(guest.document_expiry)}</ScadenzaDocumento>
        <StatoRilascioDocumento>{self._escape_xml(guest.document_country)}</StatoRilascioDocumento>
        <Sesso>{guest.gender.upper()}</Sesso>
        <DataArrivo>{fmt_date(guest.check_in)}</DataArrivo>
        <DataPartenza>{fmt_date(guest.check_out)}</DataPartenza>
    </Ospite>
</Alloggiati>"""

    def build_multiple_guests_xml(self, guests: list[QuesturaGuest]) -> str:
        """Build a single Alloggiati XML with multiple <Ospite> blocks."""

        def fmt_date(d: date) -> str:
            return d.strftime('%d/%m/%Y')

        ospiti = []
        for guest in guests:
            ospiti.append(f"""
        <Ospite>
            <Cognome>{self._escape_xml(guest.surname)}</Cognome>
            <Nome>{self._escape_xml(guest.first_name)}</Nome>
            <DataNascita>{fmt_date(guest.birth_date)}</DataNascita>
            <LuogoNascita>{self._escape_xml(guest.birth_place)}</LuogoNascita>
            <StatoNascita>{self._escape_xml(guest.birth_country)}</StatoNascita>
            <Cittadinanza>{self._escape_xml(guest.nationality)}</Cittadinanza>
            <TipoDocumento>{self._map_doc_type(guest.document_type)}</TipoDocumento>
            <NumeroDocumento>{self._escape_xml(guest.document_number)}</NumeroDocumento>
            <ScadenzaDocumento>{fmt_date(guest.document_expiry)}</ScadenzaDocumento>
            <StatoRilascioDocumento>{self._escape_xml(guest.document_country)}</StatoRilascioDocumento>
            <Sesso>{guest.gender.upper()}</Sesso>
            <DataArrivo>{fmt_date(guest.check_in)}</DataArrivo>
            <DataPartenza>{fmt_date(guest.check_out)}</DataPartenza>
        </Ospite>""")

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Alloggiati xmlns="http://www.poliziadistato.it/AlloggiatiWeb">
    <Struttura>
        <Protocollo>{self._escape_xml(self._get_protocol())}</Protocollo>
    </Struttura>"""
        xml += ''.join(ospiti)
        xml += '\n</Alloggiati>'
        return xml

    def _escape_xml(self, text: str | None) -> str:
        """Escape XML special characters"""
        if not text:
            return ''
        return (
            text.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&apos;')
        )

    def _map_doc_type(self, doc_type: str) -> str:
        """Map internal doc type to Questura codes"""
        mapping = {
            'passport': 'P',
            'passaporto': 'P',
            'id_card': 'CI',
            'carta_identita': 'CI',
            'driving_license': 'PAT',
            'patente': 'PAT',
            'other': 'ALT',
            'altro': 'ALT',
        }
        return mapping.get(doc_type.lower(), 'ALT')

    # ── SOAP transport ───────────────────────────────────────────────────────

    def _build_soap_envelope(self, body: str) -> str:
        return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="{self.SOAP_NS}">
  <soap:Body>
    {body}
  </soap:Body>
</soap:Envelope>'''

    def _call(self, operation: str, envelope: str) -> dict[str, Any]:
        """POST a SOAP request to the AlloggiatiWeb endpoint."""
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f'"{self.SERVICE_NS}/{operation}"',
        }
        try:
            response = self.session.post(
                self._get_endpoint(),
                data=envelope.encode('utf-8'),
                headers=headers,
                timeout=60,
            )
        except requests.exceptions.Timeout:
            logger.error('AlloggiatiWeb %s timed out', operation)
            return {'success': False, 'error': 'Request timed out'}
        except requests.exceptions.ConnectionError as e:
            logger.error('AlloggiatiWeb connection error: %s', e)
            return {'success': False, 'error': f'Connection error: {e}'}
        except Exception as e:
            logger.exception('AlloggiatiWeb %s failed', operation)
            return {'success': False, 'error': str(e)}

        if response.status_code != 200:
            logger.error(
                'AlloggiatiWeb %s HTTP %s: %s', operation, response.status_code, response.text[:500]
            )
            return {
                'success': False,
                'error': f'HTTP {response.status_code} — {response.text[:300]}',
                'response_xml': response.text,
            }

        return {'success': True, 'response_xml': response.text}

    def _generate_token(self) -> dict[str, Any]:
        """Step 1: exchange username/password/WsKey for a short-lived token."""
        body = (
            f'<GenerateToken xmlns="{self.SERVICE_NS}">'
            f'<Utente>{self._escape_xml(self._get_username())}</Utente>'
            f'<Password>{self._escape_xml(self._get_password())}</Password>'
            f'<WsKey>{self._escape_xml(self._get_ws_key())}</WsKey>'
            f'</GenerateToken>'
        )
        result = self._call('GenerateToken', self._build_soap_envelope(body))
        if not result.get('success'):
            return result

        response_xml = result.get('response_xml', '')
        try:
            root = ET.fromstring(response_xml)
        except ET.ParseError:
            return {'success': False, 'error': 'Invalid XML response', 'response_xml': response_xml}

        err = self._find_detail_error(root)
        if err:
            return {'success': False, 'error': err, 'response_xml': response_xml}

        token_el = root.find('.//{*}token')
        if token_el is not None and token_el.text and token_el.text.strip():
            return {'success': True, 'token': token_el.text.strip(), 'response_xml': response_xml}

        return {'success': False, 'error': 'No token returned', 'response_xml': response_xml}

    def _send_schedine(self, token: str, schedine: list[str]) -> dict[str, Any]:
        """Step 2: send the guest schedine with the previously generated token."""
        strings = ''.join(f'<string>{self._escape_xml(s)}</string>' for s in schedine)
        body = (
            f'<Send xmlns="{self.SERVICE_NS}">'
            f'<Utente>{self._escape_xml(self._get_username())}</Utente>'
            f'<token>{self._escape_xml(token)}</token>'
            f'<ElencoSchedine>{strings}</ElencoSchedine>'
            f'</Send>'
        )
        result = self._call('Send', self._build_soap_envelope(body))
        if not result.get('success'):
            return result
        return self._parse_send_response(result.get('response_xml', ''))

    def _find_detail_error(self, root: ET.Element) -> str:
        """Extract the first non-empty <ErroreDettaglio> text."""
        for el in root.iter('{*}ErroreDettaglio'):
            if el.text and el.text.strip():
                return el.text.strip()
        return ''

    def _parse_send_response(self, response_xml: str) -> dict[str, Any]:
        """Parse the Send response: SchedineValide + per-schedina error details."""
        try:
            root = ET.fromstring(response_xml)
        except ET.ParseError:
            return {'success': False, 'error': 'Invalid XML response', 'response_xml': response_xml}

        validated_el = root.find('.//{*}SchedineValide')
        validated = 0
        if validated_el is not None and validated_el.text and validated_el.text.strip().isdigit():
            validated = int(validated_el.text.strip())

        errors = []
        for esito in root.findall('.//{*}EsitoOperazioneServizio'):
            el = esito.find('{*}ErroreDettaglio')
            if el is not None and el.text and el.text.strip():
                errors.append(el.text.strip())

        top_error = self._find_detail_error(root)
        if top_error:
            return {
                'success': False,
                'error': top_error,
                'validated': validated,
                'errors': errors,
                'response_xml': response_xml,
            }
        if errors:
            return {
                'success': False,
                'error': '; '.join(errors),
                'validated': validated,
                'errors': errors,
                'response_xml': response_xml,
            }
        if validated == 0:
            return {
                'success': False,
                'error': 'No schedine validated',
                'validated': 0,
                'response_xml': response_xml,
            }

        return {'success': True, 'validated': validated, 'errors': errors, 'response_xml': response_xml}

    def submit_guests(self, guests: list[QuesturaGuest]) -> dict[str, Any]:
        """Submit guests to AlloggiatiWeb. Returns dict with success, message, details."""
        if not self.is_configured():
            return {
                'success': False,
                'error': 'Questura service not configured. Set username, password, WsKey and protocol.',
            }

        if not guests:
            return {'success': False, 'error': 'No guests to submit'}

        schedine = [self.build_guest_xml(g) for g in guests]
        request_xml = '\n'.join(schedine)

        log = QuesturaLog(
            reservation_id=guests[0].reservation_id, action='submit', request_xml=request_xml, status='pending'
        )
        db.session.add(log)
        db.session.commit()

        try:
            if self.test_mode:
                logger.info('[TEST MODE] Would submit %d guests to AlloggiatiWeb', len(guests))
                log.status = 'success'
                log.response_xml = '<TestResponse>OK</TestResponse>'
                db.session.commit()
                for g in guests:
                    self._update_reservation_questura(g.reservation_id, 'accepted', None)
                return {
                    'success': True,
                    'message': 'Test submission logged (no actual API call)',
                    'test_mode': True,
                }

            token_result = self._generate_token()
            if not token_result.get('success'):
                error = token_result.get('error', 'Token generation failed')
                log.status = 'error'
                log.error_message = error
                db.session.commit()
                for g in guests:
                    self._update_reservation_questura(g.reservation_id, 'rejected', error)
                return {'success': False, 'error': error, 'response_xml': token_result.get('response_xml')}

            result = self._send_schedine(token_result['token'], schedine)
            success = bool(result.get('success'))

            log.response_xml = result.get('response_xml', '')
            log.status = 'success' if success else 'error'
            log.error_message = result.get('error', '') if not success else ''
            db.session.commit()

            for g in guests:
                self._update_reservation_questura(
                    g.reservation_id,
                    'accepted' if success else 'rejected',
                    result.get('error') if not success else None,
                )

            return {
                'success': success,
                'message': 'Submitted successfully' if success else 'Submission failed',
                'error': result.get('error'),
                'validated': result.get('validated'),
                'response': result.get('response_xml'),
            }

        except Exception as e:
            logger.exception('Questura submission failed')
            log.status = 'error'
            log.error_message = str(e)
            db.session.commit()
            for g in guests:
                self._update_reservation_questura(g.reservation_id, 'rejected', str(e))
            return {'success': False, 'error': str(e)}

    def _update_reservation_questura(self, reservation_id: int, status: str, error: str | None = None):
        """Update reservation questura tracking fields"""
        res = Reservation.query.get(reservation_id)
        if res:
            res.questura_status = status
            res.questura_submitted_at = datetime.utcnow()
            res.questura_error = error
            db.session.commit()

    def submit_reservation(self, reservation: Reservation) -> dict[str, Any]:
        """Submit all guests (main + companions) for a reservation"""
        from datetime import date as _date

        guests = []

        def _build(surname, first_name, birth_date, birth_place, nationality,
                   document_type, document_number, document_expiry, document_country, gender):
            if not (surname and first_name and birth_date):
                return None
            return QuesturaGuest(
                surname=surname,
                first_name=first_name,
                birth_date=birth_date if isinstance(birth_date, _date) else _date.fromisoformat(birth_date),
                birth_place=birth_place or '',
                birth_country=nationality or '',
                nationality=nationality or '',
                document_type=document_type or 'altro',
                document_number=document_number or '',
                document_expiry=document_expiry if isinstance(document_expiry, _date) else (_date.fromisoformat(document_expiry) if document_expiry else _date.today()),
                document_country=document_country or 'ITA',
                gender=gender or 'M',
                check_in=reservation.check_in,
                check_out=reservation.check_out,
                reservation_id=reservation.id,
                guest_email=reservation.guest_email,
            )

        main = _build(
            reservation.guest_surname,
            reservation.guest_first_name,
            reservation.guest_birth_date,
            reservation.guest_birth_place,
            reservation.guest_nationality,
            reservation.guest_document_type,
            reservation.guest_document_number,
            reservation.guest_document_expiry,
            reservation.guest_document_country,
            reservation.guest_gender,
        )
        if main:
            guests.append(main)

        for comp in reservation.companions or []:
            guest = _build(
                comp.get('surname'),
                comp.get('first_name'),
                comp.get('birth_date'),
                comp.get('birth_place'),
                comp.get('nationality'),
                comp.get('document_type'),
                comp.get('document_number'),
                comp.get('document_expiry'),
                comp.get('document_country'),
                comp.get('gender'),
            )
            if guest:
                guests.append(guest)

        if not guests:
            return {
                'success': False,
                'error': 'Guest data collection required. Use guest check-in form.',
                'requires_guest_data': True,
            }

        return self.submit_guests(guests)


def get_questura_service(test_mode: bool = False) -> QuesturaService:
    """Factory function to get Questura service instance"""
    return QuesturaService(test_mode=test_mode)


# Convenience function for admin use
def submit_pending_questura(reservation_ids: list[int] = None) -> dict[str, Any]:
    """Submit all reservations with pending questura status"""
    query = Reservation.query.filter(
        Reservation.questura_status.in_([None, 'pending', 'rejected']),
        Reservation.status == 'confirmed',
        Reservation.check_in <= date.today(),  # Only for current/past stays
    )
    if reservation_ids:
        query = query.filter(Reservation.id.in_(reservation_ids))

    reservations = query.all()
    if not reservations:
        return {'success': True, 'message': 'No pending submissions', 'count': 0}

    # This would need guest data collected at check-in
    return {
        'success': False,
        'message': f'Found {len(reservations)} reservations needing Questura submission, but guest data not collected',
        'reservations': [r.id for r in reservations],
    }
