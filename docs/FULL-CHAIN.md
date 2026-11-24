# 完整链路（作者现网形态）

这份文档总结作者现网实际运行的一套「手搓」全链路：它比仓库里的一键路线
（`extras/xray`、`extras/domain`）多了一层前门分流与一条 Argo 备用入口。
一键路线是它的简化版——先跑通路线、再按本文进阶，是推荐的上手顺序。

## 总览

```
                          ┌─ 直连 ─────────────────────────────┐
 手机/电脑 ───────────────┤                                    ▼
                          │                          Xray 前门 :28823 (TLS)
                          │                          ├─ 默认 / h2 ──► nginx 伪装站 (回环)
                          │                          ├─ /jaitvws ──► VMess+WS 后端 :31299
                          └─ Argo ─► CF 边缘 ─► 隧道 ─┤
                                                     └─ /vlessws ──► VLESS+WS 后端 :31301
                                                                             │
                                              全部入站出站 ─► SOCKS5 :8500 (账密)
                                                                             ▼
                                              gatecore ─► OpenVPN ─► VPN Gate 家宽节点 ─► 互联网

 旁路: 面板 nginx :8443 ─► gatecore 面板 (回环, 秘密路径)
       订阅 nginx :18443 ─► 订阅文件 (https, 随机路径)
       证书 acme.sh 每日自查 ─► /etc/gatecore/tls (续期后 reload nginx + restart xray)
```

## 组件与端口

| 组件 | 监听 | 说明 |
|---|---|---|
| Xray 前门 | `0.0.0.0:28823` | VLESS+TLS+Vision 直连入站，同时用 fallback 分流 WS 后端与伪装站 |
| VMess 后端 | `127.0.0.1:31299` | VMess+WS，只接受前门转发（见下节的 xver 配对） |
| VLESS 后端 | `127.0.0.1:31301` | VLESS+WS，Argo 隧道的落点，也挂在前门 fallback 上 |
| nginx 伪装站 | `127.0.0.1:31300 / 31302` | 前门的默认与 h2 回落目标，探测者只会看到一个普通网站 |
| gatecore 网关 | `127.0.0.1:8500` | SOCKS5+HTTP，账密与 Xray 出站对齐 |
| gatecore 面板 | `127.0.0.1:18787` | nginx 在 8443 前置（TLS + 秘密路径） |
| cloudflared | — | 令牌隧道，只承载 SSH 维护与 Argo 入口两条主机名 |

## 前门分流设计（关键细节）

前门是一个带 `fallbacks` 的 VLESS+TLS 入站，按 SNI/ALPN/路径分流：

- 默认与 `alpn=h2`：回落到 nginx 伪装站，`xver: 1`（附带 PROXY 协议头）。
- `path=/jaitvws`：回落 VMess 后端，`xver: 1`。**配对关系**：后端的
  `wsSettings` 必须带 `acceptProxyProtocol: true`，否则握手即断。
- `path=/vlessws`：回落 VLESS 后端，`xver: 0`。**这里不能照抄上一条**：
  VLESS 后端还要同时接受 cloudflared 的直连（不带 PROXY 头），所以它
  的 `wsSettings` 不带 `acceptProxyProtocol`、前门这条 fallback 也不
  发 PROXY 头。两种到达方式（前门转发 / 隧道直连）共用一个后端。

后端都不带 TLS：TLS 在前门终结（直连时）或在 Cloudflare 边缘终结
（Argo 时），回环这一段是明文 WS——它不出本机。

## 出口契约（gatecore）

- Xray 的出站 SOCKS5 凭据必须与 gatecore `config.json` 里的网关凭据
  **逐字对齐**，在服务器本地完成对齐、不经网络传输。
- `config.json` 的属主必须保持为运行身份：以 root 身份改它再保存会把
  属主翻成 root，低权限的定时任务就读不到。gatecore 本体的策略是
  「文件缺失才生成；存在但读不到就报错退出，绝不静默重建凭据」——
  静默重建会让网关凭据与 Xray 出站脱节，下一次重启即断链。
