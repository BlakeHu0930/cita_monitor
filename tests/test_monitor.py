import copy
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from cita_monitor.core import State, Status, classify, validate
from cita_monitor.notify import deliver

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "config.example.toml").read_text())
# These existing fixtures exercise the browser transport. HTTP has separate tests.
CONFIG['monitor']['transport'] = 'browser'


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'runtime').mkdir()
        patcher = patch('cita_monitor.__main__.ROOT', root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fingerprint_ignores_operation_id_but_preserves_slot_dates(self):
        from cita_monitor.snapshots import page_fingerprint
        a = page_fingerprint('/result;jsessionid=a', 'unavailable', 'No citas\nCod. Oper.: A 19/09/2026 10:00')
        b = page_fingerprint('/result;jsessionid=b', 'unavailable', 'No citas\nCod. Oper.: B 19/09/2026 10:10')
        self.assertEqual(a, b)
        self.assertNotEqual(page_fingerprint('/r', 'unknown', '20/09/2026 10:00'),
                            page_fingerprint('/r', 'unknown', '21/09/2026 10:00'))

    def test_email_attaches_png(self):
        from cita_monitor.notify import email
        path = Path(self.temp.name) / 'page.png'
        path.write_bytes(b'PNG-test-data')
        env = {CONFIG['smtp'][k]: 'test@example.invalid' for k in CONFIG['smtp'] if k.endswith('_env')}
        with patch.dict(os.environ, env), patch('cita_monitor.notify.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.send_message.return_value = {}
            email(CONFIG, 'test', 'body', attachment=path)
            message = smtp.return_value.send_message.call_args.args[0]
            attachment = list(message.iter_attachments())[0]
            self.assertEqual(attachment.get_content_type(), 'image/png')
            self.assertEqual(attachment.get_payload(decode=True), b'PNG-test-data')

    def test_changed_no_slots_email_retries_after_failure(self):
        from cita_monitor.__main__ import run
        from cita_monitor.core import Result
        state = State(Path(self.temp.name) / 'state.json')
        with patch('cita_monitor.__main__.check_with_retries', side_effect=[
                Result(Status.UNAVAILABLE, 'none', fingerprint='abc', page_changed=True),
                Result(Status.UNAVAILABLE, 'none', fingerprint='abc')]), \
                patch('cita_monitor.__main__.deliver', side_effect=[False, True]) as send:
            self.assertEqual(run(CONFIG, True, state, observe=True), 2)
            self.assertEqual(run(CONFIG, True, state, observe=True), 0)
            self.assertEqual(send.call_count, 2)
            self.assertIn('当前仍无号', send.call_args.args[4])
    def test_stage_error_redacts_original_message(self):
        from cita_monitor.browser import QueryFailure, stage
        with self.assertRaises(QueryFailure) as failure:
            with stage('加载官网入口'):
                raise TimeoutError('secret-form-value')
        self.assertEqual(str(failure.exception), '加载官网入口失败 (TimeoutError)')
        self.assertNotIn('secret-form-value', str(failure.exception))
    def test_homepage_notice_is_not_result(self):
        result = classify("En este momento no hay citas disponibles en esta sede", CONFIG["detection"])
        self.assertEqual(result.status, Status.UNKNOWN)

    def test_unavailable_overrides_positive(self):
        result = classify("En este momento no hay citas disponibles", CONFIG["detection"],
                          result_stage=True, slot_visible=True)
        self.assertEqual(result.status, Status.UNAVAILABLE)

    def test_missing_negative_does_not_mean_available(self):
        for text in ("", "Internal Server Error", "Solicitar Cita", "Seleccione oficina"):
            self.assertEqual(classify(text, CONFIG["detection"], result_stage=True).status, Status.UNKNOWN)

    def test_available_needs_result_and_slot(self):
        self.assertEqual(classify("Seleccione fecha", CONFIG["detection"], result_stage=True,
                                  slot_visible=True).status, Status.AVAILABLE)
        self.assertEqual(classify("Seleccione fecha", CONFIG["detection"], slot_visible=True).status, Status.UNKNOWN)

    def test_challenge_overrides_slot(self):
        self.assertEqual(classify("", CONFIG["detection"], result_stage=True,
                                  slot_visible=True, challenge=True).status, Status.BLOCKED)

    def test_persistent_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = State(path)
            self.assertTrue(state.due("a", "email", "available", 100, now=10))
            state.sent("a", "email", "available", now=10)
            state = State(path)
            self.assertFalse(state.due("a", "email", "available", 100, now=20))
            self.assertTrue(state.due("a", "email", "available", 100, now=110))
            self.assertTrue(state.due("a", "whatsapp", "available", 100, now=20))

    def test_failed_channel_retries_without_duplicating_success(self):
        config = copy.deepcopy(CONFIG)
        config["notifications"].update(email=True, whatsapp=True)
        with tempfile.TemporaryDirectory() as d, patch("cita_monitor.notify.email") as email, \
                patch("cita_monitor.notify.whatsapp", side_effect=[RuntimeError(), None]) as whatsapp:
            state = State(Path(d) / "state.json")
            self.assertFalse(deliver(config, state, "a", "available", "test", "body"))
            self.assertTrue(deliver(config, state, "a", "available", "test", "body"))
            self.assertEqual(email.call_count, 1)
            self.assertEqual(whatsapp.call_count, 2)

    def test_unverified_flow_cannot_start(self):
        config = copy.deepcopy(CONFIG)
        config["appointment"]["procedure"] = "4010"
        with self.assertRaisesRegex(ValueError, "inspect"):
            validate(config, "check")

    def test_observe_allows_unverified_flow_but_requires_identity(self):
        config = copy.deepcopy(CONFIG)
        config['appointment']['procedure'] = '4010'
        config['notifications']['email'] = True
        with patch.dict(os.environ, {'CITA_DOCUMENT': 'local-test', 'CITA_NAME': 'test', 'CITA_NATIONALITY': 'TEST'}):
            validate(config, 'observe')

    def test_observe_unknown_notifies_without_claiming_slots(self):
        from cita_monitor.__main__ import run
        from cita_monitor.core import Result
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'runtime').mkdir()
            state = State(root / 'runtime' / 'notifications.json')
            with patch('cita_monitor.__main__.ROOT', root), \
                    patch('cita_monitor.__main__.check_with_retries', return_value=Result(Status.UNKNOWN, 'new page')), \
                    patch('cita_monitor.__main__.deliver', return_value=True) as send:
                self.assertEqual(run(CONFIG, True, state, observe=True), 2)
                self.assertIn('未确认有号', send.call_args.args[4])

    def test_no_slots_stays_quiet_in_observe(self):
        from cita_monitor.__main__ import run
        from cita_monitor.core import Result
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'runtime').mkdir()
            state = State(root / 'runtime' / 'notifications.json')
            with patch('cita_monitor.__main__.ROOT', root), \
                    patch('cita_monitor.__main__.check_with_retries', return_value=Result(Status.UNAVAILABLE, 'none')), \
                    patch('cita_monitor.__main__.deliver') as send:
                self.assertEqual(run(CONFIG, True, state, observe=True), 0)
                send.assert_not_called()

    def test_network_failure_retries_then_recovers(self):
        from cita_monitor.__main__ import check_with_retries
        from cita_monitor.core import Result
        config = copy.deepcopy(CONFIG)
        config["monitor"]["jitter_seconds"] = 0
        with patch("cita_monitor.browser.session", side_effect=[TimeoutError(), TimeoutError(),
                    Result(Status.UNAVAILABLE, "none")]) as session, patch("cita_monitor.__main__.time.sleep") as sleep:
            self.assertEqual(check_with_retries(config).status, Status.UNAVAILABLE)
            self.assertEqual(session.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60])

    def test_blocked_does_not_retry_in_same_round(self):
        from cita_monitor.__main__ import check_with_retries
        from cita_monitor.core import Result
        with patch("cita_monitor.browser.session", return_value=Result(Status.BLOCKED, "403")) as session, \
                patch("cita_monitor.__main__.time.sleep") as sleep:
            self.assertEqual(check_with_retries(CONFIG).status, Status.BLOCKED)
            self.assertEqual(session.call_count, 1)
            sleep.assert_not_called()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_http_screenshot_is_offline_and_unchanged_skips_browser(self):
        from cita_monitor.snapshots import capture_http_change
        from cita_monitor.html_forms import Document
        from cita_monitor.core import Result
        document = Document('<div id="wrap"><p>No citas</p><input value="private"></div>'
                            '<script>document.body.innerHTML="MUTATED"</script>'
                            '<img src="https://example.invalid/never-fetch">',
                            'https://icp.administracionelectronica.gob.es/icpplustieb/acValidarEntrada')
        with tempfile.TemporaryDirectory() as d:
            result = capture_http_change(document, CONFIG, Result(Status.UNAVAILABLE, 'none'), d, self.playwright)
            self.assertIsNone(result.screenshot_error)
            self.assertTrue(Path(result.screenshot).is_file())
            self.assertIn('离线',result.screenshot_kind)
            # None cannot launch a browser: the existing image must be reused.
            repeated = capture_http_change(document, CONFIG, Result(Status.UNAVAILABLE, 'none'), d, None)
            self.assertIsNone(repeated.screenshot_error)
            self.assertEqual(repeated.screenshot,result.screenshot)
            self.assertFalse(repeated.page_changed)

    def test_screenshots_mask_identity_and_retain_changes(self):
        from cita_monitor.snapshots import capture_change
        from cita_monitor.core import Result
        context = self.browser.new_context()
        try:
            page = context.new_page()
            with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'CITA_NAME': 'PRIVATE TEST NAME'}):
                page.set_content('<div id="wrap"><p>PRIVATE TEST NAME</p><input value="secret"><p>No citas</p></div>')
                r = capture_change(page, CONFIG, Result(Status.UNAVAILABLE, 'none'), Path(d))
                self.assertIsNone(r.screenshot_error)
                self.assertTrue(Path(r.screenshot).exists())
                self.assertFalse(r.page_changed)
                self.assertEqual(page.locator('[data-cita-private]').count(), 1)
                same = capture_change(page, CONFIG, Result(Status.UNAVAILABLE, 'none'), Path(d))
                self.assertEqual(same.screenshot, r.screenshot)
                page.set_content('<div id="wrap">Seleccione fecha 20/09/2026</div>')
                changed = capture_change(page, CONFIG, Result(Status.UNKNOWN, 'new'), Path(d))
                self.assertTrue(changed.page_changed)
                self.assertNotEqual(changed.screenshot, r.screenshot)
                self.assertEqual(len(list((Path(d)/'screenshots').glob('*.png'))), 2)
        finally:
            context.close()

    def simulate(self, result_html):
        from cita_monitor.browser import query
        config = copy.deepcopy(CONFIG)
        config["appointment"]["procedure"] = "4010"
        config["browser"]["result_timeout_seconds"] = 0.05
        config["detection"]["available_selectors"] = ["button.slot"]
        pages = {
            "/icpplustieb/citar": '''<select id="sede" onchange="location.href='/unnecessary-office-post'"><option value="99">Any</option></select>
                <select id="tramiteGrupo[0]"><option value="4010">Test</option></select>
                <p>En este momento no hay citas disponibles</p>
                <button id="btnAceptar" onclick="location.href='/info'">Aceptar</button>''',
            "/info": '<button id="btnEntrar" onclick="location.href=\'/identity\'">Entrar</button>',
            "/identity": '''<input type="radio" id="rdbTipoDocNie"><input id="txtIdCitado">
                <input id="txtDesCitado"><select id="txtPaisNac"><option>TEST</option></select>
                <button id="btnEnviar" onclick="location.href='/request'">Aceptar</button>''',
            "/request": '<button onclick="location.href=\'/result\'">Solicitar Cita</button>',
            "/result": result_html,
        }
        from urllib.parse import urlsplit
        context = self.browser.new_context()
        context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=pages.get(urlsplit(route.request.url).path, "")))
        try:
            page = context.new_page()
            with patch.dict(os.environ, {"CITA_DOCUMENT": "LOCAL_TEST_ONLY", "CITA_NAME": "Test",
                                         "CITA_NATIONALITY": "TEST"}):
                return query(page, config)
        finally:
            context.close()

    def test_full_available_flow(self):
        self.assertEqual(self.simulate('<button class="slot">20/09/2026 10:30</button>').status, Status.AVAILABLE)

    def test_full_unavailable_flow(self):
        self.assertEqual(self.simulate('<p>En este momento no hay citas disponibles</p>').status, Status.UNAVAILABLE)

    def test_disabled_or_hidden_slot_not_available(self):
        self.assertEqual(self.simulate('<button class="slot" disabled>10:30</button>').status, Status.UNKNOWN)
        self.assertEqual(self.simulate('<button class="slot" hidden>10:30</button>').status, Status.UNKNOWN)

    def test_challenge(self):
        self.assertEqual(self.simulate('<p>Request Rejected</p>').status, Status.BLOCKED)

    def test_fortigate_403_identified_before_form(self):
        from cita_monitor.browser import open_entry
        context = self.browser.new_context()
        context.route("**/*", lambda route: route.fulfill(status=403, content_type="text/html",
                      body="<body>FortiGate Intrusion Prevention Triggered</body>"))
        try:
            result = open_entry(context.new_page(), CONFIG)
            self.assertEqual(result.status, Status.BLOCKED)
            self.assertIn("FortiGate", result.detail)
        finally:
            context.close()

    def test_early_unavailable_does_not_wait_for_missing_button(self):
        from cita_monitor.browser import open_identity
        config = copy.deepcopy(CONFIG)
        config['appointment']['procedure'] = '4010'
        context = self.browser.new_context()
        context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=
                      '<p>Información: En este momento no hay citas disponibles</p>'))
        try:
            page = context.new_page()
            page.goto('https://icp.administracionelectronica.gob.es/test')
            page.set_content('''<select id="sede"><option value="99">Any</option></select>
                <select id="tramiteGrupo[0]"><option value="4010">Test</option></select>
                <button id="btnAceptar" onclick="location.href='/info'">Aceptar</button>''')
            self.assertEqual(open_identity(page, config).status, Status.UNAVAILABLE)
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
