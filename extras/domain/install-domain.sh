#!/usr/bin/env bash
# gatecore 域名路线: VMess + WS + TLS 入站一键安装, 三种证书模式
# 用法:
#   HTTP 验证:  sudo DOMAIN=proxy.example.com bash install-domain.sh
#   CF DNS 验证: sudo CERT_MODE=cf DOMAIN=proxy.example.com CF_Token=<token> bash install-domain.sh
#   自签证书:   sudo CERT_MODE=self DOMAIN=gatecore.local bash install-domain.sh
# 前置: 已装 gatecore (/opt/gatecore/config.json) 与 Xray 二进制 (/opt/xray/xray, 见 extras/xray/install-xray.sh)
#   http 模式: 域名 A 记录已指向本机, 防火墙放行 80 与 443
#   cf   模式: 域名托管在 Cloudflare, 需一个 API Token (建议只给该 Zone 的 DNS 编辑权限);
#              Token 会被 acme.sh 保存在 /root/.acme.sh 里供自动续期使用
#   self 模式: 无需域名与公网验证, DOMAIN 只是证书名 (客户端 SNI); 客户端必须开启"允许不安全"
# 注意: 与 Reality 路线共用同一份 Xray 配置, 运行本脚本会替换它 (旧配置自动备份)。
set -euo pipefail

DOMAIN="${DOMAIN:-}"
CERT_MODE="${CERT_MODE:-http}"
XRAY_BIN=/opt/xray/xray
CONF_DIR=/usr/local/etc/xray
TLS_DIR=/usr/local/etc/xray/tls
GATECORE_CONF=/opt/gatecore/config.json
ACME=/root/.acme.sh/acme.sh
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo DOMAIN=<域名> bash install-domain.sh" >&2
  exit 1
fi
case "$CERT_MODE" in
  http|cf|self) ;;
  *) echo "未知 CERT_MODE: $CERT_MODE (可选 http | cf | self)" >&2; exit 1 ;;
esac
if [ -z "$DOMAIN" ]; then
  if [ "$CERT_MODE" = "self" ]; then
    read -rp "输入证书名称 (域名或任意名, 客户端 SNI 用它) [gatecore.local]: " DOMAIN
    DOMAIN="${DOMAIN:-gatecore.local}"
  else
    read -rp "输入已解析到本机的域名 (如 proxy.example.com): " DOMAIN
  fi
fi
if [ -z "$DOMAIN" ]; then echo "域名不能为空" >&2; exit 1; fi
if [ ! -f "$GATECORE_CONF" ]; then
  echo "未找到 $GATECORE_CONF —— 请先安装 gatecore: sudo bash ../../install.sh" >&2
  exit 1
fi
if [ ! -x "$XRAY_BIN" ]; then
  echo "未找到 $XRAY_BIN —— 请先运行 extras/xray/install-xray.sh 装好 Xray 二进制 (它会顺带装好服务单元)。" >&2
  exit 1
fi

mkdir -p "$TLS_DIR"
CERT_NOTE=""

if [ "$CERT_MODE" = "self" ]; then
  # ---------- 自签证书 (openssl, 10 年, 无需续期) ----------
  echo ">> 生成自签证书 (名称 $DOMAIN, 有效期 10 年) ..."
  if printf '%s' "$DOMAIN" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    SAN="IP:$DOMAIN"
  else
    SAN="DNS:$DOMAIN"
  fi
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$TLS_DIR/privkey.pem" -out "$TLS_DIR/fullchain.pem" \
    -days 3650 -subj "/CN=$DOMAIN" -addext "subjectAltName=$SAN" 2>/dev/null
  chmod 600 "$TLS_DIR/privkey.pem"
  FP="$(openssl x509 -in "$TLS_DIR/fullchain.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
  CERT_NOTE="自签证书 (10 年有效, 无需续期); SHA256 指纹: $FP"