- 面板不开放网关凭据修改，原因同上：一键改密必然与 Xray 侧脱同步。

## Argo 备用入口

定位：**备用/防封，不替代直连**。直连多一跳 Cloudflare 只会更慢；它
的价值在源站 IP 被封或变更时，入口域名与客户端配置一字不用改。

为什么乘客选 VLESS+WS（实测结论，非偏好）：

- 2026-11-07 实测 VMess+WS 过同一条隧道：服务端一切检查全绿（WS 握手
  101、配置逐项核对、出口正常），但手机真实使用打不开网页；监视实验
  显示会话送达后端、连接稳定，去往 8500 出口的连接却约等于零——
  管子通、数据不流，根因未能定案到抓包层以下。
- 2026-11-23 换 VLESS+WS：服务端四条链、临时隧道、固定域名、手机
  真实使用，逐关全过。同一隧道、同一落地方式，只换了协议。

绑定方式取决于隧道类型：

- **令牌（控制台托管）隧道**：机器上只有隧道令牌、没有改配置的凭据，
  必须在 Cloudflare Zero Trust 控制台给隧道加一条 Public Hostname：
  子域自定、服务 `HTTP`、URL `localhost:31301`。DNS 由 CF 自动建。
- **本地托管隧道**：在 `config.yml` 的 ingress 里加
  `{hostname: argo.example.com, service: http://localhost:31301}` 后
  重启 cloudflared，并用 `cloudflared tunnel route dns` 建解析。

仓库的 `extras/argo/` 提供这条入口的一键加装（回环后端 + 链接生成 +
控制台步骤打印），不必手改分片。

## 优选域名（客户端侧）

原理：CF 边缘按 **SNI 与 Host 头**路由，不按你拨号的 IP 归属。于是
客户端可以把「连接地址」换成一个解析到优质 CF 边缘的第三方域名
（优选域名），SNI 与 WS Host 保持节点域名不变——服务端零改动，
TLS 仍按节点域名验证。

- 链接形态：`vless://<uuid>@<优选域名>:443?…&sni=<节点域名>&host=<节点域名>&path=<路径>`
- 优选域名是第三方的，**会过期失效**：突然不通先切回原版链接，多半
  不是节点坏了。长期最稳的是用 CloudflareSpeedTest 在自己的网络里
  测出个人最优 IP，直接拿 IP 当连接地址（SNI/Host 同理不变）。

## 证书与订阅

- 证书由 acme.sh 自管（webroot 验证），装到固定目录，`--reloadcmd`
  在续期后 reload nginx 并 restart xray；每日 cron 自查。
- 订阅是静态文件经 nginx 以 https 分发，路径带随机串；节点增减就是
  改文件内容，客户端刷新即得。

## Xray 升级纪律

现网 2026-11-24 已从 v26.3.27 升至 v26.11.9，按的就是这套：

1. 只跟官方版本，先读 release notes，重点看 VMess / WebSocket /
   Reality 有没有移除或格式变更（v26.11.9 起 WS 也被标弃用警告，
   与 VMess 同级、均无时间表）。
2. 下载后对照官方 `.dgst` 验 sha256——注意其格式是
   `SHA2-256= <hex>` 一行一种摘要，取 256 位那行比对。
3. 新二进制先对**全套生产配置**跑 `xray run -test`，过了才换。
4. 旧二进制改名留置（如 `xray.bak-v<旧版本>`），换完逐条链复验
   （各入站 × 直连/隧道各测一次出口），不对一分钟回滚。
5. 版本监测只报不自动升：每周查一次上游，有新版才通知人工决定。

## 与仓库路线的对应

| 现网形态 | 仓库等价物 |
|---|---|
| gatecore 出口（8500） | `install.sh`（默认端口以 config.json 为准） |
| 前门 + VMess 后端 | `extras/domain`（简化为单入站直听 443） |
| VLESS 后端 + Argo | `extras/argo`（加装式：回环后端 + 隧道落点） |
| VLESS+Reality 直连 | `extras/xray` |
| 订阅/证书 | `extras/domain` 的 acme 三模式 + nginx 订阅示例 |
