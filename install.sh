#!/usr/bin/env bash
# gatecore 一键安装（Debian / Ubuntu）
# 用法: sudo bash install.sh
# 做四件事: 装依赖(openvpn) → 部署到 /opt/gatecore → 装 systemd 服务 → 打印网关与面板凭据
set -euo pipefail

INSTALL_DIR=/opt/gatecore
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo bash install.sh" >&2
  exit 1
fi
if [ ! -e /dev/net/tun ]; then
  echo "警告: 未发现 /dev/net/tun —— 本机可能不支持 tun 设备（如 OpenVZ 容器），gatecore 将无法建立隧道。" >&2
fi

# 1) 依赖
if ! command -v openvpn >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  echo ">> 安装依赖 openvpn / python3 ..."
  apt-get update -qq
  apt-get install -y -qq openvpn python3 iproute2 curl >/dev/null
fi

# 2) 部署
echo ">> 部署到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SRC_DIR/gatecore.py" "$INSTALL_DIR/gatecore.py"

# 3) systemd 服务（开机自启；开机只起守护进程，不自动连接节点）
cat > /etc/systemd/system/gatecore.service <<'UNIT'
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
UNIT

systemctl daemon-reload
systemctl enable gatecore.service >/dev/null 2>&1 || true
systemctl restart gatecore.service

# 4) 等首次运行生成 config.json 并打印凭据
for _ in $(seq 1 20); do
  [ -f "$INSTALL_DIR/config.json" ] && break
  sleep 0.5
done
if [ ! -f "$INSTALL_DIR/config.json" ]; then
  echo "服务已安装，但 config.json 尚未生成，请稍后运行: sudo python3 -c \"import json;c=json.load(open('/opt/gatecore/config.json'));print(c)\"" >&2
  exit 1
fi

python3 - <<'PY'
import json
c = json.load(open('/opt/gatecore/config.json'))
print()
print("=" * 56)
print(" gatecore 安装完成")
print("=" * 56)
print(" 网关 (SOCKS5/HTTP): 127.0.0.1:%s" % c['gateway_port'])
print("   账号: %s" % c['gateway_user'])
print("   密码: %s" % c['gateway_pass'])
print(" 面板: http://127.0.0.1:%s/%s/" % (c.get('panel_port', 18787), c['panel_path']))
print("   账号: %s" % c['panel_user'])
print("   密码: %s" % c['panel_pass'])
print("=" * 56)
print(" 面板默认只绑回环: 在自己电脑上用 SSH 隧道打开:")
print("   ssh -L %s:127.0.0.1:%s root@<服务器IP>" % (c.get('panel_port', 18787), c.get('panel_port', 18787)))
print(" 下一步: 在面板里选一个国家连接, 或继续装 Xray 入站:")
print("   sudo bash extras/xray/install-xray.sh")
print("=" * 56)
PY
