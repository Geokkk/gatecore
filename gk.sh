#!/usr/bin/env bash
# gk — gatecore 快捷交互面板
# SSH 登录后输入 gk 即可调出: 状态 / 节点 / 连接 / 断开 / 自检 / 守候 / 链接 / 面板地址 / 服务管理
# 由 install.sh 或 deploy.sh 安装到 /usr/local/bin/gk; 普通用户运行会自动 sudo。
SELF="$(readlink -f "$0")"
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$SELF" "$@"
fi

GC_DIR="/opt/gatecore"
XRAY_CONF="/usr/local/etc/xray/config.json"
ARGO_STATE="/usr/local/etc/xray/argo.json"
SUB_FILE="/usr/local/etc/xray/subscribe.txt"

pause() { read -rp "回车返回菜单..." _ || true; }

status_line() {
  python3 - "$GC_DIR" <<'PY' 2>/dev/null || echo " 状态: (读取失败)"
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
  if [ -f /etc/nginx/conf.d/gatecore-panel.conf ]; then
    D="$(grep -oE 'server_name[[:space:]]+[^;]+' /etc/nginx/conf.d/gatecore-panel.conf | awk '{print $2}' | head -1)"
    P="$(python3 -c "import json;print(json.load(open('$GC_DIR/config.json'))['panel_path'])")"
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
    0) exit 0 ;;
    *) echo "无效选择" ;;
  esac
  pause
done
