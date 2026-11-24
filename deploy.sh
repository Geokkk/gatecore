#!/usr/bin/env bash
# gatecore 一键部署总入口: 交互菜单选路线, 也支持非交互环境变量
# 交互:   sudo bash deploy.sh
# 非交互: sudo MODE=reality bash deploy.sh
#         sudo MODE=domain-http DOMAIN=proxy.example.com bash deploy.sh
#         sudo MODE=domain-cf   DOMAIN=proxy.example.com CF_Token=<token> bash deploy.sh
#         sudo MODE=selfsigned  DOMAIN=gatecore.local bash deploy.sh
#         sudo MODE=argo        TUNNEL_DOMAIN=argo.example.com bash deploy.sh
# 路线 (入口四选一, 核心都是同一个 gatecore):
#   1 reality      VLESS+Reality, 免域名免证书
#   2 domain-http  VMess+WS+TLS, 域名 + acme HTTP 验证 (需 80 端口)
#   3 domain-cf    VMess+WS+TLS, 域名 + acme Cloudflare DNS 验证 (需 API Token, 不用开 80)
#   4 selfsigned   VMess+WS+TLS, 自签证书 (零域名零等待, 客户端需开启"允许不安全")
# 加装 (先装好任一入口路线后):
#   5 argo         Argo 备用入口: VLESS+WS 回环后端 + Cloudflare 隧道 + 优选域名链接
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${MODE:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo bash deploy.sh" >&2
  exit 1
fi

# 前置 1: gatecore 本体 (所有路线共用)
if [ ! -f /opt/gatecore/config.json ]; then
  echo ">> 未检测到 gatecore, 先安装本体 ..."
  bash "$HERE/install.sh"
fi

# 选路线
if [ -z "$MODE" ]; then
  echo
  echo "================================================================"
  echo " gatecore 一键部署 - 选择入口路线"
  echo "================================================================"
  echo " 1) Reality 路线        免域名免证书 (最省事)"
  echo " 2) 域名 + HTTP 验证    VMess+WS+TLS, acme 签证书, 需放行 80"
  echo " 3) 域名 + CF DNS 验证  VMess+WS+TLS, Cloudflare Token 签证书, 不用开 80"
  echo " 4) 自签证书路线        VMess+WS+TLS, 零域名, 客户端需允许不安全连接"
  echo " 5) Argo 备用入口       VLESS+WS + CF 隧道 (加装, 需先装好任一入口)"
  echo " 0) 退出"
  echo "================================================================"
  read -rp "输入序号 [1]: " CHOICE || true
  CHOICE="${CHOICE:-1}"
  case "$CHOICE" in
    1) MODE=reality ;;
    2) MODE=domain-http ;;
    3) MODE=domain-cf ;;
    4) MODE=selfsigned ;;
    5) MODE=argo ;;
    0) echo "已退出。"; exit 0 ;;
    *) echo "无效序号: $CHOICE" >&2; exit 1 ;;
  esac
fi

case "$MODE" in
  argo)
    exec bash "$HERE/extras/argo/install-argo.sh"
    ;;
  reality)
    exec bash "$HERE/extras/xray/install-xray.sh"
    ;;
  domain-http|domain-cf|selfsigned)
    # 域名类路线复用 install-xray.sh 下载校验过的 Xray 二进制与服务单元;
    # 它会先写一份 Reality 配置, 随后被 install-domain.sh 替换 (旧配置自动备份)。
    if [ ! -x /opt/xray/xray ]; then
      echo ">> 先装 Xray 二进制 (经由 Reality 脚本) ..."
      bash "$HERE/extras/xray/install-xray.sh"
    fi
    case "$MODE" in
      domain-http) CERT_MODE=http ;;
      domain-cf)   CERT_MODE=cf ;;
      selfsigned)  CERT_MODE=self ;;
    esac
    export CERT_MODE
    exec bash "$HERE/extras/domain/install-domain.sh"
    ;;
  *)
    echo "未知 MODE: $MODE (可选 reality | domain-http | domain-cf | selfsigned | argo)" >&2
    exit 1
    ;;
esac
