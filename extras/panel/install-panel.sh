#!/usr/bin/env bash
# gatecore 管理面板挂域名: nginx 反代回环面板 + https, 最后打印面板网址
# 用法 (交互): sudo bash install-panel.sh        # 会问域名, 其余回车默认
# 非交互:     sudo PANEL_DOMAIN=panel.example.com bash install-panel.sh
#             sudo PANEL_DOMAIN=panel.example.com CERT_MODE=cf CF_Token=<token> bash install-panel.sh
# 证书 (CERT_MODE, 默认 auto):
#   auto  先看域名路线装好的证书 (/usr/local/etc/xray/tls) 是否覆盖本域名, 覆盖就复用;
#         不覆盖则按 http 方式新签一张到 /usr/local/etc/gatecore-panel/tls
#   http  Let's Encrypt HTTP 验证 (域名 A 记录须指向本机, 80 端口放行)
#   cf    Let's Encrypt Cloudflare DNS 验证 (需 CF_Token)
#   self  自签证书 (浏览器会警告, 仅临时用)
# 前置: 已装 gatecore (/opt/gatecore/config.json)。面板本身只听回环, 本脚本不动它。
set -euo pipefail

GATECORE_CONF=/opt/gatecore/config.json
XRAY_TLS_DIR=/usr/local/etc/xray/tls
PANEL_TLS_DIR=/usr/local/etc/gatecore-panel/tls
ACME=/root/.acme.sh/acme.sh
PANEL_DOMAIN="${PANEL_DOMAIN:-}"
PANEL_PORT="${PANEL_PORT:-8443}"
CERT_MODE="${CERT_MODE:-auto}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo bash install-panel.sh" >&2
  exit 1
fi
if [ ! -f "$GATECORE_CONF" ]; then
  echo "未找到 $GATECORE_CONF —— 请先安装 gatecore 本体。" >&2
  exit 1
fi
case "$CERT_MODE" in
  auto|reuse|http|cf|self) ;;
  *) echo "未知 CERT_MODE: $CERT_MODE (可选 auto | reuse | http | cf | self)" >&2; exit 1 ;;
esac

# ---------- 面板参数 (取自 gatecore 配置) ----------
read -r GW_PANEL_PORT PANEL_PATH PANEL_USER PANEL_PASS <<EOF
$(python3 -c "import json;c=json.load(open('$GATECORE_CONF'));print(c.get('panel_port',18787), c['panel_path'], c['panel_user'], c['panel_pass'])")
EOF

# ---------- 域名 ----------
# 没给域名时, 先从入口路线现成证书里读出它的域名作默认值, 回车即可
if [ -z "$PANEL_DOMAIN" ] && [ -z "${DOMAIN:-}" ] && [ -f "$XRAY_TLS_DIR/fullchain.pem" ]; then
  DOMAIN="$(openssl x509 -in "$XRAY_TLS_DIR/fullchain.pem" -noout -ext subjectAltName 2>/dev/null | grep -oE 'DNS:[^, ]+' | head -1 | cut -d: -f2 || true)"
fi
if [ -z "$PANEL_DOMAIN" ]; then
  if [ -n "${DOMAIN:-}" ]; then
    read -rp "输入面板域名 [回车用入口域名 $DOMAIN]: " PANEL_DOMAIN || true
    PANEL_DOMAIN="${PANEL_DOMAIN:-$DOMAIN}"
  else
    read -rp "输入面板域名 (如 panel.example.com, 需已解析到本机): " PANEL_DOMAIN || true
  fi
fi
if [ -z "$PANEL_DOMAIN" ]; then echo "域名不能为空" >&2; exit 1; fi

# ---------- nginx ----------
if ! command -v nginx >/dev/null 2>&1; then
  echo ">> 安装 nginx ..."
  apt-get update -qq && apt-get install -y -qq nginx >/dev/null
fi

# ---------- 证书 ----------
CERT_FILE=""; KEY_FILE=""; CERT_NOTE=""
cert_covers_domain() {
  [ -f "$XRAY_TLS_DIR/fullchain.pem" ] || return 1
  openssl x509 -in "$XRAY_TLS_DIR/fullchain.pem" -noout -ext subjectAltName 2>/dev/null | grep -q "DNS:$1" && return 0
  openssl x509 -in "$XRAY_TLS_DIR/fullchain.pem" -noout -subject 2>/dev/null | grep -q "CN = $1" && return 0
  return 1
}
if [ "$CERT_MODE" = "auto" ] || [ "$CERT_MODE" = "reuse" ]; then
  if cert_covers_domain "$PANEL_DOMAIN"; then
    CERT_FILE="$XRAY_TLS_DIR/fullchain.pem"; KEY_FILE="$XRAY_TLS_DIR/privkey.pem"
    CERT_NOTE="复用域名路线的证书 ($PANEL_DOMAIN)"
    # 若该证书由 acme.sh 管理, 把续期动作补上 nginx reload, 免得续期后面板还用旧证书
    if [ -x "$ACME" ] && "$ACME" --list 2>/dev/null | grep -q "$PANEL_DOMAIN"; then
      "$ACME" --install-cert -d "$PANEL_DOMAIN" \
        --fullchain-file "$CERT_FILE" --key-file "$KEY_FILE" \
        --reloadcmd "systemctl restart xray; systemctl reload nginx || true" >/dev/null 2>&1 || true
      CERT_NOTE="$CERT_NOTE; 续期将自动 reload nginx"
    fi
  elif [ "$CERT_MODE" = "reuse" ]; then
    echo "现有证书不覆盖 $PANEL_DOMAIN, 且 CERT_MODE=reuse, 已中止。" >&2
    exit 1
  elif [ -t 0 ]; then
    read -rp "现有证书不覆盖 $PANEL_DOMAIN, 选新证书方式 [1 HTTP验证(默认) / 2 CF DNS验证 / 3 自签]: " CM || true
    case "$CM" in
      2) CERT_MODE="cf" ;;
      3) CERT_MODE="self" ;;
      *) CERT_MODE="http" ;;
    esac
  else
    CERT_MODE="http"
  fi
