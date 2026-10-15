import base64
from email.message import EmailMessage
import json
import logging
import smtplib
import ssl
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import required_env

LOG = logging.getLogger(__name__)


def validate_notifications(config):
    for channel in ("email", "whatsapp"):
        if not config["notifications"][channel]:
            continue
        section = config["smtp" if channel == "email" else "whatsapp"]
        for key, value in section.items():
            if key.endswith("_env"):
                required_env(value)
        if channel == "email" and section["security"] not in ("ssl", "starttls"):
            raise ValueError("smtp.security 只能为 ssl 或 starttls")


def email(config, subject, body, attachment=None):
    c = config["smtp"]
    get = lambda k: required_env(c[k + "_env"])
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = get("sender")
    message["To"] = get("recipient")
    message.set_content(body)
    if attachment:
        path = Path(attachment)
        message.add_attachment(path.read_bytes(), maintype='image', subtype='png', filename=path.name)
    context = ssl.create_default_context()
    if c["security"] == "ssl":
        client = smtplib.SMTP_SSL(get("host"), c["port"], timeout=30, context=context)
    else:
        client = smtplib.SMTP(get("host"), c["port"], timeout=30)
    with client:
        if c["security"] == "starttls":
            client.starttls(context=context)
        client.login(get("username"), get("password"))
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("SMTP 未接受全部收件人")


def whatsapp(config, subject, body):
    c = config["whatsapp"]
    get = lambda k: required_env(c[k + "_env"])
    sid = get("account_sid")
    if not sid.startswith("AC") or not sid.isalnum():
        raise ValueError("Twilio Account SID 格式错误")
    payload = urlencode({"From": get("from"), "To": get("to"),
                         "ContentSid": get("content_sid"),
                         "ContentVariables": json.dumps({"1": " ".join((subject + " " + body).split())})}).encode()
    auth = base64.b64encode(f"{sid}:{get('auth_token')}".encode()).decode()
    request = Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                      data=payload, headers={"Authorization": "Basic " + auth})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("sid") or result.get("status") in ("failed", "undelivered"):
        raise RuntimeError("WhatsApp 服务未接受通知")


def deliver(config, state, target, event, subject, body, *, force=False, attachment=None):
    success = True
    for channel, sender in (("email", email), ("whatsapp", whatsapp)):
        if not config["notifications"][channel]:
            continue
        cooldown = config["monitor"]["notification_cooldown_seconds"]
        if not force and not state.due(target, channel, event, cooldown):
            continue
        try:
            if channel == 'email' and attachment:
                sender(config, subject, body, attachment=attachment)
            else:
                sender(config, subject, body)
        except Exception as exc:
            # 服务异常可能包含凭据或收件人，日志不输出原文。
            LOG.error("%s 通知失败 (%s)，下轮重试", channel, type(exc).__name__)
            success = False
        else:
            state.sent(target, channel, event)
            LOG.info("%s 通知已被发送服务接受（不代表已阅读）", channel)
    return success
