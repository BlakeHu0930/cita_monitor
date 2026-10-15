import os
import logging
from contextlib import contextmanager
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from .core import Result, Status, classify, required_env, start_url
from .snapshots import capture_change
from .browser_settings import launch_options, new_context

HOST = "icp.administracionelectronica.gob.es"
LOG = logging.getLogger(__name__)


class QueryFailure(RuntimeError):
    """Only safe stage names and exception types; never form values or cookies."""


@contextmanager
def stage(name):
    started = time.monotonic()
    LOG.info("阶段开始: %s", name)
    try:
        yield
    except QueryFailure:
        raise
    except Exception as exc:
        raise QueryFailure(f"{name}失败 ({type(exc).__name__})") from None
    finally:
        LOG.info("阶段结束: %s，耗时 %.2f 秒", name, time.monotonic() - started)


def ensure_official(page):
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != HOST:
        raise RuntimeError("页面离开官方预约域名，已停止表单操作")


def assess(page, config, result_stage=False):
    text = page.locator("body").inner_text()
    # 只认可见挑战，不因页面加载了隐藏的 reCAPTCHA 库而误判。
    challenge = page.locator(
        'iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible, '
        'input[name*="captcha"]:visible, #challenge-running:visible'
    ).count() > 0
    slots = False
    if result_stage:
        for selector in config["detection"]["available_selectors"]:
            for element in page.locator(selector).all():
                if element.is_visible() and element.is_enabled():
                    slots = True
                    break
    return classify(text, config["detection"], result_stage=result_stage,
                    slot_visible=slots, challenge=challenge)


def progress_result(page):
    # Observed in the user-supplied tutorial; these stages are NOT proof of slots.
    path = urlsplit(page.url).path.split(";")[0]
    if path.endswith(("/acVerFormulario", "/acOfertarCita")):
        return Result(Status.REVIEW, "进入联系方式或日期选择页面；可能有号，尚未确认，请人工查看")
    return Result(Status.UNKNOWN, "页面与已验证流程不一致；请人工核实，不代表已有号源")


def open_entry(page, config):
    response = page.goto(start_url(config), wait_until="domcontentloaded")
    if response and response.status >= 400:
        status = Status.BLOCKED if response.status in (403, 429) else Status.ERROR
        if response.status == 403:
            body = page.locator("body").inner_text()
            if "FortiGate" in body and "Intrusion Prevention" in body:
                return Result(Status.BLOCKED, "HTTP 403：FortiGate 入侵防护拦截，尚未进入预约业务页面")
        return Result(status, f"入口返回 HTTP {response.status}")
    ensure_official(page)
    result = assess(page, config)
    if result.status == Status.BLOCKED:
        return result
    page.locator("#sede").wait_for()
    return None


def options(page):
    return page.locator("select").evaluate_all("""els => els.map(el => ({
        id: el.id, options: Array.from(el.options).map(o => ({value:o.value,label:o.text}))
    }))""")


def wait_next_or_result(page, config, target):
    """The service can return a terminal no-slots page at intermediate steps."""
    deadline = time.monotonic() + config["browser"]["timeout_ms"] / 1000
    while True:
        ensure_official(page)
        if target.is_visible():
            return None
        # Never classify the province notice or a still-visible identity form.
        at_form = page.locator('#sede:visible, #txtIdCitado:visible').count() > 0
        result = assess(page, config, result_stage=not at_form)
        if result.status in (Status.UNAVAILABLE, Status.BLOCKED):
            return result
        if time.monotonic() >= deadline:
            return progress_result(page)
        page.wait_for_timeout(500)


def open_identity(page, config):
    with stage("选择办公室和业务"):
        office = page.locator("#sede")
        # selectSede is a full-page POST. Avoid submitting the existing default again.
        if office.input_value() != config["appointment"]["office"]:
            office.select_option(value=config["appointment"]["office"])
        page.locator('[id="tramiteGrupo[0]"]').select_option(value=config["appointment"]["procedure"])
        page.locator("#btnAceptar").click()
        page.wait_for_load_state("domcontentloaded")
    ensure_official(page)
    with stage("等待无Cl@ve入口"):
        result = wait_next_or_result(page, config, page.locator("#btnEntrar"))
    if result:
        return result
    with stage("进入无Cl@ve身份表单"):
        page.locator("#btnEntrar").click()
        result = wait_next_or_result(page, config, page.locator("#txtIdCitado"))
    if result:
        return result
    ensure_official(page)


