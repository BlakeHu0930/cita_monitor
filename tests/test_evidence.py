import copy
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from cita_monitor.evidence import archive_document, describe_document
from cita_monitor.html_forms import Document
from cita_monitor.hybrid import HybridMonitor
from cita_monitor.core import Status

ROOT=Path(__file__).resolve().parents[1]
CONFIG=tomllib.loads((ROOT/'config.example.toml').read_text())
CONFIG['appointment']['procedure']='4010'
CONFIG['browser']['capture_changes']=False
URL='https://icp.administracionelectronica.gob.es/icpplustieb/acOfertarCita;jsessionid=SESSION_SECRET?token=URL_SECRET'
HTML='''<form id="appointment" action="acConfirmar?csrf=ACTION_SECRET" method="post">
<input type="hidden" name="csrf" value="CSRF_SECRET">
<input type="hidden" name="12345678-abcd-1234-abcd-123456789012" value="ROTATING_SECRET">
<input id="person" name="person" value="PRIVATE NAME" required maxlength="80">
<input name="document" value="Y1234567Z"><input name="phone" value="123456789">
<select id="office" name="idSede"><option value="42">OFFICE A</option></select>
<input name="fecha" type="date" value="2026-10-01" min="2026-09-20">
<button type="submit" onclick="confirmar('JS_SECRET')">Confirmar</button></form>
<div id="next" onclick="document.getElementById('appointment').submit()">Next</div>
<script src="/icpplustieb/app.js?token=SCRIPT_SECRET"></script>
<script>function confirmar(){form.action='acReservar?csrf=INLINE_SECRET'; form.submit();}</script>'''


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'CITA_NAME':'PRIVATE NAME','CITA_DOCUMENT':'Y1234567Z'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_form_structure_and_request_parameters_without_secrets(self):
        doc=Document(HTML,URL)
        data=describe_document(doc,CONFIG,method='POST',fields=[('csrf','REQUEST_SECRET'),('person','PRIVATE NAME'),('idSede','42')])
        encoded=json.dumps(data,ensure_ascii=False)
        for secret in ['SESSION_SECRET','URL_SECRET','ACTION_SECRET','CSRF_SECRET','ROTATING_SECRET',
                       'PRIVATE NAME','Y1234567Z','123456789"','JS_SECRET','SCRIPT_SECRET','INLINE_SECRET','REQUEST_SECRET']:
            self.assertNotIn(secret,encoded)
        f=data['forms'][0]
        self.assertEqual(f['method'],'POST')
        self.assertIn('/acConfirmar?',f['action']['url'])
        self.assertEqual(f['controls'][2]['attributes']['maxlength'],'80')
        self.assertTrue(f['controls'][2]['attributes']['required'])
        self.assertEqual(f['controls'][5]['options'][0]['value'],'42')
        self.assertEqual(f['controls'][6]['value'],'2026-10-01')
        self.assertEqual(f['controls'][7]['onclick']['functions'],['confirmar'])
        self.assertFalse(f['controls'][7]['onclick']['verified_request'])
        self.assertEqual(data['scripts']['external'][0]['query_parameters'],['token'])
        self.assertIn('acReservar',json.dumps(data['scripts']['static_references']))

    def test_dedupes_rotating_tokens_and_retains_changed_forms(self):
        first=archive_document(Document(HTML,URL),CONFIG,self.folder)
        second_html=HTML.replace('12345678-abcd-1234-abcd-123456789012','abcdef01-abcd-1234-abcd-123456789012').replace('CSRF_SECRET','NEW_SECRET')
        repeated=archive_document(Document(second_html,URL),CONFIG,self.folder)
        self.assertEqual(first,repeated)
        changed=archive_document(Document(second_html.replace('maxlength="80"','maxlength="100"'),URL),CONFIG,self.folder)
        self.assertNotEqual(first,changed)
        self.assertTrue(Path(first).is_file())
        self.assertEqual(Path(first).stat().st_mode & 0o777,0o600)
        self.assertEqual(len((Path(first).parent/'index.jsonl').read_text().splitlines()),2)

    def test_record_redirect_target_without_following_booking_endpoint(self):
        path=archive_document(Document('<p>Continue</p>',URL),CONFIG,self.folder,status=302,
                              redirect='acVerFormulario?token=REDIRECT_SECRET')
        body=Path(path).read_text()
        self.assertIn('acVerFormulario',body)
        self.assertNotIn('REDIRECT_SECRET',body)

    def test_intermediate_documents_archived_before_captcha_stop(self):
        monitor=HybridMonitor();monitor.client=Mock()
        response=Mock(status=200,headers={})
        response.text.return_value=HTML+'<input name="captcha">'
        monitor.client.fetch.return_value=response
        result=monitor.check(CONFIG,self.folder)
        self.assertEqual(result.status,Status.BLOCKED)
        self.assertEqual(len(result.structure_files),1)
        self.assertIsNone(result.structure_error)
        self.assertIn('acConfirmar',Path(result.structure_files[0]).read_text())

    def test_archive_failure_does_not_suppress_alert(self):
        monitor=HybridMonitor();monitor.client=Mock()
        response=Mock(status=200,headers={})
        response.text.return_value='<input name="captcha">'
        monitor.client.fetch.return_value=response
        with patch('cita_monitor.hybrid.archive_document',side_effect=OSError('PRIVATE NAME')):
            result=monitor.check(CONFIG,self.folder)
        self.assertEqual(result.status,Status.BLOCKED)
        self.assertIn('OSError',result.structure_error)
        self.assertNotIn('PRIVATE NAME',result.structure_error)


if __name__=='__main__':unittest.main()
