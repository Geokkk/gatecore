# 域名路线（可选）：VMess + WS + TLS

与 `extras/xray/` 的 Reality 路线二选一。适合已经有域名、想要传统 VMess 节点形态的人——作者现网用的就是这条路线。

| | Reality 路线（extras/xray） | 域名路线（本目录） |
|---|---|---|
| 域名 | 不需要 | **需要**，A 记录指向本机 |
| 证书 | 不需要 | Let's Encrypt，脚本自动签发 + 自动续期 |
| 入站 | VLESS + Reality + Vision，443 | VMess + WS + TLS，443 |
| 前置放行 | 443 | 80（签证书）+ 443 |
| 上手难度 | 更低 | 多一步域名解析 |

## 前置条件

1. 已装 gatecore：`sudo bash ../../install.sh`
2. 已跑过一次 `extras/xray/install-xray.sh`（它负责下载校验过的 Xray 二进制并装好服务单元；本脚本复用它们，只替换配置）
3. 一个域名，其 A 记录已指向本机；防火墙/安全组放行 80、443

## 安装

```bash
sudo DOMAIN=proxy.example.com bash install-domain.sh
```

脚本会：用 acme.sh（standalone）签发证书并装好每日自动续期 → 随机生成 UUID 与 WS 路径 → 写 Xray 配置（出站自动指向 gatecore 网关）→ 重启服务 → 打印 `vmess://` 客户端链接，并把订阅内容（base64）存到 `/usr/local/etc/xray/subscribe.txt`。

> 与 Reality 路线共用同一份 Xray 配置：运行本脚本会替换它，旧配置自动备份为 `config.json.bak.*`。想切回去，重跑 `extras/xray/install-xray.sh` 即可（注意 UUID/密钥会重新生成，客户端链接随之变化）。

## 订阅分发（可选）

想像作者现网那样用一条 https 订阅链接导入，把 `nginx-subscribe.conf.example` 按注释改好放进 nginx 即可：订阅地址形如 `https://你的域名:18443/s/<你自定的随机串>`，路径里的随机串相当于密码，别外传。

## 排障

- 签证书失败：先查域名解析是否已生效（`dig +short 你的域名` 应返回本机 IP）、80 端口是否放行且未被占用。
- 客户端连不上：443 是否放行；gatecore 面板里是否已连上节点（没连节点时入站通、出站不通）。
- 证书续期：acme.sh 每日 03:24 自动检查，续期后自动重启 xray 生效；手动续期 `"/root/.acme.sh"/acme.sh --cron --home "/root/.acme.sh"`。
