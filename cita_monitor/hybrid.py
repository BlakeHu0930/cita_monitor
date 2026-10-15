"""Browser session bootstrap followed by HTTP-only appointment queries.

Cookies and dynamic form tokens remain in memory. No booking endpoint is allowed.
"""
import atexit
from contextlib import contextmanager
from dataclasses import replace
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from playwright.sync_api import sync_playwright

from .browser import HOST, QueryFailure, open_entry
from .core import Result, Status, classify, normalize, required_env, start_url
from .html_forms import Document, FormMismatch
from .snapshots import capture_http_change
from .evidence import archive_document
from .browser_settings import launch_options, new_context

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://' + HOST
# Deliberately excludes contact submission, choosing a slot and confirming a booking.
READ_PATHS = {'citar', 'selectSede', 'acInfo', 'acEntrada', 'acValidarEntrada', 'acCitar'}


def official_url(url):
    u = urlsplit(url)
    if (u.scheme != 'https' or u.hostname != HOST or u.port not in (None, 443)
            or u.username or u.password or u.path.split(';')[0] not in
            {'/icpplustieb/' + name for name in READ_PATHS}):
        raise FormMismatch('HTTP 目标不属于已验证的官方查询路径，已停止')
    return url


class SessionExpired(Exception):
    pass


class StopQuery(Exception):
    def __init__(self, result):
        self.result = result


@contextmanager
def stage(name):
    started = time.monotonic()
    LOG.info('阶段开始: %s', name)
    try:
        yield
    except (StopQuery, SessionExpired, FormMismatch, QueryFailure):
        raise
    except Exception as exc:
        raise QueryFailure(f'{name}失败 ({type(exc).__name__})') from None
    finally:
        LOG.info('阶段结束: %s，耗时 %.2f 秒', name, time.monotonic() - started)