def query(page, config):
    with stage("加载官网入口"):
        result = open_entry(page, config)
    if result:
        return result
    result = open_identity(page, config)
    if result:
        return result
    c = config["identity"]
    document_types = {"NIE": "#rdbTipoDocNie", "DNI": "#rdbTipoDocDni", "PASSPORT": "#rdbTipoDocPas"}
    if c["document_type"] not in document_types:
        raise ValueError("document_type 必须为 NIE、DNI 或 PASSPORT")
    with stage("填写及提交查询身份"):
        page.locator(document_types[c["document_type"]]).check()
        page.locator("#txtIdCitado").fill(required_env(c["document_env"]))
        page.locator("#txtDesCitado").fill(required_env(c["name_env"]))
        page.locator("#txtPaisNac").select_option(label=required_env(c["nationality_env"]))
        page.locator("#btnEnviar").click()
        page.wait_for_load_state("domcontentloaded")
    ensure_official(page)
    request_button = page.get_by_role("button", name=config["flow"]["request_button_name"], exact=True)
    with stage("等待申请预约按钮"):
        result = wait_next_or_result(page, config, request_button)
    if result:
        return result
    with stage("请求号源结果"):
        request_button.click()
        page.wait_for_load_state("domcontentloaded")
    ensure_official(page)
    # 首页/身份表单绝不能被当成结果页。
    if page.locator('#txtIdCitado:visible, [id="tramiteGrupo[0]"]:visible').count():
        return Result(Status.UNKNOWN, "请求后仍停留在入口或身份表单")
    for step in config["flow"].get("steps", []):
        result = assess(page, config, result_stage=True)
        if result.status != Status.UNKNOWN:
            return result
        ensure_official(page)
        locator = page.locator(step["selector"])
        if step["action"] == "select":
            locator.select_option(value=str(step["value"]))
        elif step["action"] == "click":
            locator.click()
        else:
            raise ValueError("flow.steps 仅支持 select 和 click")
        page.wait_for_load_state("domcontentloaded")
    deadline = time.monotonic() + config["browser"]["result_timeout_seconds"]
    while True:
        ensure_official(page)
        result = assess(page, config, result_stage=True)
        if result.status != Status.UNKNOWN or time.monotonic() >= deadline:
            return progress_result(page) if result.status == Status.UNKNOWN else result
        page.wait_for_timeout(500)


def session(config, mode="check", output_dir=Path("runtime")):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[1] / ".browsers"))
    with sync_playwright() as p:
        headless = False if mode == 'inspect' else config['browser']['headless']
        browser = p.chromium.launch(**launch_options(config, headless=headless))
        context = new_context(browser, config, headless=headless)
        page = context.new_page()
        page.set_default_timeout(config["browser"]["timeout_ms"])
        try:
            if mode in ("list", "inspect"):
                result = open_entry(page, config)
                if result:
                    return result
                if mode == "list":
                    return options(page)
                result = open_identity(page, config)
                if result:
                    return result
                print("已到身份表单。请手动填写并进入号源结果页。不要提交最终预约。")
                print("Playwright Inspector 中可检查日期/时段选择器；完成后点击 Resume。")
                page.pause()
                return assess(page, config, result_stage=False)
            try:
                result = query(page, config)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, QueryFailure) else f"浏览器或页面操作失败 ({type(exc).__name__})"
                result = Result(Status.ERROR, detail)
            return capture_change(page, config, result, output_dir)
        finally:
            if config["browser"]["save_diagnostics"]:
                output_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(output_dir / "latest.png"), full_page=True)
                    (output_dir / "latest.html").write_text(page.content())
                except Exception:
                    pass
            context.close()
            browser.close()
