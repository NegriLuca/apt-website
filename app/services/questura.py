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
        self._document_codes = None
        self._luoghi = None

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

    # ── Schedina record building ─────────────────────────────────────────────
    #
    # The AlloggiatiWeb `Send` operation expects fixed-width text records
    # ("tracciato record", D.M. 07/01/2013). Normal struttura accounts use
    # tabella 1: 168 characters per alloggiato. (Accounts with "Lista
    # Appartamenti" use tabella 2 = 174 chars, adding IdAppartamento.)
    #
    # Layout (0-indexed):
    #   0:2   tipo alloggiato (16=ospite singolo, 17=capo famiglia,
    #                         19=familiare, 18/20=membro gruppo)
    #   2:12  data arrivo (gg/mm/aaaa)
    #   12:14 giorni permanenza (max 30)
    #   14:64 cognome (left-padded with spaces)
    #   64:94 nome
    #   94:95 sesso (1=M, 2=F)
    #   95:105 data nascita (gg/mm/aaaa)
    #   105:114 comune nascita (codice 9 cifre, blank if born abroad)
    #   114:116 provincia nascita (sigla 2 lettere, blank if born abroad)
    #   116:125 stato nascita (codice 9 cifre, obbligatorio)
    #   125:134 cittadinanza (codice 9 cifre, obbligatorio)
    #   134:139 tipo documento (codice 5, blank for familiari/membri)
    #   139:159 numero documento (blank for familiari/membri)
    #   159:168 luogo/stato rilascio documento (blank for familiari/membri)
    ITALY_STATE_CODE = '100000100'

    # Confirmed by the official manual (MANUALEALBERGHI.pdf §12):
    #   IDENT = Carta d'Identità. The other codes are resolved at runtime
    #   from the Tipi_Documento table downloaded via the SOAP `Tabella` op.
    _DOC_TYPE_KEYWORDS = {
        'id_card': ['carta d\'identit', 'carta di identit', 'carta identit', 'identit', 'id card', 'carta identita'],
        'passport': ['passaporto', 'passaport', 'passport'],
        'driving_license': ['patente', 'patente di guida', 'driving license', 'driving licence', 'licenza di guida'],
    }

    def build_schedine(self, guests: list[QuesturaGuest]) -> list[str]:
        """Build the 168-char text records (tabella 1) for a group of guests.

        A single guest becomes tipo 16 (ospite singolo). Multiple guests become
        a capo famiglia (17) followed by familiari (19), whose document fields
        are blank per the tracciato record rules.
        """
        if len(guests) == 1:
            return [self._build_record(guests[0], '16')]
        records = [self._build_record(guests[0], '17')]
        records.extend(self._build_record(g, '19', include_document=False) for g in guests[1:])
        return records

    def _build_record(self, guest: QuesturaGuest, tipo: str, include_document: bool = True) -> str:
        def pad(value: str | None, width: int, align: str = 'left') -> str:
            v = (value or '')[:width]
            return v.ljust(width) if align == 'left' else v.rjust(width)

        def blank(width: int) -> str:
            return ' ' * width

        def fmt_date(d) -> str:
            if not d:
                return ''
            if isinstance(d, str):
                try:
                    d = datetime.strptime(d, '%Y-%m-%d').date()
                except ValueError:
                    return d[:10]
            return d.strftime('%d/%m/%Y')

        # Days of stay, clamped to the 30-day maximum.
        days = 0
        if guest.check_out and guest.check_in:
            days = min(30, max(0, (guest.check_out - guest.check_in).days))

        # Sesso: 1 (M) or 2 (F).
        gender = '2' if guest.gender and str(guest.gender).upper().startswith('F') else '1'

        # Italy = known 9-char code; other countries cannot be resolved from
        # the web service (no stati table is exposed) and are left blank so
        # the API surfaces the validation error in the QuesturaLog.
        birth_country = (guest.birth_country or guest.nationality or '').upper()
        nationality = (guest.nationality or '').upper()
        stato_nascita = self.ITALY_STATE_CODE if birth_country == 'ITA' else ''
        cittadinanza = self.ITALY_STATE_CODE if nationality == 'ITA' else ''

        # Comune/provincia of birth — resolved against the Luoghi table.
        comune_code, provincia = ('', '')
        if birth_country == 'ITA' and guest.birth_place:
            comune_code, provincia = self._find_comune(guest.birth_place)

        # Document block: only for ospite singolo/capo famiglia/capo gruppo.
        doc_type = doc_number = doc_place = ''
        if include_document and tipo in ('16', '17', '18'):
            doc_type = self._map_doc_type(guest.document_type) or ''
            doc_number = guest.document_number or ''
            if doc_type:
                doc_country = (guest.document_country or '').upper()
                if doc_country == 'ITA':
                    doc_place = comune_code  # issued in Italy -> comune code
                elif doc_country and doc_country != 'ITA':
                    doc_place = ''  # foreign state code not resolvable
                elif birth_country == 'ITA':
                    doc_place = comune_code

        fields = [
            pad(tipo, 2),
            pad(fmt_date(guest.check_in), 10),
            pad(str(days), 2, align='right'),
            pad(guest.surname, 50),
            pad(guest.first_name, 30),
            pad(gender, 1),
            pad(fmt_date(guest.birth_date), 10),
            pad(comune_code, 9),
            pad(provincia, 2),
            pad(stato_nascita, 9),
            pad(cittadinanza, 9),
            pad(doc_type, 5),
            pad(doc_number, 20),
            pad(doc_place, 9),
        ]
        return ''.join(fields)

    # ── Reference tables (SOAP `Tabella` op) ─────────────────────────────────

    def _load_table(self, tipo: str, parser) -> list:
        """Download a reference table from the web service and parse it."""
        try:
            token_result = self._generate_token()
            if not token_result.get('success'):
                logger.warning('Cannot download %s table: %s', tipo, token_result.get('error'))
                return []
            body = (
                f'<Tabella xmlns="{self.SERVICE_NS}">'
                f'<Utente>{self._escape_xml(self._get_username())}</Utente>'
                f'<token>{self._escape_xml(token_result["token"])}</token>'
                f'<tipo>{tipo}</tipo>'
                f'<CSV></CSV>'
                f'</Tabella>'
            )
            result = self._call('Tabella', self._build_soap_envelope(body))
            if not result.get('success'):
                return []
            root = ET.fromstring(result.get('response_xml', ''))
            err = self._find_detail_error(root)
            if err:
                logger.warning('Cannot download %s table: %s', tipo, err)
                return []
            csv_el = root.find('.//{*}CSV')
            if csv_el is None or not csv_el.text:
                return []
            return parser(csv_el.text)
        except ET.ParseError:
            logger.exception('Invalid %s table response', tipo)
            return []
        except Exception:
            logger.exception('Failed to load %s table', tipo)
            return []

    def _get_document_codes(self) -> dict[str, str]:
        """Map lowercase description -> 5-char code from the Tipi_Documento table."""
        if self.test_mode:
            return {}
        if self._document_codes is None:
            codes = {}

            def parse(text: str) -> dict[str, str]:
                for line in text.splitlines():
                    cols = [c.strip() for c in line.split(';')]
                    code = next((c for c in cols if c and c.isascii() and c.isalpha() and len(c) == 5), None)
                    desc = next((c for c in cols if c and len(c) > 5), None)
                    if code and desc:
                        codes[desc.lower()] = code
                return codes

            self._document_codes = self._load_table('Tipi_Documento', parse) or codes
        return self._document_codes

    def _map_doc_type(self, doc_type: str) -> str | None:
        """Resolve the internal document type to an AlloggiatiWeb 5-char code."""
        if not doc_type:
            return None
        key = doc_type.strip().lower().replace('_', ' ')
        if key in ('id card', 'id_card', 'carta identita', 'carta d\'identita'):
            return 'IDENT'
        for internal, keywords in self._DOC_TYPE_KEYWORDS.items():
            for kw in keywords:
                if kw in key:
                    codes = self._get_document_codes()
                    for desc, code in codes.items():
                        if kw in desc:
                            return code
        return None

    def _find_comune(self, name: str) -> tuple[str, str]:
        """Return (9-char codice, 2-char provincia) for an Italian comune name."""
        if self.test_mode:
            return '', ''
        if self._luoghi is None:
            self._luoghi = self._load_table('Luoghi', self._parse_luoghi)
        target = self._normalize(name)
        for code, comune, prov in self._luoghi:
            if target == self._normalize(comune):
                return code, prov
        for code, comune, prov in self._luoghi:
            if self._normalize(comune).startswith(target):
                return code, prov
        return '', ''

    @staticmethod
    def _parse_luoghi(text: str) -> list[tuple[str, str, str]]:
        rows = []
        for line in text.splitlines():
            cols = [c.strip() for c in line.split(';')]
            code = next((c for c in cols if c.isdigit() and len(c) == 9), None)
            prov = next((c for c in cols if len(c) == 2 and c.isupper() and c.isalpha()), None)
            comune = next((c for c in cols if c and not c.isdigit() and c != prov), None)
            if code and comune:
                rows.append((code, comune, prov or ''))
        return rows

    @staticmethod
    def _normalize(text: str) -> str:
        import unicodedata
        return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()

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

        # Best-effort: warm the reference table caches (document codes,
        # comuni). Failures are non-fatal — unresolved fields stay blank and
        # surface as API validation errors in the log.
        if not self.test_mode:
            self._get_document_codes()
            self._find_comune('')

        schedine = self.build_schedine(guests)
        request_xml = '\r\n'.join(schedine)

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
                document_country=document_country or '',
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
