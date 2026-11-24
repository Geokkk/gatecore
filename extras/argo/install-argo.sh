#!/usr/bin/env bash
# gatecore Argo 备用入口加装: 回环 VLESS+WS 后端 + Cloudflare 隧道落点 + 优选域名链接
# 用法 (交互, 填一次域名、其余回车即可):
#   sudo bash install-argo.sh
# 非交互:
#   sudo TUNNEL_DOMAIN=argo.example.com bash install-argo.sh
#   sudo TUNNEL_DOMAIN=argo.example.com PREFERRED_DOMAIN=none bash install-argo.sh
# 前置: 已装 gatecore 与任一 Xray 路线 (extras/xray 或 extras/domain), 即
#   /opt/xray/xray、/usr/local/etc/xray/config.json 与 xray.service 单元已存在。
# 本脚本只往现有配置里"加"一个只听回环的入站 (自动备份 + 冷测 + 失败回滚),
# 不动已有入站/出站。隧道本身的绑定在 Cloudflare 侧完成, 脚本最后会打印确切步骤。
set -euo pipefail

XRAY_BIN=/opt/xray/xray
CONF_DIR=/usr/local/etc/xray
CONF="$CONF_DIR/config.json"
STATE="$CONF_DIR/argo.json"
HERE="$(cd "$(dirname "$0")" && pwd)"

TUNNEL_DOMAIN="${TUNNEL_DOMAIN:-}"
PREFERRED_GIVEN=0
if [ -n "${PREFERRED_DOMAIN+x}" ]; then PREFERRED_GIVEN=1; fi
PREFERRED_DOMAIN="${PREFERRED_DOMAIN:-cf.090227.xyz}"
ARGO_PORT="${ARGO_PORT:-31301}"
WS_PATH="${WS_PATH:-/vlessws}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo bash install-argo.sh" >&2
  exit 1
fi
if [ ! -x "$XRAY_BIN" ] || [ ! -f "$CONF" ]; then
  echo "未找到 Xray 二进制或配置 —— 请先用 deploy.sh 装好任一入口路线 (Reality 或域名)。" >&2
  exit 1
fi
if ! systemctl cat xray.service >/dev/null 2>&1; then
  echo "未找到 xray.service 服务单元 —— 请先跑 extras/xray/install-xray.sh 或 extras/domain/install-domain.sh 装好服务单元。" >&2
  exit 1
fi

# ---------- 参数 (回车接受默认) ----------
if [ -z "$TUNNEL_DOMAIN" ]; then
  read -rp "输入 Argo 入口域名 (将绑到隧道的子域名, 如 argo.example.com): " TUNNEL_DOMAIN || true
fi
if [ -z "$TUNNEL_DOMAIN" ]; then
  echo "域名不能为空" >&2
  exit 1
fi
if [ "$PREFERRED_GIVEN" -eq 0 ] && [ -t 0 ]; then
  read -rp "优选域名 (客户端连接地址, 回车用默认 cf.090227.xyz, 输入 none 表示不要): " P || true
  PREFERRED_DOMAIN="${P:-cf.090227.xyz}"
fi
if [ "$PREFERRED_DOMAIN" = "none" ]; then PREFERRED_DOMAIN=""; fi

# ---------- 合并后端入站 ----------
BAK="$CONF.bak.$(date +%s)"
cp "$CONF" "$BAK"
echo ">> 已备份现有配置到 $BAK"
if ! python3 "$HERE/add-vless-ws.py" --config "$CONF" --state "$STATE" --domain "$TUNNEL_DOMAIN" --preferred "$PREFERRED_DOMAIN" --port "$ARGO_PORT" --path "$WS_PATH" >/dev/null; then
  echo "合并后端入站失败, 现有配置未被修改 (备份在 $BAK)。" >&2
  exit 1
fi
# 以状态文件为准读回实际生效的端口/路径 (重复运行时后端可能已存在, 以既有值为准)
read -r EFF_PORT EFF_PATH <<EOF
$(python3 -c "import json;s=json.load(open('$STATE'));print(s['port'], s['path'])")
EOF

if ! "$XRAY_BIN" run -test -c "$CONF" >/dev/null 2>&1; then
  echo "新配置冷测未过, 已回滚。" >&2
  cp "$BAK" "$CONF"
  exit 1
fi
systemctl restart xray.service
sleep 2
if ! systemctl is-active --quiet xray.service; then
  echo "xray 重启失败, 已回滚到旧配置。" >&2
  cp "$BAK" "$CONF"
  systemctl restart xray.service || true
  journalctl -u xray --no-pager -n 10 >&2
  exit 1
fi
echo ">> VLESS+WS 后端已就绪: 127.0.0.1:$EFF_PORT (路径 $EFF_PATH)"

# ---------- 生成链接 ----------
LINKS_TMP="$(mktemp)"
python3 - "$STATE" > "$LINKS_TMP" <<'PY'
import json, sys, urllib.parse
s = json.load(open(sys.argv[1]))
q = "encryption=none&security=tls&sni=%s&type=ws&host=%s&path=%s" % (
    s["domain"], s["domain"], urllib.parse.quote(s["path"], safe=""))
def link(host):
    return "vless://%s@%s:443?%s" % (s["uuid"], host, q)
print(link(s["domain"]) + "#gatecore-argo")
print((link(s["preferred"]) + "#gatecore-argo-fast") if s.get("preferred") else "-")
PY
LINK="$(sed -n '1p' "$LINKS_TMP")"
LINK_FAST="$(sed -n '2p' "$LINKS_TMP")"
rm -f "$LINKS_TMP"

echo
echo "================================================================"
echo " Argo 备用入口: 后端完成, 还差 Cloudflare 侧一步"
echo "================================================================"
echo " 【令牌隧道】在 Cloudflare Zero Trust 控制台操作:"
echo "   网络 → 隧道 → 选你的隧道 → 公共主机名 → 添加:"
echo "     子域/域名: $TUNNEL_DOMAIN"
echo "     服务类型: HTTP"
echo "     URL:       localhost:$EFF_PORT"
echo "   (DNS 由 CF 自动创建, 保存后约一分钟生效)"
echo
echo " 【本地托管隧道】在 config.yml 的 ingress 里加一条后重启 cloudflared:"
echo "   - hostname: $TUNNEL_DOMAIN"
echo "     service: http://localhost:$EFF_PORT"
echo "   并执行: cloudflared tunnel route dns <隧道名> $TUNNEL_DOMAIN"
echo "================================================================"
echo " 客户端链接 (原版, 绑定完成后导入测试):"
echo
echo " $LINK"
if [ "$LINK_FAST" != "-" ]; then
  echo
  echo " 客户端链接 (优选域名版, 连接地址 $PREFERRED_DOMAIN, 与原版二选一测速):"
  echo
  echo " $LINK_FAST"
  echo
  echo " 注: 优选域名是第三方的、可能过期失效; 突然不通先切回原版链接。"
fi
echo "================================================================"
echo " 链路: 客户端 → CF 边缘 → 隧道 → 127.0.0.1:$EFF_PORT → gatecore → 家宽出口"
echo " 定位: 备用/防封入口, 不替代直连。状态文件: $STATE"
echo "================================================================"
