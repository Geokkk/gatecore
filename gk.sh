#!/usr/bin/env bash
# gk — gatecore 快捷交互面板
# SSH 登录后输入 gk 即可调出: 状态 / 节点 / 连接 / 断开 / 自检 / 守候 / 链接 / 面板地址 / 服务管理 / 升级卸载
# 由 install.sh 或 deploy.sh 安装到 /usr/local/bin/gk; 普通用户运行会自动 sudo。
SELF="$(readlink -f "$0")"
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$SELF" "$@"
fi

GC_DIR="/opt/gatecore"
XRAY_DIR="/opt/xray"
XRAY_ETC="/usr/local/etc/xray"
XRAY_CONF="$XRAY_ETC/config.json"
ARGO_STATE="$XRAY_ETC/argo.json"
SUB_FILE="$XRAY_ETC/subscribe.txt"
PANEL_TLS_DIR="/usr/local/etc/gatecore-panel"
PANEL_NGINX_CONF="/etc/nginx/conf.d/gatecore-panel.conf"
REPO_URL="https://github.com/Geokkk/gatecore.git"
XRAY_VER_FALLBACK="v26.11.9"

pause() { read -rp "回车继续..." _ || true; }

confirm_yes() {
  # $1 = 提示语; 必须原样输入 yes 才继续
  local ans
  read -rp "$1 输入 yes 确认: " ans || true
  [ "$ans" = "yes" ]
}

status_line() {
  python3 - "$GC_DIR" <<'PY' 2>/dev/null || echo " 出口: (核心未安装)"
import json, os, sys
d = sys.argv[1]
def load(n, default):
    try:
        with open(os.path.join(d, n)) as f: return json.load(f)
    except Exception: return default
cfg = load("config.json", {}); st = load("state.json", {})
cur = st.get("current") or {}
mode = st.get("mode") or cfg.get("mode") or "-"
watch = st.get("watch") or cfg.get("watch") or []
ip = cur.get("ip"); cc = cur.get("cc") or ""
print(" 出口: %s %s | 模式: %s | 守候: %s" % (
    ip or "未连接", ("(" + cc + ")") if cc else "", mode,
    ",".join(watch) if watch else "无"))
PY
}

show_status() {
  echo "--- 服务 ---"
  systemctl is-active gatecore 2>/dev/null | sed 's/^/ gatecore: /'
  systemctl is-active xray 2>/dev/null | sed 's/^/ xray:     /'
  echo "--- 连接 ---"
  (cd "$GC_DIR" && python3 gatecore.py check)
  (cd "$GC_DIR" && python3 gatecore.py watch)
}

show_links() {
  local shown=0
  if [ -f "$ARGO_STATE" ]; then
    python3 - "$ARGO_STATE" <<'PY'
import json, sys, urllib.parse
try:
    st = json.load(open(sys.argv[1]))
    u = st["uuid"]; host = st["domain"]; path = urllib.parse.quote(st["path"], safe="")
    def link(addr):
        return ("vless://" + u + "@" + addr + ":443?encryption=none&security=tls"
                "&sni=" + host + "&type=ws&host=" + host + "&path=" + path + "#gatecore-argo")
    print("Argo 链接 (原版):"); print(link(host)); print()
    print("Argo 链接 (优选域名版):"); print(link(st["preferred"]))
except Exception as e:
    print("Argo 状态文件读取失败:", e)
PY
    shown=1
  fi
  if [ -f "$SUB_FILE" ]; then
    echo "--- 域名路线订阅 ---"; cat "$SUB_FILE"; shown=1
  fi
  if [ -f "$XRAY_CONF" ] && grep -q '"reality"' "$XRAY_CONF"; then
    python3 - "$XRAY_CONF" <<'PY' > /tmp/gk-reality.txt 2>/dev/null
import json, sys
cfg = json.load(open(sys.argv[1]))
for ib in cfg.get("inbounds", []):
    ss = ib.get("streamSettings", {})
    if ss.get("security") == "reality":
        rs = ss.get("realitySettings", {})
        print(ib["settings"]["clients"][0]["id"])
        print(rs.get("privateKey", ""))
        print((rs.get("shortIds") or [""])[0])
        print((rs.get("serverNames") or [""])[0])
        break
PY
    if [ -s /tmp/gk-reality.txt ]; then
      local UUID PRIV SID SNI PUB PUBIP
      UUID="$(sed -n 1p /tmp/gk-reality.txt)"; PRIV="$(sed -n 2p /tmp/gk-reality.txt)"
      SID="$(sed -n 3p /tmp/gk-reality.txt)"; SNI="$(sed -n 4p /tmp/gk-reality.txt)"
      PUB="$(/opt/xray/xray x25519 -i "$PRIV" 2>/dev/null | awk '/Public/{print $NF}')"
      PUBIP="$(curl -s -4 -m 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
      echo "Reality 链接:"
      echo "vless://${UUID}@${PUBIP}:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${SNI}&fp=chrome&pbk=${PUB}&sid=${SID}#gatecore-reality"
      shown=1
    fi
    rm -f /tmp/gk-reality.txt
  fi
  [ "$shown" = 0 ] && echo "未检测到任何入口 (xray 可能未安装)。"
}

