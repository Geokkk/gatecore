# 面板挂域名（可选加装）

gatecore 的管理面板默认只听回环地址（127.0.0.1:18787），平时用 SSH 隧道打开。
本脚本用 nginx 把它反代到一个域名上（https），部署结束直接打印面板网址，
适合想在浏览器里随时打开面板的场景。

## 用法

```bash
# 交互（会问域名，其余回车默认）
sudo bash extras/panel/install-panel.sh

# 非交互
sudo PANEL_DOMAIN=panel.example.com bash extras/panel/install-panel.sh
sudo PANEL_DOMAIN=panel.example.com CERT_MODE=cf CF_Token=<token> bash extras/panel/install-panel.sh

# 也可以在总向导里走到第 3 步
sudo bash deploy.sh
```

## 证书（CERT_MODE）

- `auto`（默认）：先检查域名路线装好的证书（`/usr/local/etc/xray/tls`）是否覆盖面板域名，
  覆盖就直接复用（若由 acme.sh 管理，会顺带把续期动作补上 nginx reload）；
  不覆盖则按 `http` 方式新签一张，存到 `/usr/local/etc/gatecore-panel/tls`。
- `http`：Let's Encrypt HTTP 验证。域名 A 记录须指向本机、80 端口放行；
  签发时脚本会临时停一下 nginx（standalone 需要独占 80）。
- `cf`：Cloudflare DNS 验证，需要 `CF_Token`（只需该 Zone 的 DNS 编辑权限）。
- `self`：自签证书，浏览器会警告，仅临时用。

Let's Encrypt 证书由 acme.sh 每日自动续期，续期后自动 reload nginx。

## 产物

- nginx 站点：`/etc/nginx/conf.d/gatecore-panel.conf`（监听 `PANEL_PORT`，默认 8443）
- 最后打印：`https://<域名>:8443/<面板秘密路径>/` 与面板账号密码，并自检一次
  （返回 200 或 401 都算通——401 是面板在要求登录）

## 注意

- 本机防火墙/云安全组要放行面板端口（默认 8443/TCP）。
- 面板网址里的秘密路径不要外传；它和账号密码是面板的两道门。
- 卸载：删 `/etc/nginx/conf.d/gatecore-panel.conf` 后 reload nginx 即可，面板本体不受影响。