fi

if [ -z "$CERT_FILE" ]; then
  mkdir -p "$PANEL_TLS_DIR"
  CERT_FILE="$PANEL_TLS_DIR/fullchain.pem"; KEY_FILE="$PANEL_TLS_DIR/privkey.pem"
  if [ "$CERT_MODE" = "self" ]; then
    echo ">> 生成自签证书 ($PANEL_DOMAIN, 10 年) ..."
    if printf '%s' "$PANEL_DOMAIN" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then SAN="IP:$PANEL_DOMAIN"; else SAN="DNS:$PANEL_DOMAIN"; fi
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$KEY_FILE" -out "$CERT_FILE" \
      -days 3650 -subj "/CN=$PANEL_DOMAIN" -addext "subjectAltName=$SAN" 2>/dev/null
    chmod 600 "$KEY_FILE"
    CERT_NOTE="自签证书 (浏览器会警告, 仅临时用)"
  else
    if [ ! -x "$ACME" ]; then
      echo ">> 安装 acme.sh ..."
      curl -s https://get.acme.sh | sh -s >/dev/null 2>&1
    fi
    "$ACME" --set-default-ca --server letsencrypt >/dev/null 2>&1 || true
    NGINX_WAS_ACTIVE=0
    if [ "$CERT_MODE" = "http" ]; then
      # standalone 需要独占 80: nginx 若在跑先停, 签完再起
      if systemctl is-active --quiet nginx; then NGINX_WAS_ACTIVE=1; systemctl stop nginx; fi
      if ss -tln 2>/dev/null | grep -qE '[:.]80\s'; then
        echo "80 端口被其他程序占用, HTTP 验证无法进行, 已中止。" >&2
        [ "$NGINX_WAS_ACTIVE" = "1" ] && systemctl start nginx || true
        exit 1
      fi
      echo ">> 为 $PANEL_DOMAIN 签发 Let's Encrypt 证书 (HTTP 验证) ..."
      "$ACME" --issue --standalone -d "$PANEL_DOMAIN" --server letsencrypt
    else
      if [ -z "${CF_Token:-}" ]; then
        read -rsp "输入 Cloudflare API Token (只需该 Zone 的 DNS 编辑权限): " CF_Token || true; echo
      fi
      if [ -z "${CF_Token:-}" ]; then echo "CF_Token 不能为空" >&2; exit 1; fi
      export CF_Token
      [ -n "${CF_Account_ID:-}" ] && export CF_Account_ID
      echo ">> 为 $PANEL_DOMAIN 签发 Let's Encrypt 证书 (CF DNS 验证) ..."
      "$ACME" --issue --dns dns_cf -d "$PANEL_DOMAIN" --server letsencrypt
    fi
    "$ACME" --install-cert -d "$PANEL_DOMAIN" \
      --fullchain-file "$CERT_FILE" --key-file "$KEY_FILE" \
      --reloadcmd "systemctl reload nginx"
    chmod 600 "$KEY_FILE"
    ( crontab -l 2>/dev/null | grep -v 'acme.sh.*--cron' || true
      echo '24 3 * * * "/root/.acme.sh"/acme.sh --cron --home "/root/.acme.sh" > /dev/null' ) | crontab -
    CERT_NOTE="Let's Encrypt (acme.sh 每日 03:24 自动续期, 续期后自动 reload nginx)"
  fi
fi

# ---------- nginx 站点 ----------
cat > /etc/nginx/conf.d/gatecore-panel.conf <<EOF
server {
    listen $PANEL_PORT ssl;
    server_name $PANEL_DOMAIN;
    ssl_certificate $CERT_FILE;
    ssl_certificate_key $KEY_FILE;
    location / {
        proxy_pass http://127.0.0.1:$GW_PANEL_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
sleep 1
if ! systemctl is-active --quiet nginx; then
  echo "nginx 启动失败, 最近日志:" >&2
  journalctl -u nginx --no-pager -n 10 >&2
  exit 1
fi

# ---------- 自检 (经域名 + 本机回环验证 TLS 与反代) ----------
CODE="$(curl -sk --resolve "$PANEL_DOMAIN:$PANEL_PORT:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$PANEL_DOMAIN:$PANEL_PORT/$PANEL_PATH/" || true)"
case "$CODE" in
  200|401) echo ">> 自检通过 (https 返回 $CODE; 401 是面板在要登录密码, 属正常)" ;;
  *) echo "警告: 自检 https 返回 $CODE (期望 200/401); 请检查 $PANEL_PORT 端口放行与证书。" >&2 ;;
esac

echo
echo "================================================================"
echo " 面板已挂到域名 (https)"
echo "================================================================"
echo " 面板网址: https://$PANEL_DOMAIN:$PANEL_PORT/$PANEL_PATH/"
echo "   账号: $PANEL_USER"
echo "   密码: $PANEL_PASS"
echo " 证书: $CERT_NOTE"
echo " 提醒: 云防火墙/安全组与本机防火墙记得放行 $PANEL_PORT/TCP。"
echo "================================================================"