class HybridMonitor:
    def __init__(self):
        self.playwright = self.client = None
        self.last_document = None
        self.bootstrap_count = 0
        self.output_dir = None
        self.structure_files = []
        self.structure_error = None

    def start(self):
        if self.playwright is None:
            os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / '.browsers'))
            os.environ.setdefault('NODE_EXTRA_CA_CERTS', str(ROOT / 'certs/rapidssl-tls-rsa-ca-g1.pem'))
            self.playwright = sync_playwright().start()

    def close_client(self):
        if self.client:
            self.client.dispose()
            self.client = None

    def close(self):
        try:
            self.close_client()
        finally:
            if self.playwright:
                self.playwright.stop()
                self.playwright = None

    def bootstrap(self, config):
        self.start()
        self.close_client()
        self.bootstrap_count += 1
        with stage('浏览器刷新 HTTP 会话 Cookie'):
            options = launch_options(config)
            LOG.info('建立 Cookie: headless=%s, channel=%s, UA兼容=%s', options['headless'],
                     options.get('channel', 'default'), config['browser'].get('normalize_headless_user_agent', False))
            browser = self.playwright.chromium.launch(**options)
            try:
                context = new_context(browser, config)
                page = context.new_page()
                page.set_default_timeout(config['browser']['timeout_ms'])
                result = open_entry(page, config)
                if result:
                    self.last_document = Document(page.content(), page.url)
                    raise StopQuery(result)
                # Reuse exactly the effective UA and cookies used by this browser session.
                ua = page.evaluate('navigator.userAgent')
                self.client = self.playwright.request.new_context(
                    storage_state=context.storage_state(), user_agent=ua,
                    extra_http_headers={'Accept-Language': 'es-ES,es;q=0.9',
                                        'Accept': 'text/html,application/xhtml+xml'},
                    timeout=config.get('http', {}).get('timeout_ms', 30000))
            finally:
                browser.close()
        LOG.info('浏览器已关闭，后续查询使用 HTTP；Cookie 仅保存在内存')

    def request(self, url, config, fields=None, referer=None):
        url = official_url(url)
        method = 'GET' if fields is None else 'POST'
        headers = {'Referer': referer} if referer else {}
        if fields is not None:
            headers.update({'Origin': ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'})
        for _ in range(6):
            response = self.client.fetch(url, method=method,
                                         data=urlencode(fields) if fields is not None else None,
                                         headers=headers, max_redirects=0)
            code = response.status
            try:
                doc = Document(response.text(), url)
                location = response.headers.get('location')
            finally:
                response.dispose()
            self.last_document = doc
            if self.output_dir is not None:
                try:
                    path = archive_document(doc, config, self.output_dir, method=method,
                                            fields=fields, status=code, redirect=location)
                    if path and path not in self.structure_files:
                        self.structure_files.append(path)
                except Exception as exc:
                    self.structure_error = f'表单结构保存失败 ({type(exc).__name__})'
                    LOG.warning(self.structure_error)
            LOG.info('HTTP %s %s → %d', method, urlsplit(url).path.split(';')[0], code)
            if code in (301, 302, 303, 307, 308) and location:
                url = official_url(urljoin(url, location))
                if code == 303 or (code in (301, 302) and method == 'POST'):
                    method, fields = 'GET', None
                    headers.pop('Content-Type', None)
                    headers.pop('Origin', None)
                continue
            text = normalize(doc.text)
            if code == 429:
                raise StopQuery(Result(Status.BLOCKED, 'HTTP 429：官网限流，等待下一轮，不立即刷新 Cookie'))
            if code >= 500:
                raise StopQuery(Result(Status.ERROR, f'官网服务异常 HTTP {code}，不视为 Cookie 失效'))
            if ('request rejected' in doc.html.lower() or code in (401, 403) or any(s in text for s in (
                    'request rejected', 'access denied', 'sesion ha caducado',
                    'sesion caducada', 'session expired', 'fortigate', 'the requested url was rejected'))):
                raise SessionExpired()
            if code >= 400:
                raise StopQuery(Result(Status.ERROR, f'查询返回 HTTP {code}'))
            if (doc.has('sede') and urlsplit(url).path.split(';')[0] not in
                    ('/icpplustieb/citar', '/icpplustieb/selectSede')):
                raise SessionExpired()
            if not doc.forms and not doc.text.strip() and '/TSPD/' in doc.html:
                raise SessionExpired()
            # Visible verification inputs require a human. Never solve/submit a CAPTCHA.
            challenge = any((e.tag == 'input' and e.attrs.get('type', '').lower() != 'hidden'
                             and 'captcha' in (e.attrs.get('name', '') + e.attrs.get('id', '')).lower())
                            for e in doc.elements)
            if challenge:
                raise StopQuery(Result(Status.BLOCKED, '官网需要人工验证码，HTTP 查询暂停至下一轮'))
            blocked = classify(doc.text, config['detection'])
            if blocked.status == Status.BLOCKED:
                raise StopQuery(blocked)
            return doc
        raise StopQuery(Result(Status.ERROR, '官网重定向次数异常'))

    def submit(self, doc, form, config, overrides=None, endpoint=None):
        if form.attrs.get('method', 'get').lower() != 'post':
            raise FormMismatch('官网表单提交方式发生变化')
        action = urljoin(doc.url, form.attrs.get('action', doc.url))
        if endpoint:
            if urlsplit(action).path.split(';')[0] != '/icpplustieb/' + endpoint:
                raise FormMismatch('官网表单目标发生变化')
        return self.request(action, config, form.fields(overrides), referer=doc.url)

    def terminal(self, doc, config):
        # Entry notices and identity validation failures are never a no-slot result.
        if doc.has('sede') or doc.has('txtIdCitado'):
            return None
        result = classify(doc.text, config['detection'], result_stage=True)
        return result if result.status != Status.UNKNOWN else None

    def query(self, config):
        with stage('HTTP 加载官网入口'):
            doc = self.request(start_url(config), config)
        if not doc.has('sede'):
            raise FormMismatch('HTTP 入口缺少办公室表单，可能仍需浏览器验证')
        with stage('HTTP 选择办公室和业务'):
            form = doc.form_with('sede')
            office, procedure = config['appointment']['office'], config['appointment']['procedure']
            office_field = form.option('sede', value=office)
            # Office selection changes the available procedures server-side.
            if dict(form.fields()).get(office_field[0]) != office:
                doc = self.request(urljoin(doc.url, 'selectSede'), config,
                                   form.fields(dict([office_field])), referer=doc.url)
                form = doc.form_with('sede')
            overrides = dict([form.option('sede', value=office),
                              form.option('tramiteGrupo[0]', value=procedure)])
            doc = self.submit(doc, form, config, overrides, 'acInfo')
        result = self.terminal(doc, config)
        if result:
            return result
        with stage('HTTP 进入无 Cl@ve 身份表单'):
            if not doc.has('btnEntrar'):
                raise FormMismatch('未找到官网无 Cl@ve 入口')
            forms = [f for f in doc.forms if urlsplit(urljoin(doc.url, f.attrs.get('action', ''))).path.split(';')[0]
                     == '/icpplustieb/acEntrada']
            if len(forms) != 1 or dict(forms[0].fields()).get('acceso') != 'N':
                raise FormMismatch('无 Cl@ve 表单标记发生变化')
            doc = self.submit(doc, forms[0], config, endpoint='acEntrada')
        result = self.terminal(doc, config)
        if result:
            return result
        with stage('HTTP 提交身份查询'):
            form = doc.form_with('txtIdCitado')
            c = config['identity']
            radios = {'NIE': 'rdbTipoDocNie', 'DNI': 'rdbTipoDocDni', 'PASSPORT': 'rdbTipoDocPas'}
            radio = form.control(radios[c['document_type']])
            overrides = {radio.attrs['name']: radio.attrs['value'],
                         form.control('txtIdCitado').attrs['name']: required_env(c['document_env']),
                         form.control('txtDesCitado').attrs['name']: required_env(c['name_env'])}
            overrides.update([form.option('txtPaisNac', label=required_env(c['nationality_env']))])
            doc = self.submit(doc, form, config, overrides, 'acValidarEntrada')
        result = self.terminal(doc, config)
        if result:
            return result
        # Follow only a declarative, unambiguous request form, never arbitrary JS.
        for form in doc.forms:
            action = urljoin(doc.url, form.attrs.get('action', ''))
            buttons = [c for c in form.controls if c.tag == 'button' or c.attrs.get('type') in ('button', 'submit')]
            if (urlsplit(action).path.split(';')[0] == '/icpplustieb/acCitar' and
                    any(normalize(c.attrs.get('value', c.text)) == normalize(config['flow']['request_button_name'])
                        for c in buttons)):
                with stage('HTTP 请求号源结果'):
                    doc = self.submit(doc, form, config, endpoint='acCitar')
                result = self.terminal(doc, config)
                if result:
                    return result
                break
        return Result(Status.REVIEW, 'HTTP 已进入未验证的后续页面，请核实号源；未选择日期或提交预约')

    def check(self, config, output_dir):
        self.last_document = None
        self.output_dir = output_dir
        self.structure_files = []
        self.structure_error = None
        try:
            if self.client is None:
                self.bootstrap(config)
            else:
                LOG.info('复用现有 HTTP 会话 Cookie，本轮无需打开官网浏览器')
            for attempt in range(2):
                try:
                    result = self.query(config)
                    break
                except SessionExpired:
                    self.close_client()
                    if attempt:
                        result = Result(Status.BLOCKED, '刷新 Cookie 后 HTTP 仍被拒绝；本轮停止，等待下一轮')
                        break
                    LOG.warning('HTTP 会话失效或被拒绝，重新打开浏览器刷新 Cookie（本轮最多一次）')
                    self.bootstrap(config)
        except StopQuery as exc:
            result = exc.result
        except FormMismatch as exc:
            result = Result(Status.REVIEW, str(exc))
        except Exception as exc:
            result = Result(Status.ERROR, str(exc) if isinstance(exc, QueryFailure)
                            else f'HTTP 查询失败 ({type(exc).__name__})')
        result = replace(result, transport='hybrid_http', structure_files=tuple(self.structure_files),
                         structure_error=self.structure_error)
        if self.last_document is not None:
            return capture_http_change(self.last_document, config, result, output_dir, self.playwright)
        return result


_monitor = None


def session(config, output_dir=Path('runtime')):
    global _monitor
    if _monitor is None:
        _monitor = HybridMonitor()
        atexit.register(_monitor.close)
    return _monitor.check(config, output_dir)