show_panel() {
  python3 - "$GC_DIR" <<'PY'
import json, sys, os
cfg = json.load(open(os.path.join(sys.argv[1], "config.json")))
print("本机面板: http://127.0.0.1:%s/%s/" % (cfg["panel_port"], cfg["panel_path"]))
print("面板账号: %s" % cfg.get("panel_user", ""))
print("面板密码: %s" % cfg.get("panel_pass", ""))
PY
  if [ -f "$PANEL_NGINX_CONF" ]; then
    local D P
    D="$(grep -oE 'server_name[[:space:]]+[^;]+' "$PANEL_NGINX_CONF" | awk '{print $2}' | head -1)"
    P="$(python3 -c "import json;print(json.load(open('$GC_DIR/config.json'))['panel_path'])" 2>/dev/null || true)"
    [ -n "$D" ] && echo "公网面板: https://${D}:8443/${P}/"
  fi
  echo "在自己电脑上经 SSH 隧道打开: ssh -L 18787:127.0.0.1:18787 <服务器> 后访问本机面板地址"
}

service_menu() {
  while true; do
    echo "--- 服务管理 ---"
    echo " 1) 重启 gatecore"
    echo " 2) 重启 xray"
    echo " 3) gatecore 日志 (最近 30 行)"
    echo " 4) xray 状态 (最近 10 行)"
    echo " 0) 返回"
    read -rp "选择: " S || return
    case "$S" in
      1) systemctl restart gatecore && echo "gatecore 已重启" ;;
      2) systemctl restart xray && echo "xray 已重启" ;;
      3) journalctl -u gatecore -n 30 --no-pager ;;
      4) systemctl status xray --no-pager -n 10 ;;
      0) return ;;
      *) echo "无效选择" ;;
    esac
    pause
  done
}

# ---------------- 升级 / 卸载 ----------------

