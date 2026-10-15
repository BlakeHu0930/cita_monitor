# 查询方式分析（2026-09-19）

## 最新进展：已实现 Cookie 复用的 HTTP 查询

2026-09-19 21:02（Europe/Madrid）实测：可见 Chromium 建立入口会话后关闭，使用其 Cookie 和实际 User-Agent 的独立 HTTP 客户端成功完成 `citar → acInfo → acEntrada → acValidarEntrada`，返回明确的暂无预约。连续两轮共建立一次浏览器会话，第二轮四次 HTTP 请求约 0.8 秒。身份值与 Cookie 未写入实验日志。生产实现为 `cita_monitor/hybrid.py`；以下早期调查结论仅代表当时状态。

客户端跨轮保留 Cookie，动态隐藏字段逐页重新解析，确认无 Cl@ve 的 `acceso=N`。401/403、Request Rejected、过期提示或业务页退回入口时，本轮最多刷新一次 Cookie；429、验证码不立即刷新，5xx/超时不当作会话过期。仍属于混合方式，首次或恢复时依赖正常浏览器建立会话。

页面变化时离线渲染 HTTP 响应截图，禁用脚本和网络资源，不访问官网。尚未实测到有号日历或最终预约页面，继续使用观察模式和待核实邮件；不会把无号提示消失直接当作有号。

后续更新：证书失败已定位为本次服务端缺少中间证书；补充经过验证的 RapidSSL 中间证书后，直接请求可通过 TLS 并收到 HTTP 200，但尚未得到真实预约表单。详见 [TLS 修复](tls-fix.md)。下文保留最初调查结果。

## 实测证据

- 当日截至调查时，79 条尝试记录：63 unavailable、12 error、2 blocked、2 unknown。这是每次尝试的统计，包含重试，不等于79轮独立检查。
- 可见 Chromium 进入官网后，观察到 GET `/icpplustieb/citar`，POST `/icpplustieb/selectSede`，POST `/icpplustieb/muestraMensajesTramite`，POST `/icpplustieb/acInfo`。
- 入口是 `portadaForm` POST 表单，含 `sede`、`tramiteGrupo[0]`、`subtramite` 及动态命名的隐藏字段。按钮调用 `envia()`，不是已发现的无状态 JSON 号源接口。
- 正常流程还发起 `/TSPD/` 文档和 XHR 请求。可以判断存在附加防护流程，不能仅凭路径断言具体产品、算法或如何绕过。
- 在同一个浏览器 context 中使用 `context.request.get` 读取公开入口失败，底层报 `unable to verify the first certificate`。独立请求和启用 Node 系统 CA 的测试也失败。保留 TLS 校验，未使用 ignore_https_errors。
- 上述证据并不证明没有可用查询接口，而是说明本次没有验证出一个可安全、可靠替代浏览器的直接请求流程。也未复放动态隐藏字段或绕过验证码。

## BuscaCita

查看 https://www.buscacita.es/ 的公开首页。页面说明用户提交条件、服务代查、成功预约后通知及付款，未公开服务器端代码或官方查询 API。不能凭前端页面推断其后台一定使用 JS 逆向、独立接口或特殊号源。未向其提交用户资料或购买服务。

## 已实施

1. 当办公室下拉框当前已是目标值99时，不再重复 select_option，避免触发一次整页 selectSede POST。
2. 为入口加载、办公室/业务选择、无Cl@ve入口、身份提交、申请按钮和号源请求增加耗时日志。
3. 将安全的阶段名与异常类型写入失败原因，避免所有故障都只显示 TimeoutError；不记录原始异常中的表单值或凭据。
4. checks.jsonl 追加每次尝试的 duration_seconds，日志继续追加保存。

## 当前限制

保持每轮完成后10分钟检查、失败退避、可见浏览器、无Cl@ve、观察模式和邮件提醒。没有启用未经验证的纯HTTP号源检测；有号的精确识别也仍待验证。网络/服务器不可用时浏览器和直接接口都可能失败。合盖睡眠仍会中断本机运行，接口方案不会改变这一点。
