"""
ROSS1000 (Regione Lazio) guest check-in reporting service.
Implements SOAP client for the GIES checkinV2 Web Service.
"""

import logging
import uuid
from datetime import date, datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.models import ComplianceConfig, Reservation, Ross1000Log

logger = logging.getLogger(__name__)

NAMESPACE_GIES = 'http://checkin.ws.service.turismo5.gies.it/'
NAMESPACE_V2 = 'V2'
SOAP_ENV = 'http://schemas.xmlsoap.org/soap/envelope/'

DEFAULT_ENDPOINT = 'https://lazioturismo.ross1000.it/ws/checkinV2'


class Ross1000Service:
    """SOAP client for ROSS1000 (Regione Lazio) check-in reporting."""

    def __init__(self, test_mode: bool = False):
        self.test_mode = test_mode
        self.session = self._create_session()
        self._config_cache: dict[str, str | None] = {}

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=['POST'],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def _get_config(self, key: str) -> str | None:
        if key not in self._config_cache:
            self._config_cache[key] = ComplianceConfig.get(key)
        return self._config_cache[key]

    def _get_username(self) -> str | None:
        import os
        return os.environ.get('ROSS1000_USERNAME') or self._get_config('ross1000_username')

    def _get_password(self) -> str | None:
        import os
        return os.environ.get('ROSS1000_PASSWORD') or self._get_config('ross1000_password')

    def _get_structure_code(self) -> str:
        import os
        return os.environ.get('ROSS1000_STRUCTURE_CODE') or self._get_config('ross1000_structure_code') or ''

    def _get_product(self) -> str:
        import os
        return os.environ.get('ROSS1000_PRODUCT') or self._get_config('ross1000_product') or 'CAV'

    def _get_endpoint(self) -> str:
        import os
        return os.environ.get('ROSS1000_ENDPOINT') or self._get_config('ross1000_endpoint') or DEFAULT_ENDPOINT

    def is_configured(self) -> bool:
        return bool(self._get_username() and self._get_password() and self._get_structure_code())

    def _escape_xml(self, text: str | None) -> str:
        if not text:
            return ''
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&apos;')

    def _fmt_date(self, d: date | None) -> str:
        if not d:
            return ''
        return d.strftime('%d/%m/%Y')

    def _build_soap_envelope(self, body_xml: str) -> str:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}"
                  xmlns:gies="{NAMESPACE_GIES}"
                  xmlns:v2="{NAMESPACE_V2}">
   <soapenv:Header/>
   <soapenv:Body>
{body_xml}
   </soapenv:Body>