find_src() {
  # 找一份仓库源码 (升级用): 已有 clone 优先, 没有就现拉一份到 /tmp
  local c
  for c in /root/gatecore "$HOME/gatecore" /home/*/gatecore /opt/gatecore-src; do
    if [ -f "$c/gatecore.py" ] && [ -f "$c/install.sh" ]; then
      echo "$c"; return 0
    fi
  done
  local tmp=/tmp/gatecore-src
  rm -rf "$tmp"
  if git clone --depth 1 "$REPO_URL" "$tmp" >/dev/null 2>&1 && [ -f "$tmp/install.sh" ]; then
    echo "$tmp"; return 0
  fi
  return 1
}

upgrade_core() {
  echo "升级出口端 = 用最新版源码重跑 install.sh (config.json / state.json / 凭据全部保留, 只换程序与服务单元)。"
  if ! confirm_yes "继续?"; then echo "已取消。"; return; fi
  local SRC
  if ! SRC="$(find_src)"; then echo "找不到源码也拉不到仓库, 已中止。"; return; fi
  echo "源码: $SRC"
  if [ -d "$SRC/.git" ]; then git -C "$SRC" pull --ff-only || echo "(git pull 失败, 用现有源码继续)"; fi
  bash "$SRC/install.sh"
  echo "出口端升级完成。"
}

uninstall_core() {
  echo "卸载出口端将: 停用并删除 gatecore 服务、删除 $GC_DIR (含面板与网关凭据、连接状态)。"
  echo "进口端 (xray) 还在, 但没有出口, 所有链接都出不了网。gk 快捷命令保留 (可用来重装)。"
  if ! confirm_yes "确认卸载出口端?"; then echo "已取消。"; return; fi
  systemctl disable --now gatecore 2>/dev/null || true
  # 扫掉 gatecore 拉起的 openvpn (命令行带 gatecore 路径签名的才动)
  local pid
  for pid in $(pgrep openvpn 2>/dev/null || true); do
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q gatecore; then kill "$pid" 2>/dev/null || true; fi
  done
  rm -f /etc/systemd/system/gatecore.service
  rm -rf "$GC_DIR"
  systemctl daemon-reload
  echo "出口端已卸载。"
}

upgrade_ingress() {
  local SRC="" PIN CUR
  SRC="$(find_src || true)"
  PIN=""
  [ -n "$SRC" ] && PIN="$(grep -oE 'XRAY_VER="[^"]+"' "$SRC/extras/xray/install-xray.sh" 2>/dev/null | head -1 | cut -d'"' -f2 || true)"
  [ -z "$PIN" ] && PIN="$XRAY_VER_FALLBACK"
  CUR=""
  [ -x "$XRAY_DIR/xray" ] && CUR="v$("$XRAY_DIR/xray" version 2>/dev/null | awk 'NR==1{print $2}')"
  echo "进口端升级 = 只换 Xray 二进制到仓库锁定版 $PIN (当前: ${CUR:-未安装}), 配置一字不动, 换前用现有配置冷测、失败自动回滚。"
  if [ "$CUR" = "$PIN" ]; then echo "已是锁定版, 无需升级。"; return; fi
  if [ ! -f "$XRAY_CONF" ]; then echo "未检测到 xray 配置, 请先装入口路线。"; return; fi
  if ! confirm_yes "继续?"; then echo "已取消。"; return; fi
  local WORK; WORK="$(mktemp -d)"
  local ZIP="Xray-linux-64.zip"
  curl -fL -m 300 -o "$WORK/$ZIP" "https://github.com/XTLS/Xray-core/releases/download/$PIN/$ZIP" || { echo "下载失败, 已中止。"; rm -rf "$WORK"; return; }
  curl -fL -m 60 -o "$WORK/$ZIP.dgst" "https://github.com/XTLS/Xray-core/releases/download/$PIN/$ZIP.dgst" || { echo "校验文件下载失败, 已中止。"; rm -rf "$WORK"; return; }
  local WANT GOT
  WANT="$(grep -oE '[0-9a-f]{64}' "$WORK/$ZIP.dgst" | head -1)"
  GOT="$(sha256sum "$WORK/$ZIP" | awk '{print $1}')"
  if [ -z "$WANT" ] || [ "$WANT" != "$GOT" ]; then echo "sha256 校验不符, 已中止。"; rm -rf "$WORK"; return; fi
  (cd "$WORK" && unzip -o -q "$ZIP" xray) || { echo "解压失败, 已中止。"; rm -rf "$WORK"; return; }
  if ! "$WORK/xray" run -test -config "$XRAY_CONF" >/dev/null 2>&1; then
    echo "新二进制与现有配置冷测不通过, 已中止 (旧版未动)。"; rm -rf "$WORK"; return
  fi
  local BAK="$XRAY_DIR/xray.bak-$(date +%Y%m%d%H%M%S)"
  cp -a "$XRAY_DIR/xray" "$BAK"
  install -m 0755 "$WORK/xray" "$XRAY_DIR/xray"
  systemctl restart xray
  sleep 1
  if systemctl is-active --quiet xray; then
    echo "进口端已升级到 $PIN, 服务正常。"
  else
    echo "重启后服务异常, 正在回滚..."
    cp -a "$BAK" "$XRAY_DIR/xray"
    systemctl restart xray || true
    echo "已回滚到旧二进制, 请检查日志。"
  fi
  rm -rf "$WORK"
}

uninstall_ingress() {
  echo "卸载进口端将: 停用并删除 xray 服务、删除 $XRAY_DIR 与 $XRAY_ETC (含入口配置、证书、订阅、Argo 状态)、删除面板域名站点 (nginx 配置与面板证书)。"
  echo "出口端 (gatecore) 还在运行, 但没有入口连得进来。Cloudflare 控制台里的隧道绑定 (如 argo 主机名) 需你自己去删。"
  if ! confirm_yes "确认卸载进口端?"; then echo "已取消。"; return; fi
  systemctl disable --now xray 2>/dev/null || true
  rm -f /etc/systemd/system/xray.service
  rm -rf "$XRAY_DIR" "$XRAY_ETC"
  if [ -f "$PANEL_NGINX_CONF" ]; then
    rm -f "$PANEL_NGINX_CONF"
    rm -rf "$PANEL_TLS_DIR"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
  fi
  systemctl daemon-reload
  echo "进口端已卸载。"
}

uninstall_all() {
  echo "全部卸载 = 出口端 + 进口端 + gk 快捷命令本身, 一并清除。"
  echo "不处理: 防火墙规则、cloudflared、fail2ban、你自己的仓库 clone、Cloudflare 控制台里的绑定。"
  if ! confirm_yes "确认全部卸载?"; then echo "已取消。"; return; fi
  systemctl disable --now xray 2>/dev/null || true
  systemctl disable --now gatecore 2>/dev/null || true
  local pid
  for pid in $(pgrep openvpn 2>/dev/null || true); do
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q gatecore; then kill "$pid" 2>/dev/null || true; fi
  done
  rm -f /etc/systemd/system/xray.service /etc/systemd/system/gatecore.service
  rm -rf "$XRAY_DIR" "$XRAY_ETC" "$GC_DIR"
  if [ -f "$PANEL_NGINX_CONF" ]; then
    rm -f "$PANEL_NGINX_CONF"; rm -rf "$PANEL_TLS_DIR"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
  fi
  systemctl daemon-reload
  rm -f /usr/local/bin/gk
  echo "全部卸载完成。重装: git clone $REPO_URL 后 sudo bash deploy.sh"
  exit 0
}

maintain_menu() {
  while true; do
    echo "--- 升级 / 卸载 ---"
    echo " 1) 出口端 (gatecore 本体: 网关/面板/家宽出口)"
    echo " 2) 进口端 (xray 入口: Reality/域名/Argo + 面板站点)"
    echo " 3) 全部卸载"
    echo " 0) 返回主菜单"
    read -rp "选择: " S || return
    case "$S" in
      1)
        echo "出口端:  1) 升级   2) 卸载   0) 返回"
        read -rp "选择: " T || true
        case "$T" in
          1) upgrade_core ;;
          2) uninstall_core ;;
          *) echo "已返回。" ;;
        esac
        ;;
      2)
        echo "进口端:  1) 升级   2) 卸载   0) 返回"
        read -rp "选择: " T || true
        case "$T" in
          1) upgrade_ingress ;;
          2) uninstall_ingress ;;
          *) echo "已返回。" ;;
        esac
        ;;
      3) uninstall_all ;;
      0) return ;;
      *) echo "无效选择" ;;
    esac
    pause
  done
}

# ---------------- 主菜单 ----------------

while true; do
  echo
  echo "================================================"
  echo " gatecore 快捷面板 (gk)"
  systemctl is-active --quiet gatecore 2>/dev/null && echo " 服务: 运行中" || echo " 服务: 未运行"
  status_line
  echo "------------------------------------------------"
  echo " 1) 查看状态      2) 节点排行 (前10)"
  echo " 3) 按国家看节点  4) 连接节点"
  echo " 5) 断开连接      6) 出口自检"
  echo " 7) 守候名单      8) 客户端链接"
  echo " 9) Web 面板地址  10) 服务管理"
  echo " 11) 升级 / 卸载"
  echo " 0) 退出"
  echo "================================================"
  read -rp "选择: " C || exit 0
  case "$C" in
    1) show_status ;;
    2) (cd "$GC_DIR" && python3 gatecore.py list) ;;
    3) read -rp "国家代码 (如 JP): " CC || true
       (cd "$GC_DIR" && python3 gatecore.py list "${CC:-JP}") ;;
    4) read -rp "国家代码 [回车=JP]: " CC || true
       (cd "$GC_DIR" && python3 gatecore.py connect "${CC:-JP}") ;;
    5) (cd "$GC_DIR" && python3 gatecore.py disconnect) && echo "已断开" ;;
    6) (cd "$GC_DIR" && python3 gatecore.py check) ;;
    7) (cd "$GC_DIR" && python3 gatecore.py watch) ;;
    8) show_links ;;
    9) show_panel ;;
    10) service_menu ;;
    11) maintain_menu ;;
    0) exit 0 ;;
    *) echo "无效选择" ;;
  esac
  pause
done
