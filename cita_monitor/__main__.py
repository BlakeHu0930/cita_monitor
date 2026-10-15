import argparse
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import random
import time
import tomllib

from dotenv import load_dotenv

from .core import Result, State, Status, start_url, target_key, validate
from .notify import deliver, validate_notifications

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("cita")


def check_with_retries(config):
    from .browser import QueryFailure
    if config['monitor'].get('transport', 'browser') == 'hybrid_http':
        from .hybrid import session
    else:
        from .browser import session
    m = config["monitor"]
    for attempt in range(m["attempts"]):
        LOG.info("开始查询，第 %d/%d 次尝试", attempt + 1, m["attempts"])
        started = time.monotonic()
        try:
            result = session(config, output_dir=ROOT / "runtime")
        except Exception as exc:
            # Playwright 的异常常含已填写字段，不把原始消息写入日志。
            detail = str(exc) if isinstance(exc, QueryFailure) else f"浏览器或页面操作失败 ({type(exc).__name__})"
            result = Result(Status.ERROR, detail)
        LOG.info("本次查询结果 %s: %s", result.status, result.detail)
        with (ROOT / "runtime" / "checks.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps({"checked_at": datetime.now().astimezone().isoformat(),
                                      "attempt": attempt + 1, "status": result.status.value,
                                      "duration_seconds": round(time.monotonic() - started, 3),
                                      "page_changed": result.page_changed, "screenshot": result.screenshot,
                                      "transport": result.transport,
                                      "structure_files": result.structure_files,
                                      "structure_error": result.structure_error,
                                      "detail": result.detail}, ensure_ascii=False) + "\n")
        if result.status != Status.ERROR or attempt + 1 == m["attempts"]:
            return result
        delay = min(m["retry_max_seconds"], m["retry_base_seconds"] * 2 ** attempt)
        LOG.warning("第 %d 次尝试失败，%.0f 秒后重试", attempt + 1, delay)
        time.sleep(delay + random.uniform(0, m["jitter_seconds"]))


def run(config, once, state, observe=False):
    target = target_key(config)
    failures = 0
    while True:
        result = check_with_retries(config)
        LOG.info("%s: %s", result.status, result.detail)
        healthy = result.status in (Status.AVAILABLE, Status.UNAVAILABLE)
        failures = 0 if healthy else failures + 1
        pending_key = f'pending-page:{target}'
        if result.page_changed and result.fingerprint:
            state.data[pending_key] = result.fingerprint
            state.save()
        pending_change = bool(result.fingerprint) and state.data.get(pending_key) == result.fingerprint
        subject = None
        if result.status == Status.AVAILABLE:
            subject = "CITA 发现可选预约，请立即查看"
        elif result.status == Status.REVIEW:
            subject = "CITA 进入后续预约页面，请立即核实号源"
        elif observe and result.status == Status.UNKNOWN:
            subject = "CITA 页面变化，需要人工核实（未确认有号）"
        elif (config["notifications"]["health_alerts"] and
              (result.status == Status.BLOCKED or failures >= config["monitor"]["health_alert_after"])):
            subject = "CITA 监测异常，需要检查"
        if not subject and pending_change:
            subject = "CITA 页面变化：当前仍无号" if result.status == Status.UNAVAILABLE else "CITA 页面变化，请核实"
        ok = True
        if subject:
            body = (f"{result.detail}\n业务: {config['appointment']['procedure']}\n"
                    f"办公室: {config['appointment']['office']}\n"
                    f"检测时间: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
                    f"{start_url(config)}\n仅监测，未保留或提交预约。")
            if result.screenshot:
                body += f"\n附有已遮盖身份字段的{result.screenshot_kind}：{Path(result.screenshot).name}（该页面最近一次保存）"
            if result.screenshot_error:
                body += f"\n{result.screenshot_error}，本邮件未附截图。"
            if result.structure_files:
                body += f"\n本轮 {len(result.structure_files)} 份脱敏表单/请求结构已保存在监测电脑 runtime/forms/，供后续联调；不代表已确认有号。"
            if result.structure_error:
                body += f"\n{result.structure_error}，号源提醒仍正常发送。"
            event = result.status.value + (':' + result.fingerprint if result.fingerprint else '')
            ok = deliver(config, state, target, event, subject, body, attachment=result.screenshot)
            if ok and pending_change:
                state.data.pop(pending_key, None)
                state.save()
        status_path = ROOT / "runtime" / "status.json"
        temp = status_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"status": result.status.value, "detail": result.detail,
                                   "checked_at": datetime.now().astimezone().isoformat(),
                                   "mode": "observe" if observe else "verified", "pid": os.getpid(),
                                   "page_changed": result.page_changed, "screenshot": result.screenshot,
                                   "transport": result.transport,
                                   "structure_files": result.structure_files,
                                   "structure_error": result.structure_error,
                                   "consecutive_failures": failures, "notification_ok": ok}, ensure_ascii=False))
        temp.replace(status_path)
        if once:
            return 0 if healthy and ok else 2
        m = config["monitor"]
        delay = m["interval_seconds"] if healthy else max(m["interval_seconds"], m["failure_interval_seconds"])
        LOG.info("下次检查约 %.0f 秒后", delay)
        time.sleep(delay + random.uniform(0, m["jitter_seconds"]))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Barcelona CITA 号源监测（不自动预约）")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--observe", action="store_true", help="观察未完全验证的流程；新页面立即发待核实邮件")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="检查一次，仍可发送真实通知")
    group.add_argument("--list-options", action="store_true", help="仅读取官网业务/地点选项")
    group.add_argument("--inspect", action="store_true", help="有界面手动联调，不发送通知")
    group.add_argument("--test-notification", action="store_true", help="向配置收件人发送测试消息")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(runtime / "monitor.log", mode="a", encoding="utf-8")])
    lock = (runtime / "monitor.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(2, "已有监测进程运行，避免重复检查和通知。\n")
    mode = "list" if args.list_options else "inspect" if args.inspect else "notify" if args.test_notification else "check"
    if args.observe:
        if mode != "check":
            parser.error("--observe 仅与默认持续检查或 --once 搭配")
        mode = "observe"
    try:
        config = tomllib.loads(args.config.read_text())
        validate(config, mode)
        if mode in ("notify", "check", "observe"):
            validate_notifications(config)
        if mode == "observe":
            # Observation mode never reports confirmed availability using unverified selectors.
            config["detection"]["available_selectors"] = []
        state = State(runtime / "notifications.json")
        if mode in ("list", "inspect"):
            from .browser import session
            result = session(config, mode, runtime)
            if isinstance(result, Result):
                print(f"{result.status}: {result.detail}")
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if mode == "notify":
            if not any(config["notifications"][k] for k in ("email", "whatsapp")):
                raise ValueError("至少启用一种通知方式")
            return 0 if deliver(config, state, target_key(config), "test", "CITA 通知测试",
                                "这是测试消息，不代表发现号源。", force=True) else 2
        LOG.info('监测传输方式: %s', config['monitor'].get('transport', 'browser'))
        return run(config, args.once, state, observe=args.observe)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        LOG.error("配置错误: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("监测已停止")
        return 0
    except Exception as exc:
        LOG.error("运行失败 (%s)，可用 --inspect 检查页面", type(exc).__name__)
        return 2
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
