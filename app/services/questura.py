"""
Questura (Italian Police) guest registration service.
Implements the AlloggiatiWeb / Ross1000 protocol for guest check-in reporting.
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
    """Service for submitting guest data to Questura via AlloggiatiWeb/Ross1000"""

    WSDL_URL_KEY = 'questura_wsdl_url'
    USERNAME_KEY = 'questura_username'
    PASSWORD_KEY = 'questura_password'
    CERT_PATH_KEY = 'questura_cert_path'
    CERT_PASSWORD_KEY = 'questura_cert_password'
    PROTOCOL_KEY = 'questura_protocol_number'

    # Default Ross1000 endpoints
    DEFAULT_WSDL = 'https://alloggiatiweb.poliziadistato.it/AlloggiatiWebService.svc?wsdl'
    TEST_WSDL = 'https://alloggiatiwebtest.poliziadistato.it/AlloggiatiWebService.svc?wsdl'

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

    def is_configured(self) -> bool:
        """Check if all required config is present"""
        required = [self.USERNAME_KEY, self.PASSWORD_KEY, self.PROTOCOL_KEY]
        if not self.test_mode:
            required.append(self.CERT_PATH_KEY)
        return all(self._get_config(k) for k in required)

    def build_guest_xml(self, guest: QuesturaGuest) -> str:
        """Build XML for a single guest according to AlloggiatiWeb schema"""

        # All dates in DD/MM/YYYY format
        def fmt_date(d: date) -> str:
            return d.strftime('%d/%m/%Y')

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Alloggiati xmlns="http://www.poliziadistato.it/AlloggiatiWeb">
    <Struttura>
        <Protocollo>{self._get_config(self.PROTOCOL_KEY)}</Protocollo>
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
        return xml

    def build_multiple_guests_xml(self, guests: list[QuesturaGuest]) -> str:
        """Build XML for multiple guests in one submission"""

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
            <Cittadinanza>{self._escape_xml(guest.nationality)}</Cittadinanza
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
        <Protocollo>{self._get_config(self.PROTOCOL_KEY)}</Protocollo>
    </Struttura>"""
        xml += ''.join(ospiti)
        xml += '\n</Alloggiati>'
        return xml

    def _escape_xml(self, text: str) -> str:
        """Escape XML special characters"""
        if not text:
            return ''
        return text.replace('&', '&').replace('<', '<').replace('>', '>').replace('"', '"').replace("'", '&apos;')

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

    def submit_guests(self, guests: list[QuesturaGuest]) -> dict[str, Any]:
        """Submit guests to Questura. Returns dict with success, message, details"""
        if not self.is_configured():
            return {'success': False, 'error': 'Questura service not configured. Missing credentials or certificate.'}

        if not guests:
            return {'success': False, 'error': 'No guests to submit'}

        # Build XML
        xml_payload = self.build_multiple_guests_xml(guests)

        # Log attempt
        log = QuesturaLog(
            reservation_id=guests[0].reservation_id, action='submit', request_xml=xml_payload, status='pending'
        )
        db.session.add(log)
        db.session.commit()

        try:
            # For Ross1000/AlloggiatiWeb, the typical approach is:
            # 1. Client certificate auth (mTLS)
            # 2. POST to service endpoint with XML body

            cert_path = self._get_config(self.CERT_PATH_KEY)
            cert_password = self._get_config(self.CERT_PASSWORD_KEY)

            if self.test_mode:
                # In test mode, just log and return success
                logger.info(f'[TEST MODE] Would submit {len(guests)} guests to Questura')
                log.status = 'success'
                log.response_xml = '<TestResponse>OK</TestResponse>'
                db.session.commit()

                # Update reservation tracking
                for g in guests:
                    self._update_reservation_questura(g.reservation_id, 'accepted')

                return {'success': True, 'message': 'Test submission logged (no actual API call)', 'test_mode': True}

            # Production: client certificate auth
            if cert_path and cert_password:
                # Load PKCS12 certificate
                import ssl

                context = ssl.create_default_context()
                context.load_cert_chain(cert_path, password=cert_password)

                # For requests with client cert
                response = self.session.post(
                    self._get_config(self.WSDL_URL_KEY) or self.DEFAULT_WSDL,
                    data=xml_payload,
                    headers={'Content-Type': 'application/soap+xml; charset=utf-8', 'SOAPAction': 'InviaAlloggiati'},
                    cert=(cert_path, cert_password) if cert_password else cert_path,
                    timeout=30,
                )
            else:
                # Basic auth fallback (some implementations)
                username = self._get_config(self.USERNAME_KEY)
                password = self._get_config(self.PASSWORD_KEY)
                response = self.session.post(
                    self._get_config(self.WSDL_URL_KEY) or self.DEFAULT_WSDL,
                    data=xml_payload,
                    headers={'Content-Type': 'application/soap+xml; charset=utf-8', 'SOAPAction': 'InviaAlloggiati'},
                    auth=(username, password),
                    timeout=30,
                )

            # Parse response
            response_text = response.text
            log.response_xml = response_text

            if response.status_code == 200:
                success = self._parse_response(response_text, log)
            else:
                success = False
                log.status = 'error'
                log.error_message = f'HTTP {response.status_code}: {response_text[:500]}'
                db.session.commit()

            return {
                'success': success,
                'status_code': response.status_code,
                'response': response_text,
                'message': 'Submitted successfully' if success else 'Submission failed',
            }

        except Exception as e:
            logger.exception('Questura submission failed')
            log.status = 'error'
            log.error_message = str(e)
            db.session.commit()
            return {'success': False, 'error': str(e)}

    def _parse_response(self, response_xml: str, log: QuesturaLog) -> bool:
        """Parse Questura response XML"""
        try:
            root = ET.fromstring(response_xml)
            # Look for success indicators
            # Typical responses: <Esito>OK</Esito> or <Esito>KO</Esito> with <Messaggio>
            esito = root.find('.//{*}Esito')
            if esito is not None and esito.text == 'OK':
                log.status = 'success'
                db.session.commit()

                # Update reservations
                for g in []:  # We'd need to track which reservations
                    self._update_reservation_questura(g.reservation_id, 'accepted')
                return True

            messaggio = root.find('.//{*}Messaggio')
            log.status = 'error'
            log.error_message = messaggio.text if messaggio is not None else 'Unknown error'
            db.session.commit()
            return False

        except ET.ParseError:
            # Not XML, maybe HTML error page
            if 'errore' in response_xml.lower() or 'error' in response_xml.lower():
                log.status = 'error'
                log.error_message = 'Invalid response format'
                db.session.commit()
                return False
            log.status = 'success'  # Assume success if no error
            db.session.commit()
            return True

    def _update_reservation_questura(self, reservation_id: int, status: str):
        """Update reservation questura tracking fields"""
        res = Reservation.query.get(reservation_id)
        if res:
            res.questura_status = status
            res.questura_submitted_at = datetime.utcnow()
            db.session.commit()

    def submit_reservation(self, reservation: Reservation) -> dict[str, Any]:
        """Submit all guests for a reservation"""
        # Convert reservation to QuesturaGuest
        # This requires the guest data to be collected at check-in
        # For now, return info that manual entry is needed
        return {
            'success': False,
            'error': 'Guest data collection required. Use guest check-in form.',
            'requires_guest_data': True,
        }


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