else
  # ---------- Let's Encrypt (acme.sh): http standalone 或 cf dns ----------
  if [ ! -x "$ACME" ]; then
    echo ">> 安装 acme.sh ..."
    curl -s https://get.acme.sh | sh -s >/dev/null 2>&1
  fi
  "$ACME" --set-default-ca --server letsencrypt >/dev/null 2>&1 || true
  if [ "$CERT_MODE" = "http" ]; then
    if ss -tln 2>/dev/null | grep -qE '[:.]80\s'; then
      echo "80 端口已被占用, standalone 验证需要临时使用 80 端口, 请先释放它。" >&2
      exit 1
    fi
    echo ">> 为 $DOMAIN 签发 Let's Encrypt 证书 (HTTP 验证, 请确保域名已指向本机且 80 放行) ..."
    "$ACME" --issue --standalone -d "$DOMAIN" --server letsencrypt
  else
    if [ -z "${CF_Token:-}" ]; then
      read -rsp "输入 Cloudflare API Token (只给该 Zone 的 DNS 编辑权限即可): " CF_Token; echo
    fi
    if [ -z "${CF_Token:-}" ]; then echo "CF_Token 不能为空" >&2; exit 1; fi
    export CF_Token
    [ -n "${CF_Account_ID:-}" ] && export CF_Account_ID
    echo ">> 为 $DOMAIN 签发 Let's Encrypt 证书 (Cloudflare DNS 验证) ..."
    "$ACME" --issue --dns dns_cf -d "$DOMAIN" --server letsencrypt
  fi
  "$ACME" --install-cert -d "$DOMAIN" \
    --fullchain-file "$TLS_DIR/fullchain.pem" \
    --key-file "$TLS_DIR/privkey.pem" \
    --reloadcmd "systemctl restart xray"
  chmod 600 "$TLS_DIR/privkey.pem"
  # acme.sh 自带续期: 确保 root crontab 里有 --cron
  ( crontab -l 2>/dev/null | grep -v 'acme.sh.*--cron' || true
    echo '24 3 * * * "/root/.acme.sh"/acme.sh --cron --home "/root/.acme.sh" > /dev/null' ) | crontab -
  CERT_NOTE="Let's Encrypt (acme.sh 每日 03:24 自动续期, 续期后自动重启 xray)"
  if [ "$CERT_MODE" = "cf" ]; then
    CERT_NOTE="$CERT_NOTE; CF Token 已由 acme.sh 保存在 /root/.acme.sh 供续期, 请确保它长期有效"
  fi
fi

# ---------- 生成凭据 + 写配置 ----------
UUID="$("$XRAY_BIN" uuid)"
WS_PATH="/$(openssl rand -hex 4)"
read -r GW_PORT GW_USER GW_PASS <<EOF
$(python3 -c "import json;c=json.load(open('$GATECORE_CONF'));print(c['gateway_port'], c['gateway_user'], c['gateway_pass'])")
EOF
mkdir -p "$CONF_DIR"
[ -f "$CONF_DIR/config.json" ] && cp "$CONF_DIR/config.json" "$CONF_DIR/config.json.bak.$(date +%s)"
sed -e "s/__DOMAIN__/$DOMAIN/g" \
    -e "s/__UUID__/$UUID/" \
    -e "s|__CERT_FILE__|$TLS_DIR/fullchain.pem|" \
    -e "s|__KEY_FILE__|$TLS_DIR/privkey.pem|" \
    -e "s|__WS_PATH__|$WS_PATH|" \
    -e "s/__GATECORE_PORT__/$GW_PORT/" \
    -e "s/__GW_USER__/$GW_USER/" \
    -e "s/__GW_PASS__/$GW_PASS/" \
    "$HERE/config.template.json" > "$CONF_DIR/config.json"
"$XRAY_BIN" run -test -c "$CONF_DIR/config.json"

# ---------- 服务 (复用 extras/xray 装好的 xray.service; 没有则补建) ----------
if [ ! -f /etc/systemd/system/xray.service ]; then
  cat > /etc/systemd/system/xray.service <<UNIT
[Unit]
Description=Xray (official) - gatecore inbound
After=network-online.target gatecore.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=$XRAY_BIN run -c $CONF_DIR/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable xray.service >/dev/null 2>&1 || true
fi
systemctl restart xray.service
sleep 2
if ! systemctl is-active --quiet xray.service; then
  echo "xray 启动失败, 最近日志:" >&2
  journalctl -u xray --no-pager -n 10 >&2
  exit 1
fi

# ---------- 链接与订阅 ----------
LINK="$(python3 - "$UUID" "$DOMAIN" "$WS_PATH" <<'PY'
import base64, json, sys
uuid, domain, path = sys.argv[1], sys.argv[2], sys.argv[3]
v = {"v": "2", "ps": "gatecore-vmess", "add": domain, "port": "443", "id": uuid,
     "aid": "0", "scy": "auto", "net": "ws", "type": "none", "host": domain,
     "path": path, "tls": "tls", "sni": domain, "alpn": ""}
print("vmess://" + base64.b64encode(json.dumps(v).encode()).decode())
PY
)"
printf '%s' "$LINK" | base64 -w0 > "$CONF_DIR/subscribe.txt"
echo
echo "================================================================"
echo " 域名路线安装完成: VMess + WS + TLS @ $DOMAIN:443 (证书模式: $CERT_MODE)"
echo "================================================================"
echo " 客户端链接 (导入 v2rayNG 等):"
echo
echo " $LINK"
echo
echo " 订阅内容 (base64) 已存到 $CONF_DIR/subscribe.txt"
echo " 想用 https 订阅链接分发: 见同目录 nginx-subscribe.conf.example"
echo " 证书: $CERT_NOTE"
if [ "$CERT_MODE" = "self" ]; then
  echo " ⚠ 自签证书: 客户端必须开启「允许不安全连接 / allowInsecure」, 并把 SNI 填为 $DOMAIN"
fi
echo " 链路: 客户端 → Xray(443) → gatecore 网关(127.0.0.1:$GW_PORT) → 家宽出口"
echo "================================================================"
