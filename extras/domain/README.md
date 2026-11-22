# 域名路线（可选）：VMess + WS + TLS

与 `extras/xray/` 的 Reality 路线二选一。适合已经有域名、想要传统 VMess 节点形态的人——作者现网用的就是这条路线。

| | Reality 路线（extras/xray） | 域名路线（本目录） |
|---|---|---|
| 域名 | 不需要 | **需要**，A 记录指向本机 |
| 证书 | 不需要 | 三种模式：LE 证书（HTTP 验证 / CF DNS 验证，自动续期）或自签证书 |
| 入站 | VLESS + Reality + Vision，443 | VMess + WS + TLS，443 |
| 前置放行 | 443 | 80（签证书）+ 443 |
| 上手难度 | 更低 | 多一步域名解析 |

## 证书模式（三选一）

| 模式 | `CERT_MODE` | 要求 | 续期 |
|---|---|---|---|
| HTTP 验证 | `http`（默认） | 域名 A 记录指向本机，放行 80 | acme.sh 每日自动 |
| Cloudflare DNS 验证 | `cf` | 域名托管在 CF + API Token（建议只给该 Zone 的 DNS 编辑权限） | acme.sh 每日自动（Token 由 acme.sh 保存在 `/root/.acme.sh` 供续期） |
| 自签证书 | `self` | 什么都不用等，`DOMAIN` 只是证书名 | 10 年有效，无需续期；客户端必须开启「允许不安全连接」并核对脚本打印的 SHA256 指纹 |

## 前置条件

1. 已装 gatecore：`sudo bash ../../install.sh`
2. 已跑过一次 `extras/xray/install-xray.sh`（它负责下载校验过的 Xray 二进制并装好服务单元；本脚本复用它们，只替换配置）
3. 按所选证书模式准备：`http` 需域名解析到本机 + 放行 80/443；`cf` 需 CF Token + 放行 443；`self` 只需放行 443

也可以直接跑仓库根目录的 `sudo bash deploy.sh`，用菜单选路线，它会转到对应脚本。

## 安装

```bash
# HTTP 验证
sudo DOMAIN=proxy.example.com bash install-domain.sh
# Cloudflare DNS 验证
sudo CERT_MODE=cf DOMAIN=proxy.example.com CF_Token=<你的Token> bash install-domain.sh
# 自签证书
sudo CERT_MODE=self DOMAIN=gatecore.local bash install-domain.sh
```

脚本会：按所选模式备好证书（LE 模式装好每日自动续期）→ 随机生成 UUID 与 WS 路径 → 写 Xray 配置（出站自动指向 gatecore 网关）→ 重启服务 → 打印 `vmess://` 客户端链接，并把订阅内容（base64）存到 `/usr/local/etc/xray/subscribe.txt`。

> 与 Reality 路线共用同一份 Xray 配置：运行本脚本会替换它，旧配置自动备份为 `config.json.bak.*`。想切回去，重跑 `extras/xray/install-xray.sh` 即可（注意 UUID/密钥会重新生成，客户端链接随之变化）。

## 订阅分发（可选）

想像作者现网那样用一条 https 订阅链接导入，把 `nginx-subscribe.conf.example` 按注释改好放进 nginx 即可：订阅地址形如 `https://你的域名:18443/s/<你自定的随机串>`，路径里的随机串相当于密码，别外传。

## 排障

- 签证书失败：先查域名解析是否已生效（`dig +short 你的域名` 应返回本机 IP）、80 端口是否放行且未被占用。
- 客户端连不上：443 是否放行；gatecore 面板里是否已连上节点（没连节点时入站通、出站不通）。
- 证书续期：acme.sh 每日 03:24 自动检查，续期后自动重启 xray 生效；手动续期 `"/root/.acme.sh"/acme.sh --cron --home "/root/.acme.sh"`。
