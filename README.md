# Barcelona CITA 监测

Python 3.11+ / Playwright，针对巴塞罗那省按指纹预约。仅查询和发邮件，不选择号源或提交预约。

## 当前运行方式（2026-09-20）

`monitor.transport="hybrid_http"`：首次用无头 Chromium 打开官网建立会话，取出 Cookie 和此次会话使用的 User-Agent 后关闭浏览器；后续使用 Playwright 的 HTTP 客户端执行查询。Cookie 在同一个后台进程内跨轮复用，HTTP 响应中的 Cookie 更新由客户端接收，不写入磁盘。重启后重新建立会话。

无头配置为 `browser.headless=true`、`channel="chromium"`、`normalize_headless_user_agent=true`。读取安装的浏览器实际 UA，仅将 `HeadlessChrome/` 替换为 `Chrome/`，保留实际版本和平台；HTTP 客户端沿用相同 UA。未修改 webdriver、Canvas/WebGL 等特征，未安装额外 stealth 插件。`channel="chromium"` 选择完整 Chromium 的新版无头实现，参考 [Playwright 浏览器文档](https://playwright.dev/python/docs/browsers)。

实测默认无头和未调整 UA 的新版无头均被入口拦截，Cookie 为 0；调整 UA 后进入真实入口、取得 13 个 Cookie，并连续两轮完成 HTTP 无号查询。正式代码在另一独立会话中也连续两轮成功，只建立一次浏览器会话。这说明当前配置可用，不保证以后不被识别；遇到拒绝仍按既有刷新次数上限与退避处理。`--inspect` 仍显式打开可见浏览器进行人工联调。

已实测 HTTP 查询路径：入口 → 业务说明 → **无 Cl@ve** 身份表单（`acceso=N`）→ 身份提交 → 官网明确返回暂无预约。连续两轮实测只建立一次浏览器会话，第二轮四个 HTTP 请求约 0.8 秒；这不是稳定延迟保证。当前条件为巴塞罗那省 `8`、按指纹 `4010`、任意办公室 `99`。

- HTTP 401/403、Request Rejected、会话过期或业务页退回入口：本轮最多重新打开浏览器刷新一次 Cookie，并从入口重查；再次被拒绝则报告 blocked。
- HTTP 429、验证码不立即刷新 Cookie；服务器 5xx、网络超时走失败重试。刷新 Cookie 不保证解除网站防护或服务器故障。
- 每次提交解析刚收到的表单，保留动态隐藏字段；只允许官方域名已核实的查询路径，不允许最终预约提交或跨域转发。
- 当前使用 `--observe`。确认无号可以保持安静；进入未验证的后续页面会发“待核实”邮件。日历、可选时段、联系方式及预约确认流程尚未验证，不能将缺少无号提示等同于有号。
- HTTP 模式不执行 `flow.steps` 或任意页面 JavaScript，也不使用浏览器 CSS 选择器宣称确认有号。遇到未知流程通知人工核实。

```toml
[monitor]
transport = "hybrid_http" # 改为 browser 可使用原来的全浏览器流程
interval_seconds = 600
jitter_seconds = 0

[http]
timeout_ms = 30000
```

上方是相关配置项说明，不要用这段替换完整配置。编辑现有 `config.toml` 后重启进程生效。

## 截图与邮件

`browser.capture_changes=true` 时，对每轮最终页面的文字、路径和查询状态做比较，忽略操作编号及时间戳。首次保存基线；相同页面复用最近截图；变化时新建带 UTC 时间和随机后缀的 PNG，保存在 `runtime/screenshots/`，不覆盖旧图片。

HTTP 模式用响应 HTML **离线渲染**截图：只在需要新图时启动独立的无头浏览器，禁用网页脚本和外部网络访问。没有外部样式时画面会更朴素，邮件明确注明截图来源。这个浏览器不访问官网、不刷新会话。原浏览器模式直接截取业务页面；两种模式的页面基线分别保存，避免切换造成误报。

## 后续预约表单结构留存

`http.capture_forms=true`（默认）会在每次收到 HTTP 响应时，先保存新的脱敏结构，再判断结果或进入下一步，避免遗漏中间表单。记录不限于已确认有号；待核实页和验证码页同样留存，仍然不会自动提交预约。

文件在 `runtime/forms/<任务标识>/`：

- 记录页面地址、已实际执行的请求方法/参数名/响应码，重定向目标。
- 记录表单 action/method/enctype、字段 id/name/type、必填/长度/格式限制、选项，以及按钮和页面事件中引用的函数名。
- 保留已知办公室、日期、时间等公开业务选项值；姓名、证件、电话等身份字段值、Cookie、隐藏令牌和未知字段值不保存。动态隐藏字段的名称保留，但值必须以后重新从当前会话取得。
- 记录公开脚本地址和静态发现的业务路径引用，不保存原始脚本/HTML。页面声明的接口会标记为未实测，不能据此认定已验证预约 API；由 JavaScript 动态生成的字段可能不在 HTTP 响应中。
- 相同结构复用既有文件，变化时新建带时间的 JSON；`index.jsonl` 追加索引，不覆盖历史。动态 UUID 字段名和被隐藏的令牌值变化不重复存档。

提醒邮件会说明本轮结构已在本机保存；`status.json` 和 `checks.jsonl` 的 `structure_files` 可定位文件。结构保存失败不会阻断提醒，邮件会注明失败原因。

输入框、选择框及检测到的本地姓名、证件号、邮箱文本会被遮盖。截图失败不会发送未遮盖的备用图；邮件正文说明失败。邮件带 PNG 附件，注明最近保存的文件名。首次无号基线不发邮件，后续变化（包括恢复为无号）会提醒。比较不包含仅样式或像素变化，也不是逐步录像。

## 运行

```sh
.venv/bin/python -m cita_monitor --observe --once
.venv/bin/python -m cita_monitor --observe
```

当前后台进程号在 `runtime/monitor.pid`；检查 `runtime/status.json` 的 `checked_at`、`status`、`transport`，以及 `runtime/monitor.log`。持有同一工作区的文件锁防止重复监测。停止旧进程后再启动；进程号文件本身不保证进程仍存活。

每轮结束后约 600 秒再检查；失败轮次至少 900 秒。网络错误最多 3 次尝试，间隔 30、60 秒。持续错误发健康告警；同一内容的提醒冷却 30 分钟，邮件失败下轮重试。SMTP 接受不等于邮件已送达或阅读。

`monitor.log` 和每次尝试的 `checks.jsonl` 始终追加写入，重启不清空、不自动轮转或删除。`status.json` 只记录最新快照。Cookie、隐藏表单密钥、身份值和 SMTP 密码不写日志；`.env`、`config.toml`、`runtime/` 不进 Git。可选 `save_diagnostics` 是原浏览器模式的旧诊断功能，默认关闭，启用会覆盖 `latest.html/latest.png`，与历史截图无关。

本机后台进程没有安装开机自启。锁屏通常可以继续；休眠、合盖导致的睡眠、断网或退出进程会中断监测。`caffeinate -i` 仅阻止空闲睡眠，不能保证合盖继续运行。

## 安装与配置

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" .venv/bin/python -m playwright install chromium
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env config.toml
```

已填写的本地文件不要覆盖。身份在 `.env` 中配置，国籍使用官网选项原文；当前按指纹表单已验证 NIE。SMTP 支持 465 SSL 和 587 STARTTLS，收件人可用逗号分隔。开启 `notifications.email=true`；应用专用密码只填本地 `.env`。

```sh
# 真实发送测试邮件给配置收件人
.venv/bin/python -m cita_monitor --test-notification
# 这两个命令仍用浏览器，只读取选项或手动联调
.venv/bin/python -m cita_monitor --list-options
.venv/bin/python -m cita_monitor --inspect
```

官网证书链补充说明见 [TLS 诊断](docs/tls-fix.md)。HTTP 模式加载经过验证的公共中间证书，保持 TLS 验证，不使用忽略证书错误选项。

如改回 `monitor.transport="browser"`，原来的手动联调、`flow.steps` 与验证后的 CSS 可用时段选择器仍可使用。HTTP 模式目前只允许 `--observe`，避免把未验证流程标成精确监测。

## 测试与调查

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖表单隐藏字段、无 Cl@ve 查询、Cookie 复用/刷新上限、限流及超时、不把拦截和首页公告当成无号、禁止跨域及预约提交路径、截图、邮件附件和通知重试。测试使用模拟官网，不发送测试身份或真实邮件。

`python -m cita_monitor.http_probe` 保留为独立公开表单诊断，只访问入口和提交业务选择，不读取身份；报告追加至 `runtime/http-probe.jsonl`。正式混合查询实现见 `cita_monitor/hybrid.py`，历史调查见 [HTTP 调查](docs/transport-investigation.md) 与 [教程核对](docs/tutorial-review.md)。
