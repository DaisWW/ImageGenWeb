# HTTPS 反向代理

Chrome 对通过 HTTP 下载 ZIP 的请求可能显示“已阻止不安全的下载”。应用的下载接口不需要改写，访问入口必须改为 HTTPS。

## 内网部署

项目提供一个默认关闭的 Caddy Compose profile。先在 `.env` 中设置：

```env
IMAGEGEN_HTTPS_ENABLED=true
IMAGEGEN_HTTPS_HOST=xjbkw.weiw
IMAGEGEN_HTTPS_BIND_HOST=0.0.0.0
IMAGEGEN_HTTPS_PORT=18443
IMAGEGEN_CADDY_TLS=tls internal
COOKIE_SECURE=true
TRUST_PROXY_HEADERS=true
```

然后运行：

```powershell
.\deploy-docker.ps1 -Lan
```

访问 `https://xjbkw.weiw:18443`。内网模式的 Caddy 根证书需要加入每台客户端的受信任根证书：

```powershell
docker compose --profile lucida --profile https cp proxy:/data/caddy/pki/authorities/local/root.crt .\caddy-root.crt
Import-Certificate -FilePath .\caddy-root.crt -CertStoreLocation Cert:\CurrentUser\Root
```

如果 `18443` 被占用，可改为其他宿主机端口。启用 HTTPS profile 后，应用的 `18081` 只绑定本机，局域网请求应使用 HTTPS 地址。

## 公网域名

将 `IMAGEGEN_HTTPS_HOST` 改为已解析到服务器的公网域名，并把 `IMAGEGEN_CADDY_TLS` 留空：

```env
IMAGEGEN_HTTPS_HOST=studio.example.com
IMAGEGEN_HTTPS_BIND_HOST=0.0.0.0
IMAGEGEN_HTTPS_PORT=443
IMAGEGEN_CADDY_TLS=
```

Caddy 会通过 ACME 申请公开可信证书；公网 DNS 和 443 端口必须正确指向这台机器。运行部署脚本时使用 `-Lan`，让 HTTPS 代理绑定局域网/公网网卡；同时按实际公网防火墙策略开放 443 端口。
