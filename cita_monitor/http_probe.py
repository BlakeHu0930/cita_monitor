"""Read-only transport experiment: no identity submission or appointment booking.

Run separately: .venv/bin/python -m cita_monitor.http_probe
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import tomllib
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def describe(response):
    body = response.text()
    title = re.search(r'<title[^>]*>(.*?)</title>', body, re.S | re.I)
    return {'status': response.status, 'bytes': len(body),
            'title': title.group(1).strip() if title else '',
            'entry_form': bool(re.search(r'id=["\x27]sede["\x27]', body)),
            'sin_clave_entry': bool(re.search(r'id=["\x27]btnEntrar["\x27]', body)),
            'no_slots_text': 'En este momento no hay citas disponibles' in body,
            'rejected': 'Request Rejected' in body or 'FortiGate' in body}


def main():
    os.umask(0o077)
    os.environ.setdefault('NODE_EXTRA_CA_CERTS', str(ROOT / 'certs/rapidssl-tls-rsa-ca-g1.pem'))
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.browsers'))
    from playwright.sync_api import sync_playwright
    from .core import start_url
    config = tomllib.loads((ROOT / 'config.toml').read_text())
    url = start_url(config)
    records = []
    step = 'http_entry'

    def record(step, data):
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'step': step, **data}
        records.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    try:
        with sync_playwright() as p:
            client = p.request.new_context()
            try:
                record(step, describe(client.get(url, timeout=30000)))
            finally:
                client.dispose()
            browser = p.chromium.launch(headless=False)
            try:
                context = browser.new_context(locale='es-ES')
                page = context.new_page()
                page.set_default_timeout(45000)
                step = 'browser_session'
                page.goto(url, wait_until='domcontentloaded')
                page.locator('#sede').wait_for()
                record(step, {'entry_form': True})
                step = 'http_shared_session_entry'
                record(step, describe(context.request.get(url, timeout=30000)))
                # The GET may rotate form tokens. Reload the browser's form before serializing.
                step = 'browser_current_form'
                page.reload(wait_until='domcontentloaded')
                page.locator('#sede').wait_for()
                if page.locator('#sede').input_value() != config['appointment']['office']:
                    page.locator('#sede').select_option(config['appointment']['office'])
                page.locator('[id="tramiteGrupo[0]"]').select_option(config['appointment']['procedure'])
                form = page.locator('#portadaForm').evaluate(
                    'f=>({action:f.action,fields:Object.fromEntries(new FormData(f))})')
                dest = urlsplit(form['action'])
                if (dest.scheme != 'https' or dest.hostname != 'icp.administracionelectronica.gob.es'
                        or dest.path.split(';')[0] != '/icpplustieb/acInfo'):
                    raise ValueError('Unexpected form destination')
                step = 'http_procedure_post'
                started = time.monotonic()
                response = context.request.post(form['action'], form=form['fields'], timeout=30000)
                record(step, {**describe(response), 'seconds': round(time.monotonic()-started, 2)})
            finally:
                browser.close()
    except Exception as exc:
        record(step, {'error_type': type(exc).__name__})
        return 2
    finally:
        folder = ROOT / 'runtime'
        folder.mkdir(exist_ok=True)
        with (folder / 'http-probe.jsonl').open('a') as f:
            for entry in records:
                f.write(json.dumps(entry, ensure_ascii=False)+'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
