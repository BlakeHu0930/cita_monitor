"""Compare terminal pages and retain timestamped, masked screenshots."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from urllib.parse import urlsplit

from .core import normalize, target_key


def private_values(config):
    names = [config['identity'][k] for k in ('document_env', 'name_env')]
    names += [config['smtp'][k] for k in ('username_env', 'sender_env', 'recipient_env')]
    values = []
    for name in names:
        values.extend(v.strip() for v in os.environ.get(name, '').split(','))
    return [v for v in values if len(v) >= 4]


def page_fingerprint(path, status, text, secrets=()):
    # Operation IDs and their timestamps change on every identical no-slots response.
    text = re.sub(r'(?im)^\s*cod\.?\s*oper\.?[^\n]*', '', text)
    for value in secrets:
        text = re.sub(re.escape(value), '[private]', text, flags=re.I)
    path = urlsplit(path).path.split(';')[0]
    return hashlib.sha256(f'{status}\n{path}\n{normalize(text)}'.encode()).hexdigest()


def capture_change(page, config, result, output_dir, *, fingerprint_override=None, state_prefix='page'):
    if not config['browser'].get('capture_changes', True):
        return result
    try:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        key = target_key(config)
        state_path = output_dir / f'{state_prefix}-{key}.json'
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        content = page.locator('#wrap')
        if not content.count():
            content = page.locator('body')
        secrets = private_values(config)
        fingerprint = fingerprint_override or page_fingerprint(page.url, result.status, content.inner_text(timeout=5000), secrets)
        changed = bool(previous) and previous.get('fingerprint') != fingerprint
        screenshot = previous.get('screenshot')
        # Capture baseline as well. Reuse the last image for unchanged repeated alerts.
        if not previous or changed or not screenshot or not Path(screenshot).exists():
            folder = output_dir / 'screenshots'
            folder.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
            screenshot = str((folder / f'{stamp}_{result.status}_{uuid4().hex[:8]}.png').resolve())
            # Mark elements holding identity text, in addition to masking input values.
            page.evaluate('''values => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                const nodes = [];
                while (walker.nextNode()) nodes.push(walker.currentNode);
                for (const n of nodes) {
                    const p = n.parentElement;
                    if (!p || ['SCRIPT','STYLE'].includes(p.tagName)) continue;
                    const t = n.textContent.toLowerCase();
                    if (values.some(v => t.includes(v.toLowerCase())) ||
                        /[xyz]\\d{7}[a-z]|[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/i.test(t))
                        p.setAttribute('data-cita-private', '');
                }
            }''', secrets)
            masks = page.locator('input:not([type="hidden"]):not([type="button"]):not([type="submit"]), textarea, select, [data-cita-private]')
            page.screenshot(path=screenshot, full_page=True, mask=[masks], timeout=10000)
            Path(screenshot).chmod(0o600)
        temp = state_path.with_suffix('.tmp')
        temp.write_text(json.dumps({'fingerprint': fingerprint, 'screenshot': screenshot}))
        temp.chmod(0o600)
        temp.replace(state_path)
        return replace(result, screenshot=screenshot, fingerprint=fingerprint, page_changed=changed)
    except Exception as exc:
        # Do not leak browser exceptions or send an unmasked fallback image.
        return replace(result, screenshot_error=f'截图未生成 ({type(exc).__name__})')


def capture_http_change(document, config, result, output_dir, playwright):
    """Render the actual HTTP response offline only when a new image is needed."""
    if not config['browser'].get('capture_changes', True):
        return result
    result = replace(result, screenshot_kind='HTTP 响应离线渲染截图（未执行页面脚本）')
    try:
        fingerprint = page_fingerprint(document.url, result.status, document.text, private_values(config))
        state_path = Path(output_dir) / f'http-page-{target_key(config)}.json'
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        screenshot = previous.get('screenshot')
        if previous.get('fingerprint') == fingerprint and screenshot and Path(screenshot).is_file():
            return replace(result, screenshot=screenshot, fingerprint=fingerprint)
        # No requests to the site: rendering must neither refresh the session nor
        # execute scripts, meta refreshes, external assets or form submissions.
        html = re.sub(r'<meta\b[^>]*>', '', document.html, flags=re.I)
        html = re.sub(r'<noscript\b[^>]*>.*?</noscript\s*>', '', html, flags=re.I | re.S)
        html = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
                "style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'\">" + html)
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(java_script_enabled=False, offline=True)
            context.route('**/*', lambda route: route.abort())
            page = context.new_page()
            page.set_content(html, wait_until='domcontentloaded', timeout=10000)
            return capture_change(page, config, result, output_dir,
                                  fingerprint_override=fingerprint, state_prefix='http-page')
        finally:
            browser.close()
    except Exception as exc:
        return replace(result, screenshot_error=f'HTTP 截图未生成 ({type(exc).__name__})')
