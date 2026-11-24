# Argo 备用入口（可选加装）：VLESS + WS + Cloudflare 隧道

给已跑通任一入口路线（Reality 或域名）的部署再加一条**备用入口**：
客户端连 Cloudflare 边缘，流量经隧道落到本机一个只听回环的
VLESS+WS 后端，再走原有出站去 gatecore。

适合的场景：源站 IP 被封、或机器没有稳定公网 IP 时，直连入口全断，
这条还能活。平时它比直连多绕一跳，**定位是备用，不替代直连**。

## 为什么乘客是 VLESS+WS（作者实测）

- VMess+WS 过隧道：2026-11-07 实测失败——服务端检查全绿、WS 握手
  101，但手机真实使用打不开网页；监视显示连接送达后端却几乎没有
  数据流向出口，根因未定案。
- VLESS+WS 过同一条隧道：2026-11-23 服务端与手机真实使用逐关全过。
  详见仓库 `docs/FULL-CHAIN.md`。

## 前置条件

1. 已装 gatecore 并跑通任一入口路线（`extras/xray` 或 `extras/domain`），
   即 `/opt/xray/xray` 与 `/usr/local/etc/xray/config.json` 已存在。
2. 一个托管在 Cloudflare 的域名，以及一条已建好的 Cloudflare 隧道
   （令牌或本地托管均可）。隧道本身的创建不在本脚本范围。

## 安装

```bash
sudo bash install-argo.sh
# 依次问两个问题, 都可回车接受默认:
#   1) Argo 入口域名 (如 argo.example.com) —— 必须填
#   2) 优选域名 —— 回车用默认 cf.090227.xyz, 或填 none 不要优选版链接
```

非交互：

```bash
sudo TUNNEL_DOMAIN=argo.example.com bash install-argo.sh
sudo TUNNEL_DOMAIN=argo.example.com PREFERRED_DOMAIN=none bash install-argo.sh
```

脚本会：备份现有配置 → 合并一个 `127.0.0.1:31301` 的 VLESS+WS 入站
（幂等，重复运行不重复添加）→ 冷测 → 重启 xray（失败自动回滚）→
打印 Cloudflare 侧的确切绑定步骤与两条客户端链接（原版 + 优选版）。
状态（UUID/路径/域名）存 `/usr/local/etc/xray/argo.json`（0600）。

## Cloudflare 侧绑定（二选一，与脚本末尾打印的一致）

- **令牌隧道**：Zero Trust 控制台 → 网络 → 隧道 → 选隧道 →
  公共主机名 → 添加：域名填你的 Argo 域名，服务 `HTTP`，
  URL `localhost:31301`。DNS 由 CF 自动创建。
- **本地托管隧道**：`config.yml` 的 ingress 加
  `{hostname: <Argo 域名>, service: http://localhost:31301}`，
  重启 cloudflared，再 `cloudflared tunnel route dns <隧道名> <Argo 域名>`。

## 优选域名是什么

CF 边缘按 SNI 与 Host 头路由，不按拨号 IP 的归属。把客户端的
**连接地址**换成一个解析到优质边缘的第三方域名（优选域名），SNI 与
WS Host 保持节点域名不变，就能绕开本地 DNS 给的拥挤边缘——服务端
零改动，TLS 仍按节点域名验证。

注意：优选域名是第三方的，**会过期失效**。优选版突然不通先切回
原版链接，多半不是节点坏了。长期最稳：用 CloudflareSpeedTest 在
自己的网络测出最优 IP，直接以 IP 作连接地址（SNI/Host 同理不变）。

## 排障

- 绑定后打不开：先在服务器本机测后端
  `curl -i http://127.0.0.1:31301/vlessws`（应返回 400，说明后端在）；
  再从外网测 `https://<Argo 域名>/vlessws`（同样应 400，经隧道到达）。
- 延迟数字不算数：以真实使用（连上开网页、看出口 IP）为准。
