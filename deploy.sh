#!/usr/bin/env bash
# gatecore 一键部署总入口
# 交互 (推荐): sudo bash deploy.sh
#   全流程向导: 装本体 → 选入口路线 → 问是否加装 Argo → 问是否把面板挂域名,
#   每一步回车即确认默认, 最后打印客户端链接与面板网址。
# 非交互 (单步, 保持旧行为): sudo MODE=<路线> bash deploy.sh
#   MODE=reality | domain-http | domain-cf | selfsigned | argo
#   例: sudo MODE=domain-http DOMAIN=proxy.example.com bash deploy.sh
#       sudo MODE=argo TUNNEL_DOMAIN=argo.example.com bash deploy.sh
#       sudo MODE=reality PANEL_DOMAIN=panel.example.com bash deploy.sh   # 只跑到入口为止
# 入口路线 (四选一, 核心都是同一个 gatecore):
#   1 reality      VLESS+Reality, 免域名免证书
#   2 domain-http  VMess+WS+TLS, 域名 + acme HTTP 验证 (需 80 端口)
#   3 domain-cf    VMess+WS+TLS, 域名 + acme Cloudflare DNS 验证 (需 API Token, 不用开 80)
#   4 selfsigned   VMess+WS+TLS, 自签证书 (零域名零等待, 客户端需开启"允许不安全")
# 可选加装:
#   argo   Argo 备用入口: VLESS+WS 回环后端 + Cloudflare 隧道 + 优选域名链接
#   panel  面板挂域名: nginx 反代回环面板 + https, 打印 https://域名:8443/<秘密路径>/
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${MODE:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行: sudo bash deploy.sh" >&2
  exit 1
fi

# 前置: gatecore 本体 (所有路线共用)
if [ ! -f /opt/gatecore/config.json ]; then
  echo ">> 未检测到 gatecore, 先安装本体 ..."
  bash "$HERE/install.sh"
fi

run_route() {
  case "$1" in
    reality)
      bash "$HERE/extras/xray/install-xray.sh"
      ;;
    domain-http|domain-cf|selfsigned)
      # 域名类路线复用 install-xray.sh 下载校验过的 Xray 二进制与服务单元;
      # 它会先写一份 Reality 配置, 随后被 install-domain.sh 替换 (旧配置自动备份)。
      if [ ! -x /opt/xray/xray ]; then
        echo ">> 先装 Xray 二进制 (经由 Reality 脚本) ..."
        bash "$HERE/extras/xray/install-xray.sh"
      fi
      case "$1" in
        domain-http) CERT_MODE=http ;;
        domain-cf)   CERT_MODE=cf ;;
        selfsigned)  CERT_MODE=self ;;
      esac
      export CERT_MODE
      bash "$HERE/extras/domain/install-domain.sh"
      ;;
    argo)
      bash "$HERE/extras/argo/install-argo.sh"
      ;;
    *)
      echo "未知路线: $1" >&2
      return 1
      ;;
  esac
}

# ---------- 非交互: 单步直达 (旧行为) ----------
if [ -n "$MODE" ]; then
  case "$MODE" in
    reality|domain-http|domain-cf|selfsigned|argo)
      run_route "$MODE"
      ;;
    *)
      echo "未知 MODE: $MODE (可选 reality | domain-http | domain-cf | selfsigned | argo)" >&2
      exit 1
      ;;
  esac
  exit 0
fi

# ---------- 交互: 全流程向导 ----------
echo
echo "================================================================"
echo " gatecore 一键部署向导"
echo " 流程: 选入口 → 加装 Argo (可选) → 面板挂域名 (可选)"
echo " 每一步回车 = 接受默认/继续, 输入 n = 跳过该步"
echo "================================================================"
echo " 1) Reality 路线        免域名免证书 (最省事)"
echo " 2) 域名 + HTTP 验证    VMess+WS+TLS, acme 签证书, 需放行 80"
echo " 3) 域名 + CF DNS 验证  VMess+WS+TLS, Cloudflare Token 签证书, 不用开 80"
echo " 4) 自签证书路线        VMess+WS+TLS, 零域名, 客户端需允许不安全连接"
echo " 0) 退出"
echo "================================================================"
read -rp "输入序号 [回车=1]: " CHOICE || true
CHOICE="${CHOICE:-1}"
case "$CHOICE" in
  1) MODE=reality ;;
  2) MODE=domain-http ;;
  3) MODE=domain-cf ;;
  4) MODE=selfsigned ;;
  0) echo "已退出。"; exit 0 ;;
  *) echo "无效序号: $CHOICE" >&2; exit 1 ;;
esac

echo
echo ">> 第 1 步: 安装入口 ($MODE) ..."
run_route "$MODE"

echo
read -rp ">> 第 2 步: 加装 Argo 备用入口 (VLESS+WS + CF 隧道)? [回车=加装 / n=跳过]: " ANS || true
case "$ANS" in
  n|N) echo "跳过 Argo。" ;;
  *) bash "$HERE/extras/argo/install-argo.sh" ;;
esac

echo
read -rp ">> 第 3 步: 把管理面板挂到域名 (https 网址)? [回车=挂 / n=跳过]: " ANS || true
case "$ANS" in
  n|N) echo "跳过面板挂域名 (面板仍可经 SSH 隧道打开)。" ;;
  *) bash "$HERE/extras/panel/install-panel.sh" ;;
esac

echo
echo "================================================================"
echo " 部署向导结束。上面依次打印了: 入口客户端链接 / Argo 链接 / 面板网址。"
echo " 提醒: 先在面板里连上一个节点, 客户端才能出网;"
echo "       本机防火墙/云安全组记得放行入口端口 (443) 与面板端口 (8443)。"
echo "================================================================"
