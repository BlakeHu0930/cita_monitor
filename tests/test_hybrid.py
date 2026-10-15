import copy
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from cita_monitor.core import Result, Status
from cita_monitor.html_forms import Document, FormMismatch
from cita_monitor.hybrid import HybridMonitor, SessionExpired, StopQuery, official_url

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / 'config.example.toml').read_text())
CONFIG['appointment']['procedure'] = '4010'
CONFIG['browser']['capture_changes'] = False
CONFIG.setdefault('http', {})['capture_forms'] = False
BASE = 'https://icp.administracionelectronica.gob.es/icpplustieb/'
ENTRY = '''<form method="post" action="acInfo"><input type="hidden" name="token" value="entry-token">
<select id="sede" name="sede"><option value="99">Any</option></select>
<select id="tramiteGrupo[0]" name="tramiteGrupo[0]"><option value="4010">Huellas</option></select></form>
<p>En este momento no hay citas disponibles</p>'''
INFO = '''<div id="btnEntrar">Sin Cl@ve</div><form method="post" action="acEntrada">
<input type="hidden" name="acceso" value="N"><input type="hidden" name="token" value="info-token"></form>'''
IDENTITY = '''<form method="post" action="acValidarEntrada"><input name="token" type="hidden" value="identity-token">
<input id="rdbTipoDocNie" type="radio" name="docType" value="N.I.E.">
<input id="txtIdCitado" name="document"><input id="txtDesCitado" name="person">
<select id="txtPaisNac" name="country"><option value="999">TEST</option></select></form>'''
NONE = '<p>En este momento no hay citas disponibles</p>'
ENV = {'CITA_DOCUMENT': 'LOCAL_TEST', 'CITA_NAME': 'PRIVATE NAME', 'CITA_NATIONALITY': 'TEST'}


def response(body, code=200, headers=None):
    r = Mock(status=code, headers=headers or {})
    r.text.return_value = body
    return r


