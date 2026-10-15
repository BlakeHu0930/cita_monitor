"""Archive form/API structure without storing reusable credentials or identity values."""
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from .core import target_key
from .snapshots import private_values

LOG = logging.getLogger(__name__)
PUBLIC_FIELDS = {'sede', 'tramitegrupo[0]', 'subtramite', 'acceso', 'locale', 'p',
                 'rdbtipodoc', 'idsede', 'fecha', 'hora', 'idfecha', 'idhora'}
UUID = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.I)
SENSITIVE = re.compile(r'csrf|token|cookie|session|password|captcha|verific|codigo|secret|nonce', re.I)


def redact(text, secrets):
    text = str(text)
    for value in secrets:
        text = re.sub(re.escape(value), '[private]', text, flags=re.I)
    text = re.sub(r'\b[XYZ]\d{7}[A-Z]\b|[\w.+-]+@[\w.-]+\.[a-z]{2,}', '[private]', text, flags=re.I)
    return text


def public_value(name, value, secrets):
    # Only known business selectors may retain values; unknown inputs are schema only.
    if name.lower() in PUBLIC_FIELDS and not SENSITIVE.search(name):
        return redact(value, secrets)
    return '[redacted]' if value else ''


def safe_url(base, value, secrets):
    u = urlsplit(urljoin(base, value))
    if u.scheme not in ('http', 'https'):
        return {'url': '[non-http reference]', 'query_parameters': []}
    path = u.path.split(';')[0]
    path = re.sub(r'/[A-Za-z0-9_-]{40,}(?=/|$)', '/[opaque]', path)
    query = [(redact(k, secrets), public_value(k, v, secrets)) for k, v in parse_qsl(u.query, keep_blank_values=True)]
    host = u.hostname or ''
    return {'url': redact(urlunsplit((u.scheme, host, path, urlencode(query), '')), secrets),
            'query_parameters': [k for k, _ in query]}


def handler_structure(code, base, secrets):
    # Record names and static route references, never literal arguments/JS source.
    calls = sorted(set(re.findall(r'\b([A-Za-z_$][\w.$]*)\s*\(', code)))
    paths = sorted(set(re.findall(r'''["']((?:/icpplustieb/)?ac[A-Z][A-Za-z0-9_]*(?:\?[^"']*)?)["']''', code)))
    return {'functions': [redact(c, secrets) for c in calls],
            'endpoint_candidates': [safe_url(base, p, secrets) for p in paths],
            'verified_request': False}


class ScriptReferences(HTMLParser):
    def __init__(self, html, base, secrets):
        super().__init__(convert_charrefs=True)
        self.sources, self.inline = [], []
        self.in_script = False
        self.base, self.secrets = base, secrets
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.in_script = True
            src = dict(attrs).get('src')
            if src:
                self.sources.append(safe_url(self.base, src, self.secrets))

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_script = False

    def handle_data(self, text):
        if self.in_script:
            refs = handler_structure(text, self.base, self.secrets)
            if refs['endpoint_candidates']:
                self.inline.append(refs)


def describe_document(doc, config, *, method='GET', fields=None, status=200):
    secrets = private_values(config)
    forms = []
    for form in doc.forms:
        controls = []
        for c in form.controls:
            a = c.attrs
            name = a.get('name', '')
            attrs = {k: redact(a[k], secrets) for k in
                     ('id', 'name', 'type', 'min', 'max', 'step', 'minlength', 'maxlength', 'pattern', 'autocomplete')
                     if k in a}
            attrs.update({k: k in a for k in ('required', 'disabled', 'readonly', 'multiple', 'checked')})
            # Save option values only for recognised office/date/time selectors.
            options = [{'value': public_value(name, o.attrs.get('value', o.text), secrets),
                        'label': redact(o.text.strip(), secrets),
                        'selected': 'selected' in o.attrs, 'disabled': 'disabled' in o.attrs}
                       for o in c.options]
            control = {'tag': c.tag, 'attributes': attrs, 'options': options,
                       'value': public_value(name, a.get('value', c.text if c.tag == 'textarea' else ''), secrets)}
            if c.tag == 'button' or a.get('type') in ('button', 'submit', 'reset'):
                control['label'] = redact(a.get('value', c.text).strip(), secrets)
            for event in ('onclick', 'onchange'):
                if a.get(event):
                    control[event] = handler_structure(a[event], doc.url, secrets)
            if a.get('formaction'):
                control['formaction'] = safe_url(doc.url, a['formaction'], secrets)
                control['formmethod'] = a.get('formmethod', form.attrs.get('method', 'get')).upper()
            controls.append(control)
        forms.append({'id': redact(form.attrs.get('id', ''), secrets),
                      'name': redact(form.attrs.get('name', ''), secrets),
                      'method': form.attrs.get('method', 'get').upper(),
                      'action': safe_url(doc.url, form.attrs.get('action', doc.url), secrets),
                      'enctype': form.attrs.get('enctype', 'application/x-www-form-urlencoded'),
                      'onsubmit': handler_structure(form.attrs.get('onsubmit', ''), doc.url, secrets),
                      'controls': controls})
    references = ScriptReferences(doc.html, doc.url, secrets)
    return {'schema_version': 1, 'source': 'HTTP response; not evidence of availability',
            'page': safe_url(doc.url, doc.url, secrets),
            'observed_request': {'method': method, 'status': status,
                                 'parameters': [{'name': redact(k, secrets), 'value': public_value(k, v, secrets)}
                                                for k, v in (fields or [])]},
            'forms': forms,
            'page_handlers': [{'tag': e.tag, 'id': redact(e.attrs.get('id', ''), secrets),
                               'events': {k: handler_structure(e.attrs[k], doc.url, secrets)
                                          for k in ('onclick', 'onchange') if e.attrs.get(k)}}
                              for e in doc.elements if e.attrs.get('onclick') or e.attrs.get('onchange')],
            'scripts': {'external': references.sources, 'static_references': references.inline},
            'limitations': ['Only observed response structure; JavaScript-generated fields may be absent.',
                           'Hidden/security/identity values redacted. Fetch fresh tokens before any future submission.',
                           'Form actions and static JS references are not verified booking API calls.']}


def archive_document(doc, config, output_dir, *, method='GET', fields=None, status=200, redirect=None):
    """Save every distinct observed schema, including intermediate query pages."""
    if not config.get('http', {}).get('capture_forms', True):
        return None
    evidence = describe_document(doc, config, method=method, fields=fields, status=status)
    if redirect:
        evidence['redirect'] = safe_url(doc.url, redirect, private_values(config))
    # Ignore dynamic UUID field names in the comparison, retain observed names in JSON.
    canonical = UUID.sub('[dynamic-name]', json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    folder = Path(output_dir) / 'forms' / target_key(config)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    index = folder / 'index.jsonl'
    if index.exists():
        for line in index.read_text().splitlines():
            try:
                entry = json.loads(line)
                path = folder / entry['file']
                if entry['digest'] == digest and path.is_file():
                    return str(path.resolve())
            except (ValueError, KeyError):
                continue
    now = datetime.now(timezone.utc)
    path = folder / f'{now.strftime("%Y%m%dT%H%M%S_%fZ")}_{uuid4().hex[:8]}.json'
    evidence['captured_at'] = now.isoformat()
    evidence['structure_digest'] = digest
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2)
    with os.fdopen(os.open(index, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as f:
        f.write(json.dumps({'digest': digest, 'file': path.name, 'captured_at': evidence['captured_at']}) + '\n')
    LOG.info('已保存脱敏表单结构: %s', path.name)
    return str(path.resolve())
