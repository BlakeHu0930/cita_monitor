# TLS 证书链问题定位与修复（2026-09-19）

本次直接握手观察到官网服务器只发送一张叶证书：

- Subject: `*.administracionelectronica.gob.es`
- Issuer: `RapidSSL TLS RSA CA G1`
- 叶证书有效期：2025-09-18 至 2026-10-09。
- AIA 中间证书地址：`http://cacerts.rapidssl.com/RapidSSLTLSRSACAG1.crt`。

OpenSSL 默认验证返回 `unable to get local issuer certificate`。系统 curl 验证成功；Playwright APIRequestContext（Node）失败，启用系统 CA 也失败。证据与服务端漏发中间证书相符；不同客户端可能通过缓存或补全证书链得到不同结果。

从证书明确指定的 AIA 地址获取公开中间证书后，进行了两项验证：

1. 使用 `/opt/homebrew/etc/openssl@3/cert.pem` 中现有可信根验证中间证书：OK。
2. 将中间证书作为 `-untrusted` 链材料，仍使用原可信根，并以 `-verify_hostname icp.administracionelectronica.gob.es` 验证叶证书：OK。

中间证书保存为 `certs/rapidssl-tls-rsa-ca-g1.pem`。这是公开 CA 证书，不含私钥。使用前已验证，不是把任意下载证书直接设为可信。

为需要直接请求的单个进程补充证书：

```sh
NODE_EXTRA_CA_CERTS="$PWD/certs/rapidssl-tls-rsa-ca-g1.pem" .venv/bin/python your_http_script.py
```

`your_http_script.py` 是占位文件名，应换成实际调用 Playwright APIRequestContext 的脚本。变量必须在 Playwright Node 驱动启动之前设置。未修改系统钥匙串、全局环境或运行中的监测进程。

实测该设置消除了证书错误，APIRequestContext 在约 0.1 秒内取得 HTTP 200。但响应未包含预期的 `#sede` 入口表单，**仅能确认 TLS 已通过，不能当作查询号源成功**。浏览器会话、动态表单及网站防护仍需另行验证。

不要使用 `ignore_https_errors=True`、`verify=False` 或 `NODE_TLS_REJECT_UNAUTHORIZED=0` 代替补全证书链。网站换证后需要重新核对实际签发链。
