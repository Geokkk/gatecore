#!/usr/bin/env bash
# gatecore 配套: 官方 Xray 入站一键安装 (VLESS + Reality, 免域名免证书)
# 用法: sudo bash install-xray.sh
# 前置: 已安装 gatecore (存在 /opt/gatecore/config.json), 出站会自动指向它
# 做五件事: 下载官方 Xray 并验 sha256 → 生成 UUID/Reality 密钥 → 写配置 → 装 systemd 服务 → 打印客户端链接
set -euo pipefail

XRAY_VERSION="v26.11.9"   # 故意锁定: 与作者现网验证过的版本一致 (2026-11-24 升级并逐链复验); 升级前请看上游 release notes (VMess/WS/Reality 变更) 与 docs/FULL-CHAIN.md 的升级纪律
XRAY_DIR=/opt/xray
CONF_DIR=/usr/local/etc/xray
GATECORE_CONF=/opt/gatecore/config.json
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo bash install-xray.sh" >&2
  exit 1
fi
if [ ! -f "$GATECORE_CONF" ]; then
  echo "未找到 $GATECORE_CONF —— 请先安装 gatecore: sudo bash ../../install.sh" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64)  ASSET="Xray-linux-64.zip" ;;
  aarch64) ASSET="Xray-linux-arm64-v8a.zip" ;;
  *) echo "不支持的架构: $(uname -m)" >&2; exit 1 ;;
esac

# 1) 下载官方 Xray 并验 sha256 (对照官方 .dgst)
echo ">> 下载 Xray $XRAY_VERSION ($ASSET) ..."
TMP="$(mktemp -d)"
curl -sL -o "$TMP/xray.zip" "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/$ASSET"
curl -sL -o "$TMP/xray.zip.dgst" "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/$ASSET.dgst"
MINE="$(sha256sum "$TMP/xray.zip" | awk '{print $1}')"
OFFICIAL="$(grep -oiE '[0-9a-f]{64}' "$TMP/xray.zip.dgst" | head -1)"
if [ "$MINE" != "$OFFICIAL" ]; then
  echo "sha256 校验失败, 已中止。mine=$MINE official=$OFFICIAL" >&2
  exit 1
fi
echo ">> sha256 校验通过"
mkdir -p "$XRAY_DIR"
cd "$TMP" && unzip -o -q xray.zip -d "$XRAY_DIR"
chmod +x "$XRAY_DIR/xray"

# 2) 生成凭据
UUID="$("$XRAY_DIR/xray" uuid)"
KEYS="$("$XRAY_DIR/xray" x25519)"
PRIV="$(printf '%s\n' "$KEYS" | grep -i 'private' | awk '{print $NF}')"
PUB="$(printf '%s\n' "$KEYS" | grep -i 'public' | awk '{print $NF}')"
SHORT_ID="$(openssl rand -hex 8)"
read -r GW_PORT GW_USER GW_PASS <<EOF
$(python3 -c "import json;c=json.load(open('$GATECORE_CONF'));print(c['gateway_port'], c['gateway_user'], c['gateway_pass'])")
EOF

# 3) 写配置 (先备份旧配置)
mkdir -p "$CONF_DIR"
[ -f "$CONF_DIR/config.json" ] && cp "$CONF_DIR/config.json" "$CONF_DIR/config.json.bak.$(date +%s)"
sed -e "s/__UUID__/$UUID/" \
    -e "s/__REALITY_PRIVATE_KEY__/$PRIV/" \
    -e "s/__SHORT_ID__/$SHORT_ID/" \
    -e "s/__GATECORE_PORT__/$GW_PORT/" \
    -e "s/__GW_USER__/$GW_USER/" \
    -e "s/__GW_PASS__/$GW_PASS/" \
    "$HERE/config.template.json" > "$CONF_DIR/config.json"
"$XRAY_DIR/xray" run -test -c "$CONF_DIR/config.json"

# 4) systemd 服务
cat > /etc/systemd/system/xray.service <<UNIT
[Unit]
Description=Xray (official) - gatecore inbound
After=network-online.target gatecore.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=$XRAY_DIR/xray run -c $CONF_DIR/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable xray.service >/dev/null 2>&1 || true
systemctl restart xray.service
sleep 2
if ! systemctl is-active --quiet xray.service; then
  echo "xray 启动失败, 最近日志:" >&2
  journalctl -u xray --no-pager -n 10 >&2
  exit 1
fi

# 5) 打印客户端链接
SERVER_IP="$(curl -s -m 5 https://api.ipify.org || curl -s -m 5 https://ifconfig.me || echo '<服务器IP>')"
LINK="vless://$UUID@$SERVER_IP:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.microsoft.com&fp=chrome&pbk=$PUB&sid=$SHORT_ID#gatecore-reality"
echo
echo "================================================================"
echo " Xray 入站安装完成 (VLESS + Reality, 端口 443)"
echo "================================================================"
echo " 客户端链接 (导入 v2rayNG / Streisand / Shadowrocket 等):"
echo
echo " $LINK"
echo
echo "================================================================"
echo " 链路: 客户端 → Xray(443) → gatecore 网关(127.0.0.1:$GW_PORT) → 家宽出口"
echo " 提醒: 先在 gatecore 面板里连上一个节点, 客户端才能出网。"
echo "       云厂商安全组/防火墙记得放行 443/TCP。"
echo "================================================================"