</soapenv:Envelope>'''

    def _build_arrivo_xml(self, guest: dict[str, Any], is_primary: bool) -> str:
        idcapo = ''
        if not is_primary:
            idcapo = f'<gies:idcapo>{self._escape_xml(guest.get("idcapo", ""))}</gies:idcapo>'

        return f'''<gies:arrivo>
            <gies:idswh>{self._escape_xml(guest.get('idswh', ''))}</gies:idswh>
            <gies:tipoalloggiato>{'1' if is_primary else '2'}</gies:tipoalloggiato>
            {idcapo}
            <gies:cognome>{self._escape_xml(guest.get('cognome', ''))}</gies:cognome>
            <gies:nome>{self._escape_xml(guest.get('nome', ''))}</gies:nome>
            <gies:sesso>{self._escape_xml(guest.get('sesso', ''))}</gies:sesso>
            <gies:cittadinanza>{self._escape_xml(guest.get('cittadinanza', ''))}</gies:cittadinanza>
            <gies:statoresidenza>{self._escape_xml(guest.get('statoresidenza', ''))}</gies:statoresidenza>
            <gies:luogoresidenza>{self._escape_xml(guest.get('luogoresidenza', ''))}</gies:luogoresidenza>
            <gies:datanascita>{self._escape_xml(guest.get('datanascita', ''))}</gies:datanascita>
            <gies:statonascita>{self._escape_xml(guest.get('statonascita', ''))}</gies:statonascita>
            <gies:comunenascita>{self._escape_xml(guest.get('comunenascita', ''))}</gies:comunenascita>
            <gies:tipoturismo>{self._escape_xml(guest.get('tipoturismo', 'ALTRO'))}</gies:tipoturismo>
            <gies:mezzotrasporto>{self._escape_xml(guest.get('mezzotrasporto', ''))}</gies:mezzotrasporto>
            <gies:canaleprenotazione>{self._escape_xml(guest.get('canaleprenotazione', ''))}</gies:canaleprenotazione>
            <gies:titolostudio>{self._escape_xml(guest.get('titolostudio', ''))}</gies:titolostudio>
            <gies:professione>{self._escape_xml(guest.get('professione', ''))}</gies:professione>
            <gies:esenzioneimposta>{self._escape_xml(guest.get('esenzioneimposta', 'NO'))}</gies:esenzioneimposta>
        </gies:arrivo>'''

    def build_movimento_xml(
        self,
        data: date,
        arrivi: list[dict[str, Any]],
        partenze: list[dict[str, Any]] | None = None,
        prenotazioni: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build a <v2:movimento> XML block."""

        arrivi_xml = ''
        if arrivi:
            arrivi_xml = '<gies:arrivi>\n' + '\n'.join(
                self._build_arrivo_xml(g, i == 0) for i, g in enumerate(arrivi)
            ) + '\n        </gies:arrivi>'

        partenze_xml = ''
        if partenze:
            p_xml = '\n'.join(
                f'''<gies:partenza>
            <gies:idswh>{self._escape_xml(p.get('idswh', ''))}</gies:idswh>
            <gies:tipoalloggiato>{self._escape_xml(p.get('tipoalloggiato', '1'))}</gies:tipoalloggiato>
            <gies:arrivo>{self._escape_xml(p.get('arrivo', ''))}</gies:arrivo>
        </gies:partenza>''' for p in partenze
            )
            partenze_xml = f'<gies:partenze>\n{p_xml}\n        </gies:partenze>'

        prenotazioni_xml = ''
        if prenotazioni:
            pr_xml = '\n'.join(
                f'''<gies:prenotazione>
            <gies:idswh>{self._escape_xml(p.get('idswh', ''))}</gies:idswh>
            <gies:arrivo>{self._escape_xml(p.get('arrivo', ''))}</gies:arrivo>
            <gies:partenza>{self._escape_xml(p.get('partenza', ''))}</gies:partenza>
            <gies:ospiti>{self._escape_xml(p.get('ospiti', '1'))}</gies:ospiti>
            <gies:camere>{self._escape_xml(p.get('camere', '1'))}</gies:camere>
            <gies:prezzo>{self._escape_xml(p.get('prezzo', ''))}</gies:prezzo>
            <gies:canaleprenotazione>{self._escape_xml(p.get('canaleprenotazione', ''))}</gies:canaleprenotazione>
            <gies:statoprovenienza>{self._escape_xml(p.get('statoprovenienza', ''))}</gies:statoprovenienza>
            <gies:comuneprovenienza>{self._escape_xml(p.get('comuneprovenienza', ''))}</gies:comuneprovenienza>
        </gies:prenotazione>''' for p in prenotazioni
            )
            prenotazioni_xml = f'<gies:prenotazioni>\n{pr_xml}\n        </gies:prenotazioni>'

        struttura_xml = f'''<gies:struttura>
            <gies:apertura>A</gies:apertura>
            <gies:camereoccupate>1</gies:camereoccupate>
            <gies:cameredisponibili>0</gies:cameredisponibili>
            <gies:lettidisponibili>2</gies:lettidisponibili>
        </gies:struttura>'''

        return f'''<movimento>
        <gies:data>{self._fmt_date(data)}</gies:data>
        {struttura_xml}
        {arrivi_xml}
        {partenze_xml}
        {prenotazioni_xml}
    </movimento>'''

    def _build_request_xml(
        self,
        codice: str,
        prodotto: str,
        movimenti_xml: str,
    ) -> str:
        """Build the full SOAP request XML for inviaMovimentazione."""
        body = f'''<gies:inviaMovimentazione>
        <movimentazione>
            <codice>{self._escape_xml(codice)}</codice>
            <prodotto>{self._escape_xml(prodotto)}</prodotto>
            {movimenti_xml}
        </movimentazione>
    </gies:inviaMovimentazione>'''
        return self._build_soap_envelope(body)

    def _call(self, request_xml: str) -> dict[str, Any]:
        """Send SOAP request and return parsed result."""
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': '',
        }

        username = self._get_username()
        password = self._get_password()

        if self.test_mode:
            logger.info('[TEST MODE] ROSS1000 SOAP request (not sent):\n%s', request_xml[:2000])
            return {'success': True, 'test_mode': True, 'response_xml': '<TestResponse>OK</TestResponse>'}

        try:
            response = self.session.post(
                self._get_endpoint(),
                data=request_xml.encode('utf-8'),
                headers=headers,
                auth=(username, password) if username and password else None,
                timeout=60,
            )
        except requests.exceptions.Timeout:
            logger.error('ROSS1000 request timed out')
            return {'success': False, 'error': 'Request timed out'}
        except requests.exceptions.ConnectionError as e:
            logger.error('ROSS1000 connection error: %s', e)
            return {'success': False, 'error': f'Connection error: {e}'}
        except Exception as e:
            logger.exception('ROSS1000 request failed')
            return {'success': False, 'error': str(e)}

        response_text = response.text

        if response.status_code != 200:
            logger.error('ROSS1000 HTTP %s: %s', response.status_code, response_text[:1000])
            return {'success': False, 'error': f'HTTP {response.status_code} — {response_text[:300]}', 'response_xml': response_text}

        return self._parse_response(response_text)

    def _parse_response(self, response_xml: str) -> dict[str, Any]:
        """Parse SOAP response XML from ROSS1000."""
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(response_xml)
        except ET.ParseError:
            logger.warning('ROSS1000 response is not valid XML')
            return {'success': True if 'OK' in response_xml else False, 'response_xml': response_xml}

        namespaces = {
            'soap': SOAP_ENV,
            'gies': NAMESPACE_GIES,
        }

        body = root.find('.//soap:Body', namespaces)
        if body is None:
            return {'success': False, 'error': 'No SOAP body in response', 'response_xml': response_xml}

        fault = body.find('soap:Fault', namespaces)
        if fault is not None:
            faultstring = fault.find('faultstring')
            detail = fault.find('detail')
            msg = faultstring.text if faultstring is not None else 'SOAP Fault'
            detail_text = ''
            if detail is not None:
                detail_text = ET.tostring(detail, encoding='unicode')
            return {'success': False, 'error': msg, 'detail': detail_text, 'response_xml': response_xml}

        # Parse risultati
        risultati = body.find('.//gies:return', namespaces)
        if risultati is not None:
            errors = []
            for struttura in risultati.findall('gies:struttura', namespaces):
                err = struttura.get('errore') or struttura.findtext('gies:errore', namespaces)
                if err:
                    errors.append(err)
                for giorno in struttura.findall('gies:giorno', namespaces):
                    for arrivo in giorno.findall('gies:arrivi/gies:arrivo', namespaces):
                        idswh = arrivo.findtext('gies:idswh', namespaces) or ''
                        successo = arrivo.findtext('gies:successo', namespaces)
                        errore = arrivo.findtext('gies:errore', namespaces)
                        if errore:
                            errors.append(f'{idswh}: {errore}')
                        if successo and successo.lower() == 'false':
                            errors.append(f'{idswh}: rejected')

            if errors:
                return {'success': False, 'error': '; '.join(errors), 'response_xml': response_xml}
            return {'success': True, 'response_xml': response_xml}

        return {'success': True, 'response_xml': response_xml}

    def invia_movimentazione(
        self,
        codice: str | None = None,
        prodotto: str | None = None,
        movimenti: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send one or more movimenti to ROSS1000."""
        codice = codice or self._get_structure_code()
        prodotto = prodotto or self._get_product()

        if not codice:
            return {'success': False, 'error': 'ROSS1000 structure code not configured'}

        movimenti_xml = ''
        if movimenti:
            movimenti_xml = '\n'.join(
                self.build_movimento_xml(
                    data=m.get('data'),
                    arrivi=m.get('arrivi', []),
                    partenze=m.get('partenze'),
                    prenotazioni=m.get('prenotazioni'),
                )
                for m in movimenti
            )

        request_xml = self._build_request_xml(codice, prodotto, movimenti_xml)
        return self._call(request_xml)

    def _reservation_to_guests(self, reservation: Reservation) -> list[dict[str, Any]]:
        """Convert a Reservation to a list of guest dicts for ROSS1000."""
        primary_id = f'RES{reservation.id:06d}_G1'
        checkin_str = self._fmt_date(reservation.check_in)

        guests = []

        # Primary guest (capogruppo)
        nazionalita = reservation.guest_nationality or 'ITA'
        comune_nascita = reservation.guest_birth_place or ''
        stato_nascita = 'ITA'

        primary = {
            'idswh': primary_id,
            'cognome': reservation.guest_surname or reservation.guest_name.split()[-1] if reservation.guest_name else '',
            'nome': reservation.guest_first_name or reservation.guest_name.split()[0] if reservation.guest_name else '',
            'sesso': reservation.guest_gender or 'M',
            'cittadinanza': nazionalita,
            'statoresidenza': nazionalita,
            'luogoresidenza': comune_nascita,
            'datanascita': self._fmt_date(reservation.guest_birth_date),
            'statonascita': stato_nascita,
            'comunenascita': comune_nascita,
            'tipoturismo': 'ALTRO',
            'canaleprenotazione': reservation.source or 'DIRETTA',
        }
        guests.append(primary)

        # Additional guests (if num_guests > 1)
        for i in range(1, reservation.num_guests or 1):
            guest_id = f'RES{reservation.id:06d}_G{i + 1}'
            extra = {
                'idswh': guest_id,
                'idcapo': primary_id,
                'cognome': f'Ospite {i + 1}',
                'nome': reservation.guest_first_name or '',
                'sesso': 'M',
                'cittadinanza': nazionalita,
                'statoresidenza': nazionalita,
                'luogoresidenza': comune_nascita,
                'datanascita': self._fmt_date(reservation.guest_birth_date),
                'statonascita': stato_nascita,
                'comunenascita': comune_nascita,
                'tipoturismo': 'ALTRO',
                'canaleprenotazione': reservation.source or 'DIRETTA',
            }
            guests.append(extra)

        return guests

    def submit_reservation(self, reservation: Reservation) -> dict[str, Any]:
        """Submit a single reservation's check-in to ROSS1000."""
        codice = self._get_structure_code()
        if not codice:
            return {'success': False, 'error': 'ROSS1000 structure code not configured'}

        if not self._get_username() or not self._get_password():
            return {'success': False, 'error': 'ROSS1000 credentials not configured'}

        # Use CIR code from Apartment model if available, else the configured structure code
        from app.models import Apartment
        apt = Apartment.query.first()
        if apt and apt.cir_code:
            codice = apt.cir_code

        guests = self._reservation_to_guests(reservation)

        partenze = None
        if reservation.check_out:
            partenze = [
                {
                    'idswh': g['idswh'],
                    'tipoalloggiato': '1' if i == 0 else '2',
                    'arrivo': self._fmt_date(reservation.check_in),
                }
                for i, g in enumerate(guests)
            ]

        movimento = {
            'data': reservation.check_in,
            'arrivi': guests,
            'partenze': partenze,
        }

        request_xml = self._build_request_xml(
            codice,
            self._get_product(),
            self.build_movimento_xml(
                data=reservation.check_in,
                arrivi=guests,
                partenze=partenze,
            ),
        )

        log = Ross1000Log(
            reservation_id=reservation.id,
            action='submit',
            request_xml=request_xml,
            status='pending',
        )
        db.session.add(log)
        db.session.commit()

        try:
            result = self._call(request_xml)

            log.response_xml = result.get('response_xml', '')
            log.status = 'success' if result.get('success') else 'error'
            log.error_message = result.get('error', '')
            db.session.commit()

            reservation.ross1000_status = 'accepted' if result.get('success') else 'rejected'
            reservation.ross1000_submitted_at = datetime.utcnow()
            reservation.ross1000_response = result.get('response_xml', '')
            reservation.ross1000_error = result.get('error', '')
            db.session.commit()

            return result

        except Exception as e:
            logger.exception('ROSS1000 submission failed for reservation %s', reservation.id)
            log.status = 'error'
            log.error_message = str(e)
            db.session.commit()

            reservation.ross1000_status = 'rejected'
            reservation.ross1000_submitted_at = datetime.utcnow()
            reservation.ross1000_error = str(e)
            db.session.commit()

            return {'success': False, 'error': str(e)}

    def submit_reservations_bulk(self, reservations: list[Reservation]) -> dict[str, Any]:
        """Submit multiple reservations in a single SOAP call."""
        results = []
        for res in reservations:
            result = self.submit_reservation(res)
            results.append({'reservation_id': res.id, **result})
        return {'success': all(r.get('success') for r in results), 'results': results}

    def test_connection(self) -> dict[str, Any]:
        """Test the connection to ROSS1000 by sending an empty movimento."""
        codice = self._get_structure_code()
        if not codice:
            return {'success': False, 'error': 'ROSS1000 structure code not configured, set ROSS1000_STRUCTURE_CODE env var'}

        movimento = {
            'data': date.today(),
            'arrivi': [],
        }
        request_xml = self._build_request_xml(
            codice,
            self._get_product(),
            self.build_movimento_xml(data=date.today(), arrivi=[]),
        )

        logger.info('Testing ROSS1000 connection with structure code: %s', codice)
        return self._call(request_xml)


def get_ross1000_service(test_mode: bool = False) -> Ross1000Service:
    return Ross1000Service(test_mode=test_mode)
