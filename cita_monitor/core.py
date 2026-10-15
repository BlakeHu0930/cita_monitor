from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import time
import unicodedata
from urllib.parse import urlencode


class Status(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    ERROR = "error"
    REVIEW = "review"


@dataclass(frozen=True)
class Result:
    status: Status
    detail: str
    screenshot: str | None = None
    fingerprint: str | None = None
    page_changed: bool = False
    screenshot_error: str | None = None
    transport: str = 'browser'
    screenshot_kind: str = '浏览器页面截图'
    structure_files: tuple[str, ...] = ()
    structure_error: str | None = None


def normalize(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def classify(text, rules, *, result_stage=False, slot_visible=False, challenge=False):
    normalized = normalize(text)
    if challenge or any(normalize(t) in normalized for t in rules["blocked_texts"]):
        return Result(Status.BLOCKED, "网站拦截、会话过期或需要人工验证")
    if not result_stage:
        return Result(Status.UNKNOWN, "尚未进入号源查询结果阶段")
    if any(normalize(t) in normalized for t in rules["unavailable_texts"]):
        return Result(Status.UNAVAILABLE, "查询结果明确显示暂无可用预约")
    if slot_visible:
        return Result(Status.AVAILABLE, "查询结果出现已配置的可选日期/时间元素")
    return Result(Status.UNKNOWN, "无法确认号源；需要核实页面流程或识别规则")


def start_url(config):
    return "https://icp.administracionelectronica.gob.es/icpplustieb/citar?" + urlencode(
        {"p": config["appointment"]["province"], "locale": config["appointment"]["locale"]}
    )


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"请在 .env 设置 {name}")
    return value


def validate(config, mode):
    b = config['browser']
    if not isinstance(b['headless'], bool) or not isinstance(b.get('normalize_headless_user_agent', False), bool):
        raise ValueError('browser.headless 和 normalize_headless_user_agent 必须为布尔值')
    if b.get('channel', '') not in ('', 'chromium', 'chrome', 'msedge'):
        raise ValueError('browser.channel 必须为空、chromium、chrome 或 msedge')
    if config.get('monitor', {}).get('transport', 'browser') not in ('browser', 'hybrid_http'):
        raise ValueError('monitor.transport 必须为 browser 或 hybrid_http')
    if config.get('http', {}).get('timeout_ms', 30000) <= 0:
        raise ValueError('http.timeout_ms 必须大于 0')
    m = config["monitor"]
    for key in ("interval_seconds", "attempts", "retry_base_seconds", "retry_max_seconds",
                "failure_interval_seconds", "notification_cooldown_seconds", "health_alert_after"):
        if m[key] <= 0:
            raise ValueError(f"monitor.{key} 必须大于 0")
    if m["jitter_seconds"] < 0 or not isinstance(m["attempts"], int):
        raise ValueError("jitter_seconds 必须非负，attempts 必须是整数")
    if config["browser"]["timeout_ms"] <= 0 or config["browser"]["result_timeout_seconds"] <= 0:
        raise ValueError("浏览器超时必须大于 0")
    if mode in ("list", "notify"):
        return
    if not config["appointment"]["procedure"]:
        raise ValueError("请先填写 appointment.procedure（可运行 --list-options）")
    if mode == "inspect":
        return
    if config['monitor'].get('transport', 'browser') == 'hybrid_http' and mode != 'observe':
        raise ValueError('hybrid_http 当前只支持 --observe：后续有号页面尚未验证')
    if mode != "observe" and (not config["flow"]["verified"] or not config["detection"]["available_selectors"]):
        raise ValueError("请先 --inspect 联调结果页，填写 available_selectors 并设置 flow.verified=true")
    if not any(config["notifications"][k] for k in ("email", "whatsapp")):
        raise ValueError("至少启用一种通知方式")
    for key in ("document_env", "name_env", "nationality_env"):
        required_env(config["identity"][key])


def target_key(config):
    data = {k: config[k] for k in ("appointment", "flow", "detection")}
    # 身份不同的监测任务不要共用去重状态；只存哈希，不存身份证件。
    data["identity"] = os.environ.get(config["identity"]["document_env"], "")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def due(self, target, channel, event, cooldown, now=None):
        now = time.time() if now is None else now
        stamp = self.data.get(f"{target}:{channel}:{event}")
        return stamp is None or now - stamp >= cooldown

    def sent(self, target, channel, event, now=None):
        self.data[f"{target}:{channel}:{event}"] = time.time() if now is None else now
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2))
        temp.chmod(0o600)
        temp.replace(self.path)