class FormTests(unittest.TestCase):
    def test_preserves_hidden_and_repeated_fields_and_select_values(self):
        doc = Document('''<form method="post"><input name="csrf" value="fresh" type="hidden">
        <input name="repeat" value="1"><input name="repeat" value="2">
        <input name="ignored" disabled value="x"><input name="unchecked" type="checkbox">
        <select id="nationality" name="pais"><option value="001"> ONE </option>
        <option value="002" selected>TWO</option></select></form>''', BASE)
        form = doc.forms[0]
        self.assertEqual(form.fields(), [('csrf','fresh'),('repeat','1'),('repeat','2'),('pais','002')])
        self.assertEqual(form.option('nationality', label='ONE'), ('pais','001'))
        with self.assertRaises(FormMismatch):
            form.fields({'missing': 'no'})

    def test_excludes_head_script_and_noscript_from_classification(self):
        doc = Document('<head><script>bad</script></head><body><noscript>bad</noscript><p>Result</p></body>', BASE)
        self.assertEqual(doc.text.strip(), 'Result')

    def test_non_query_urls_rejected(self):
        for path in ['https://example.org/acInfo', BASE+'acConfirmarCita',
                     'http://icp.administracionelectronica.gob.es/icpplustieb/acEntrada',
                     'https://user@icp.administracionelectronica.gob.es/icpplustieb/acEntrada']:
            with self.assertRaises(FormMismatch):
                official_url(path)


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.monitor = HybridMonitor()
        self.monitor.client = Mock()
        self.config = copy.deepcopy(CONFIG)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_full_http_flow_and_cookie_client_reused(self):
        client = self.monitor.client
        client.fetch.side_effect = [response(s) for s in [ENTRY, INFO, IDENTITY, NONE] * 2]
        for _ in range(2):
            result = self.monitor.check(self.config, Path('/unused'))
            self.assertEqual(result.status, Status.UNAVAILABLE)
            self.assertEqual(result.transport, 'hybrid_http')
        self.assertIs(self.monitor.client, client)
        self.assertEqual(self.monitor.bootstrap_count, 0)
        calls = client.fetch.call_args_list
        self.assertEqual(len(calls), 8)
        self.assertTrue(calls[2].args[0].endswith('/acEntrada'))
        self.assertEqual(parse_qs(calls[2].kwargs['data'])['acceso'], ['N'])
        identity = parse_qs(calls[3].kwargs['data'])
        self.assertEqual(identity['token'], ['identity-token'])
        self.assertEqual(identity['country'], ['999'])
        self.assertEqual(identity['document'], ['LOCAL_TEST'])
        self.assertEqual(identity['docType'], ['N.I.E.'])

    def test_refresh_once_then_recover(self):
        expired = self.monitor.client
        expired.fetch.return_value = response('<head><title>Request Rejected</title></head>'
                                               '<body>The requested URL was rejected.</body>')
        fresh = Mock()
        fresh.fetch.side_effect = [response(s) for s in [ENTRY,INFO,IDENTITY,NONE]]
        def bootstrap(config):
            self.monitor.client = fresh
        with patch.object(self.monitor, 'bootstrap', side_effect=bootstrap) as boot:
            result = self.monitor.check(self.config, Path('/unused'))
        self.assertEqual(result.status, Status.UNAVAILABLE)
        boot.assert_called_once()
        expired.dispose.assert_called_once()

    def test_repeated_rejection_stops_after_one_refresh(self):
        self.monitor.client.fetch.return_value = response('Request Rejected')
        def bootstrap(config):
            self.monitor.client = Mock()
            self.monitor.client.fetch.return_value = response('Request Rejected')
        with patch.object(self.monitor, 'bootstrap', side_effect=bootstrap) as boot:
            result = self.monitor.check(self.config, Path('/unused'))
        self.assertEqual(result.status, Status.BLOCKED)
        boot.assert_called_once()
        self.assertIsNone(self.monitor.client)

    def test_server_errors_limits_and_captcha_do_not_refresh(self):
        for body, code, status in [('broken',503,Status.ERROR), ('slow down',429,Status.BLOCKED),
                                    ('<input name="captcha">',200,Status.BLOCKED)]:
            with self.subTest(code=code, body=body):
                self.monitor.client.fetch.return_value = response(body,code)
                with patch.object(self.monitor, 'bootstrap') as boot:
                    result = self.monitor.check(self.config, Path('/unused'))
                self.assertEqual(result.status,status)
                boot.assert_not_called()

    def test_external_redirect_blocked_before_request(self):
        self.monitor.client.fetch.return_value = response('',302,{'location':'https://example.org/'})
        with self.assertRaises(FormMismatch):
            self.monitor.request(BASE+'acValidarEntrada',self.config,[('person','PRIVATE')])
        self.assertEqual(self.monitor.client.fetch.call_count,1)

    def test_identity_validation_form_not_no_slots(self):
        self.monitor.client.fetch.side_effect = [response(s) for s in [ENTRY,INFO,IDENTITY,IDENTITY+NONE]]
        self.assertEqual(self.monitor.check(self.config,Path('/unused')).status,Status.REVIEW)

    def test_unknown_page_never_available(self):
        self.monitor.client.fetch.side_effect = [response(s) for s in [ENTRY,INFO,IDENTITY,'<p>New calendar</p>']]
        self.assertEqual(self.monitor.check(self.config,Path('/unused')).status,Status.REVIEW)

    def test_post_returning_entry_is_expired(self):
        self.monitor.client.fetch.return_value=response(ENTRY)
        with self.assertRaises(SessionExpired):
            self.monitor.request(BASE+'acValidarEntrada',self.config,[('person','PRIVATE')])

    def test_network_exception_does_not_leak_form_or_refresh_cookie(self):
        self.monitor.client.fetch.side_effect=RuntimeError('PRIVATE NAME secret-cookie')
        with patch.object(self.monitor,'bootstrap') as boot:
            r=self.monitor.check(self.config,Path('/unused'))
        self.assertEqual(r.status,Status.ERROR)
        self.assertNotIn('PRIVATE',r.detail)
        self.assertNotIn('secret-cookie',r.detail)
        boot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
