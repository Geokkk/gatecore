# gatecore

基于 [VPN Gate](https://www.vpngate.net/) 公开节点的**家宽出口核心**：自动拉取节点列表、区分家宽/机房、按质量排序，经 OpenVPN 建立隧道，再以本地 SOCKS5 / HTTP 网关把流量灌出去——出口 IP 就是志愿者家里的宽带 IP。

**纯 Python 标准库、零第三方依赖、单文件实现、全源码可审计。**

同类工具的核心多以闭源二进制分发，行为不可审计。gatecore 是为「可审计」而写的开源替代：不到一千行 Python，任何人都可以逐行检查它做了什么、没做什么。

## 一行部署

```bash
git clone https://github.com/Geokkk/gatecore.git && cd gatecore && sudo bash deploy.sh
```

回车式向导四步走完（选入口 → Argo 备用入口 → 域名证书 → 面板挂域名），最后依次打印客户端链接与面板网址。装完 SSH 登录输入 `gk` 打开交互面板。细节见下方「快速开始」。第一次装、想照着一步一步来的，看 [新手部署教程](docs/tutorial.html)（从准备、放行防火墙到手机导入与验收的完整八步，下载后用浏览器打开）。

## 工作原理

```
你的设备 / 其它代理核心
        │  SOCKS5 / HTTP（账密认证）
        ▼
  gatecore 网关 ──绑定──► tun 设备 geotun0 ──► OpenVPN 隧道 ──► VPN Gate 志愿节点 ──► 互联网
        ▲                                                              （家宽出口）
  Web 面板（仅回环 + 秘密路径 + Basic 认证）：选国家、看归属、一键切换
```

- **节点供给**：多源抓取（官方 API 双路径 + 第三方镜像双 CDN）并入 48 小时累积池——官方每次只返回全部志愿者的一小部分子集，多源、跨时并集才凑得齐；48 小时未再出现的节点淘汰，全部源失败时沿用旧池。
- **家宽分类**：多信号判定（主机名/运营者字段 + ISP/组织关键词），把真正的住宅节点与项目方/机房节点分开，面板有徽标与「只看家宽」筛选。
- **排序**：综合 API 评分、速度、历史连接成功率、经隧道实测时延（EWMA 平滑）、UDP 加成；连接前有并发预探测，死节点直接跳过、不计惩罚。
- **网关**：SOCKS5（含 UDP ASSOCIATE）+ HTTP 混合网关，出站 socket 绑定 tun 设备，物理上不可能绕过隧道；DNS 只走隧道内解析器，失败即报错、绝不回退系统解析（fail-closed）。
- **面板**：状态总览、国家筛选、节点归属与 ISP、一键切换/断开、四种运行模式（手动 / 自动 / 固定国家 / 固定 IP）、账号设置（右上角头像菜单：面板登录凭据可在面板内修改、立即生效，含退出登录）。网关凭据由安装时随机生成，故意不开放面板修改——改它必须同步改客户端一侧（如 Xray 出站），否则会断链；确需更换时改 `config.json` 的 `gateway_user` / `gateway_pass` 并同步客户端。`config.json` 只在文件缺失时首次生成；若文件存在但不可读或损坏，程序会报错退出，绝不静默重建凭据。
- **守候**：盯住指定国家，一旦有新的家宽节点出现就记录并通知——**只通知，绝不自动连接**。
- **收藏冷备**：面板里点 ☆ 把节点收藏成一条冷备队列（状态区有队列卡，顺位/在线状态一目了然，每 5 分钟轻探刷新）；自动 / 固定国家模式下主节点断开时按队列顺序从收藏中顺延（固定国家时本国收藏优先），收藏全部不可用才回落全局排行，不断网优先且回落会在事件里标明。手动 / 固定 IP 模式不顺延。
- **健康循环**：每 30 秒经网关实测出口；自动类模式下节点死亡自动切换，手动模式只记录、等面板操作。
- **出口实测**：面板状态区显示当前出口的实测速度与网页延时——网页延时随每轮健康检查计时，实测速度每 10 分钟经隧道下载 1 MB 测试文件测得（`gatecore.py speedtest` 可随时手动测一次）。
- **可达轮探**：后台每分钟并行轻探一批节点（1200 个的池约 25 分钟轮完一遍），面板在节点行以圆点标注可达 / 不可达 / 未知（tls-auth 节点裸探不了），并提供「只看可达」筛选，不用再逐个手点试错；连接前的预探测照旧兜底。

## 快速开始

要求：Linux（需要 `/dev/net/tun`）、Python 3.9+、OpenVPN、以 root 运行（创建 tun 设备所需）。

```bash
git clone https://github.com/Geokkk/gatecore.git
cd gatecore
sudo python3 gatecore.py serve
```

首次运行自动生成 `config.json`（权限 0600）：网关/面板的账号密码与面板秘密路径均为随机生成，请从该文件读取。启动日志会打印面板地址，默认仅监听回环：

- 网关：`127.0.0.1:18500`（SOCKS5 + HTTP，账密认证）
- 面板：`127.0.0.1:18787/<秘密路径>/`

端口可用环境变量覆盖：`GATECORE_PORT`、`GATECORE_PANEL_PORT`、`GATECORE_PANEL_BIND`。

systemd 托管示例：

```ini
[Unit]
Description=gatecore residential egress core
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/gatecore
ExecStart=/usr/bin/python3 /opt/gatecore/gatecore.py serve
Restart=on-failure
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
```

启动时会对账：若状态文件残留着「已连接」但 tun 设备不存在（例如机器重启过），会清除残留状态，面板如实显示未连接。

一键安装（自动装依赖、部署到 `/opt/gatecore`、装 systemd 服务、打印凭据）：

```bash
sudo bash install.sh
```

## 三步跑通完整链路（小白路线）

gatecore 本身只是**出口**核心。要让手机/电脑真正用上，还需要一个**入口**。仓库在 `extras/xray/` 附了一套官方 Xray 的对接模板与一键脚本，核心保持纯粹、入口按需拼装：

```bash
# 第 1 步: 装出口核心 (见上文一键安装), 然后在面板里连上一个国家的节点
sudo bash install.sh

# 第 2 步: 装入口 (官方 Xray, VLESS + Reality, 免域名免证书, 出站自动指向 gatecore)
sudo bash extras/xray/install-xray.sh

# 第 3 步: 脚本最后会打印一条 vless:// 链接, 导入客户端 (v2rayNG / Streisand / Shadowrocket 等) 即可
```

链路：`客户端 → Xray(443, Reality) → gatecore 网关 → OpenVPN 隧道 → 家宽出口`。

说明：安装脚本会从 GitHub 下载官方 Xray（版本锁定在与作者现网一致的 v26.11.9），并对照官方 `.dgst` 校验 sha256 后才安装；UUID、Reality 密钥均为本机随机生成。云厂商安全组记得放行 443/TCP。

**域名路线（可选）**：已有域名、想要传统 VMess + WS + TLS 形态（作者现网用的就是这条）的，看 `extras/domain/`：一键脚本自动签 Let's Encrypt 证书并装好续期，附订阅分发示例。与 Reality 路线二选一，细节见该目录的 README。

**一键部署向导（推荐）**：一条命令走完全流程——装本体 → 选入口 → 问是否加装 Argo → 问是否把面板挂到域名，每一步回车即确认默认，最后依次打印客户端链接与面板网址。也支持 `MODE=` 环境变量单步直达（`reality` / `domain-http` / `domain-cf` / `selfsigned` / `argo`）：

```bash
sudo bash deploy.sh
# 向导第 1 步选入口: 1) Reality  2) 域名+HTTP验证  3) 域名+CF DNS验证  4) 自签证书
# 向导第 2 步: 加装 Argo 备用入口? [回车=加装]
# 向导第 3 步: 准备域名证书 (入口已自带则自动复用, 否则选 HTTP/CF/自签新签)
# 向导第 4 步: 把管理面板挂到域名?   [回车=挂, 最后打印 https 面板网址]
```

**快捷命令 `gk`**：装好后 SSH 登录服务器，输入 `gk` 就打开交互面板——服务状态、当前出口、节点排行、按国家连接/断开、出口自检、守候名单、客户端链接（Reality/域名/Argo，哪条装了显示哪条）、Web 面板地址、服务重启与日志，全在菜单里，不用记任何命令。菜单里的「升级 / 卸载」可分开处理出口端（本体：升级保留全部凭据与状态；卸载只删本体）与进口端（Xray：升级只换到仓库锁定版二进制、配置不动且带冷测与自动回滚；卸载清入口配置/证书/Argo），外加一键全部卸载；所有卸载都要输入 yes 才执行。

**Argo 备用入口（可选加装）**：任一路线跑通后，用向导第 2 步（或 `extras/argo/install-argo.sh`，或 `MODE=argo`）加一条经 Cloudflare 隧道的备用入口：回环 VLESS+WS 后端 + 隧道落点，填一次域名、其余回车即可，并生成原版与**优选域名**两条客户端链接。源站 IP 被封或变更时它不受影响；定位是备用，不替代直连。为什么乘客选 VLESS+WS 而不是 VMess（作者实测对比）见 `extras/argo/README.md`。

**面板挂域名（可选加装）**：面板默认只听回环（SSH 隧道打开）。用向导第 3–4 步（或 `extras/panel/install-panel.sh`）可先备好域名证书、再把它经 nginx 反代挂到域名上：能复用域名路线的证书就复用（域名自动从证书里读出，回车即可），否则按所选方式新签，最后打印 `https://域名:8443/<秘密路径>/` 面板网址与账号密码。

**作者现网的完整形态**（前门分流、Argo 落点、证书/订阅、Xray 升级纪律）总结在 [`docs/FULL-CHAIN.md`](docs/FULL-CHAIN.md)，一键路线是它的简化版。

## 与其它代理核心对接

网关就是一个标准的账密 SOCKS5 出口。任何代理核心（Xray、sing-box 等）把出站指过来，即可获得家宽出口：

```json
{ "protocol": "socks", "settings": { "servers": [{ "address": "127.0.0.1", "port": 18500,
  "users": [{ "user": "<gateway_user>", "pass": "<gateway_pass>" }] }] } }
```

## 命令行

```bash
python3 gatecore.py fetch      # 刷新节点列表
python3 gatecore.py sync       # 重新做家宽分类
python3 gatecore.py list [CC]  # 看排序后的候选
python3 gatecore.py countries  # 可选国家列表（代码 / 节点数 / 家宽数）
python3 gatecore.py connect JP # 连指定国家（家宽优先）
python3 gatecore.py disconnect
python3 gatecore.py check      # 经网关实测当前出口
python3 gatecore.py speedtest  # 手动测一次当前出口的速度与网页延时
python3 gatecore.py watch      # 守候状态
python3 test_udp.py            # 自检：UDP 出口与 TCP 出口是否一致
```

## 安全说明

- 默认只绑回环；如需对外暴露面板，请自行置于反向代理与 TLS 之后。
- 所有凭据随机生成、只存本机 `config.json`（0600），不经任何网络传输。
- DNS 失败关闭：隧道解析不通就报错，不会偷偷用系统 DNS。
- 本程序需要 root 仅因创建 tun 设备与拉起 OpenVPN；除 VPN Gate 公开 API 与所选节点的 OpenVPN 端点外，不连接任何其它外部服务，无遥测。

## 已知限制

- 不支持 IPv6（失败关闭，不泄漏）。
- VPN Gate 的节点名单本身是公开的，出口 IP 可能被识别为代理——这是这类供给的固有属性，任何基于 VPN Gate 的方案都一样。
- 家宽节点是志愿者家里的设备，说下线就下线；可用性靠预探测 + 健康检查 + 故障切换兜底。
- 本项目是**出口**核心，本体不含入站协议；仓库 `extras/xray/` 附有官方 Xray 入站模板与一键脚本（见「三步跑通完整链路」），也可自行用任意 Xray / sing-box 入站指向本网关。

## License

MIT

---

## English summary

gatecore is a single-file, zero-dependency, fully auditable residential-egress core built on the public [VPN Gate](https://www.vpngate.net/) relay network. It fetches and classifies relay nodes (residential vs datacenter, multi-signal), ranks them by connection history and measured through-tunnel latency, brings up an OpenVPN tunnel, and exposes an authenticated SOCKS5 (TCP + UDP) / HTTP gateway bound to the tunnel device with fail-closed DNS. A loopback web panel (secret path + Basic auth) offers country filtering, one-click switching, four run modes, and a notify-only country watch that never auto-connects. Written as an open-source alternative to closed-binary cores of the same kind. MIT licensed.
