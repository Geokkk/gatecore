#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gatecore — 自研 VPN Gate 家宽出口核心 v1（纯 Python 标准库，零依赖）

功能：拉取 VPN Gate 公开节点列表 → 探测/排序（历史数据加权）→ OpenVPN 隧道管理
     → 本地混合网关（SOCKS5 + HTTP，同口，出站绑定 tun 设备）
     → Web 控制面板（状态/按国家筛选/节点实测/一键切换/模式选择）
     → 健康检查与自动切换（智能自动 / 固定国家 / 固定 IP 三种模式）

设计原则：全源码可审计；状态全在 DATA_DIR 的 JSON 文件里；不碰系统其它服务。
运行：sudo python3 gatecore.py serve   （tun 与 SO_BINDTODEVICE 需要 root）
"""
import base64, csv, hashlib, io, ipaddress, json, math, os, random, re
import signal, socket, ssl, struct, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("GATECORE_DATA", BASE)
API_URLS = [
    "https://www.vpngate.net/api/iphone/",   # 官方主源（https）
    "http://www.vpngate.net/api/iphone/",    # 官方备用路径（http）
    # 第三方镜像：GitHub + jsDelivr 转存的官方 CSV（每小时更新）。官方每次只返回
    # 全部志愿者的一小部分子集，各快照差异很大，多源并集能显著扩大节点池。
    "https://cdn.jsdelivr.net/gh/GeorgeXie2333/vpngate-list-mirror@latest/data/vpngate.csv",
    "https://fastly.jsdelivr.net/gh/GeorgeXie2333/vpngate-list-mirror@latest/data/vpngate.csv",
]
POOL_TTL = 172800   # 累积池：保留最后在线 48 小时内的节点
POOL_MAX = 800      # 累积池上限，超出丢最久未见的
TUN_DEV = os.environ.get("GATECORE_TUN", "geotun0")
EXIT_CHECK_URL = "http://ip-api.com/json/?fields=status,query,country,countryCode,isp,org"

def p(*names): return os.path.join(DATA, *names)

# ---------------------------------------------------------------- config/state
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=1)
    # 保留属主与权限: 原文件在则跟随原文件, 否则跟随所在目录。
    # 否则 root 保存会把属主翻转, 低权限进程 (如守候巡查) 读不到文件时
    # 可能误判"配置不存在"而重建凭据 (2026-11-22 实际发生过)。
    try:
        ref = path if os.path.exists(path) else os.path.dirname(path)
        st = os.stat(ref)
        os.chown(tmp, st.st_uid, st.st_gid)
        if os.path.exists(path): os.chmod(tmp, st.st_mode & 0o7777)
    except Exception: pass
    os.replace(tmp, path)

def get_config():
    path = p("config.json")
    if not os.path.exists(path):
        cfg = {
            "gateway_port": int(os.environ.get("GATECORE_PORT", "18500")),
            "gateway_user": "gate",
            "gateway_pass": hashlib.sha256(os.urandom(32)).hexdigest()[:16],
            "panel_port": int(os.environ.get("GATECORE_PANEL_PORT", "18787")),
            "panel_bind": os.environ.get("GATECORE_PANEL_BIND", "127.0.0.1"),
            "panel_path": hashlib.sha256(os.urandom(32)).hexdigest()[:10],
            "panel_user": "admin",
            "panel_pass": hashlib.sha256(os.urandom(32)).hexdigest()[:16],
        }
        save_json(path, cfg)
        try: os.chmod(path, 0o600)
        except Exception: pass
        print("[config] 已生成 config.json（网关/面板凭据在内，0600）", flush=True)
        return cfg
    cfg = load_json(path, None)
    if cfg is None:
        # 文件存在却读不到/解析失败 (如权限被改、内容损坏): 宁可报错退出,
        # 绝不静默重建一份新凭据——那会让网关与客户端凭据悄无声息地分家。
        raise SystemExit("[config] config.json 存在但无法读取或解析, 拒绝静默重建: " + path)
    return cfg

def get_state():
    return load_json(p("state.json"), {"mode": "manual", "cc": "", "current": None,
                                       "fail_count": 0, "events": [],
                                       "watch": [], "watch_interval": 900, "watch_hits": [],
                                       "favorites": [], "fav_status": {}})
def set_state(st): save_json(p("state.json"), st)

def add_event(msg):
    st = get_state()
    st.setdefault("events", []).append({"t": time.strftime("%H:%M:%S"), "msg": msg})
    st["events"] = st["events"][-50:]
    set_state(st)
    print("[event]", msg, flush=True)

# ---------------------------------------------------------------- node list
def _fetch_one(url):
    """抓一个源的 CSV 并解析成节点行；失败抛异常，由调用方容忍。"""
    req = urllib.request.Request(url, headers={"User-Agent": "gatecore/1.0"})
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    lines = [l for l in raw.splitlines() if l and not l.startswith("*")]
    if len(lines) < 2: raise RuntimeError("API 返回异常")
    hdr = lines[0].lstrip("#").split(",")
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[1:])), fieldnames=hdr):
        try:
            cfg_b64 = r.get("OpenVPN_ConfigData_Base64") or ""
            if not cfg_b64: continue
            rows.append({
                "host": r["HostName"], "ip": r["IP"],
                "hostname": r["HostName"], "operator": r.get("Operator", ""),
                "score": int(r["Score"]), "ping": int(r["Ping"]),
                "speed": int(r["Speed"]), "cc": r["CountryShort"],
                "country": r["CountryLong"], "sessions": int(r["NumVpnSessions"]),
                "uptime": int(r["Uptime"]), "cfg": cfg_b64,
            })
        except Exception: continue
    return rows

def fetch_nodes(force=False):
    """多源抓取 + 累积池。官方每次只返回全部志愿者的一小部分子集，各源、各时刻的
    快照差异很大，所以抓取结果按 IP 并入持久池（nodes.json）：见过的节点保留
    first_seen/last_seen，只淘汰 48 小时未再出现的。返回整个池（新鲜的在前）。
    全部源失败时沿用旧池；无池可用才抛异常。"""
    cache = p("nodes.json")
    pool = load_json(cache, [])
    if not isinstance(pool, list): pool = []
    if not force and pool and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 900:
        return pool
    now = int(time.time())
    results = {}
    def grab(url):
        try: results[url] = _fetch_one(url)
        except Exception as ex: results[url] = ex
    threads = [threading.Thread(target=grab, args=(u,)) for u in API_URLS]
    for t in threads: t.start()
    for t in threads: t.join()
    fresh = []
    for u in API_URLS:
        r = results.get(u)
        if isinstance(r, list): fresh.extend(r)
        else: print("[fetch] 源失败 %s: %s" % (u, r), flush=True)
    if not fresh:
        if pool:
            print("[fetch] 全部源失败，沿用累积池（%d 个）" % len(pool), flush=True)
            return pool
        raise RuntimeError("API 返回异常（全部源失败）")
    by_ip = {}
    for n in pool:
        n.setdefault("first_seen", now); n.setdefault("last_seen", now)
        by_ip[n["ip"]] = n
    for row in fresh:
        ip = row["ip"]
        if ip in by_ip:
            keep = by_ip[ip]; fs = keep.get("first_seen", now)
            keep.update(row); keep["first_seen"] = fs; keep["last_seen"] = now
        else:
            row["first_seen"] = now; row["last_seen"] = now
            by_ip[ip] = row
    merged = [n for n in by_ip.values() if now - n.get("last_seen", now) <= POOL_TTL]
    merged.sort(key=lambda n: -n.get("last_seen", now))
    merged = merged[:POOL_MAX]
    save_json(cache, merged)
    return merged

def parse_remote(cfg_text):
    """从 .ovpn 文本里取 (remote_host, port, proto, has_tls_auth)"""
    host = port = None; proto = "udp"; tls = False
    for line in cfg_text.splitlines():
        s = line.strip()
        if s.startswith("remote "):
            parts = s.split()
            if len(parts) >= 2: host = parts[1]
            if len(parts) >= 3:
                try: port = int(parts[2])
                except Exception: pass
        elif s.startswith("proto "):
            proto = s.split()[1].lower()
        elif s.startswith(("tls-auth", "tls-crypt")):
            tls = True
    return host, port, proto, tls

def node_key(n): return "%s|%s" % (n["ip"], n.get("port") or "")

def enrich(nodes):
    """解码配置，补上 port/proto/tls 字段（缓存解码结果）"""
    cache = load_json(p("endpoints.json"), {})
    dirty = False
    for n in nodes:
        k = n["ip"]
        if k in cache:
            n.update(cache[k]); continue
        try:
            text = base64.b64decode(n["cfg"]).decode("utf-8", "replace")
            host, port, proto, tls = parse_remote(text)
            info = {"port": port, "proto": proto, "tls": tls, "remote_host": host}
        except Exception:
            info = {"port": None, "proto": "udp", "tls": False, "remote_host": None}
        cache[k] = info; n.update(info); dirty = True
    if dirty: save_json(p("endpoints.json"), cache)
    return classify_nodes(nodes)

# ---------------------------------------------------------------- residential classification
# 分类逻辑（多信号、可解释）：
#   项目/学术 → 非家宽：hostname 以 public-vpn- 开头，或 operator 含 "Academic Use Only"/"Nobori"
#   机房 → 非家宽：ip-api 的 isp/org/as 命中数据中心/云/大学关键词
#   家宽：hostname 为 vpn<纯数字>（志愿者匿名主机名），或 operator 形如 "XXX's owner"（个人桌面）
#   其余 → 未知（不奖不罚）
DC_KEYWORDS = ["softether", "university", "academic", "data center", "datacenter",
               "hosting", "cloud", "amazon", "google llc", "microsoft", "oracle",
               "digitalocean", "linode", "vultr", "hetzner", "ovh", "contabo",
               "leaseweb", "choopa", "m247", "datacamp", "server"]
PREFER_RES = os.environ.get("GATECORE_PREFER_RES", "1") != "0"

def _isp_lookup(ips):
    """ip-api 批量查归属（isp/org/as），结果缓存 24h 到 isp.json"""
    cache = load_json(p("isp.json"), {})
    now = int(time.time())
    todo = [ip for ip in ips if ip not in cache or now - cache[ip].get("ts", 0) > 86400]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        try:
            body = json.dumps([{"query": ip, "fields": "status,query,isp,org,as"} for ip in chunk]).encode()
            req = urllib.request.Request("http://ip-api.com/batch", data=body,
                                         headers={"Content-Type": "application/json", "User-Agent": "gatecore/1.0"})
            for item in json.loads(urllib.request.urlopen(req, timeout=25).read().decode()):
                if item.get("status") == "success":
                    cache[item["query"]] = {"isp": item.get("isp", ""), "org": item.get("org", ""),
                                            "as": item.get("as", ""), "ts": now}
        except Exception as ex:
            print("[classify] ip-api 查询失败:", ex, flush=True)
    save_json(p("isp.json"), cache)
    return cache

def classify_nodes(nodes):
    """给每个节点打上 residential ∈ {True, False, None} + 归属信息，并同步家宽清单"""
    isp = _isp_lookup([n["ip"] for n in nodes])
    for n in nodes:
        info = isp.get(n["ip"], {})
        n["isp"], n["org"], n["as"] = info.get("isp", ""), info.get("org", ""), info.get("as", "")
        hn = (n.get("hostname") or "").lower(); op = (n.get("operator") or "").lower()
        blob = (n["isp"] + " " + n["org"] + " " + n["as"]).lower()
        if hn.startswith("public-vpn-") or "academic use only" in op or "nobori" in op:
            n["residential"], n["cls_reason"] = False, "项目方学术服务器"
        elif any(k in blob for k in DC_KEYWORDS):
            n["residential"], n["cls_reason"] = False, "机房/云归属"
        elif re.fullmatch(r"vpn\d+", hn) or "'s owner" in op:
            n["residential"], n["cls_reason"] = True, "志愿者个人设备"
        else:
            n["residential"], n["cls_reason"] = None, "信号不足"
    sync_residential(nodes)
    return nodes

def sync_residential(nodes):
    """把真正的家宽节点同步成独立清单 residential.json（按评分排序）"""
    hist = get_history()
    res = []
    for n in nodes:
        if n.get("residential") is True:
            res.append({"ip": n["ip"], "cc": n["cc"], "country": n["country"],
                        "isp": n.get("isp", ""), "org": n.get("org", ""),
                        "hostname": n.get("hostname", ""), "operator": n.get("operator", ""),
                        "proto": n.get("proto", ""), "port": n.get("port"),
                        "score": n["score"], "ping": n["ping"],
                        "speed_mbps": round(n["speed"] / 1e6, 1), "sessions": n["sessions"],
                        "rank": rank_score(n, hist)})
    res.sort(key=lambda x: -x["rank"])
    save_json(p("residential.json"), {"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                                      "count": len(res), "nodes": res})
    return res

# ---------------------------------------------------------------- history & ranking
def get_history(): return load_json(p("history.json"), {})
def hist_update(key, ok, ms=None):
    h = get_history(); e = h.get(key, {"ok": 0, "fail": 0, "ms": None, "last_ok": 0})
    if ok:
        e["ok"] += 1; e["last_ok"] = int(time.time())
        e["ms"] = ms if e["ms"] is None else round(e["ms"] * 0.6 + ms * 0.4, 1)
    else: e["fail"] += 1
    h[key] = e; save_json(p("history.json"), h)

def hist_latency(key, ms):
    """记录某节点的经隧道实测时延（EWMA 平滑），供排序小权重参考。"""
    if not key or not ms: return
    h = get_history(); e = h.get(key, {"ok": 0, "fail": 0, "ms": None, "last_ok": 0})
    e["lat"] = int(ms) if not e.get("lat") else int(round(0.5 * e["lat"] + 0.5 * ms))
    h[key] = e; save_json(p("history.json"), h)

def rank_score(n, hist):
    """可解释的排序分：API 评分/速度/延迟 + 历史成功率加权（越用越准）"""
    s = math.log10(max(n["score"], 1)) * 100
    s += min(n["speed"] / 1e6, 300) / 3.0
    s -= n["ping"] * 0.5
    if PREFER_RES:
        if n.get("residential") is True: s += 70
        elif n.get("residential") is False: s -= 50
    if str(n.get("proto", "")).startswith("udp"): s += 15  # UDP 节点免 TCP-over-TCP 闷死
    e = hist.get(node_key(n))
    if e:
        total = e["ok"] + e["fail"]
        if total: s += (e["ok"] / total) * 60 - (e["fail"] / total) * 30
        if e["ms"] is not None: s -= min(e["ms"], 3000) * 0.01
        if e.get("lat"): s += max(-20.0, min(20.0, (110.0 - e["lat"]) * 0.2))  # 经隧道实测时延：±20 封顶，只做同档微调
        if time.time() - e.get("last_ok", 0) > 86400 and e["ok"]: s -= 10
    return round(s, 1)

def rank(nodes, cc=None):
    hist = get_history()
    out = [n for n in nodes if (not cc or n["cc"] == cc)]
    for n in out: n["rank"] = rank_score(n, hist)
    return sorted(out, key=lambda x: -x["rank"])

# ---------------------------------------------------------------- probing
def probe_node(n, timeout=3.0):
    """轻量实测：TCP 配置→TCP 连接计时；UDP 配置→OpenVPN hard-reset 握手计时。
    返回 (ok, ms, note)。tls-auth 的 UDP 节点无法裸探，返回 (None, None, 'tls')。"""
    host = n.get("remote_host") or n["ip"]; port = n.get("port")
    if not port: return (None, None, "noport")
    try: addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    except Exception: addr = (n["ip"], port)
    if str(n.get("proto", "")).startswith("tcp"):
        t0 = time.time()
        try:
            s = socket.create_connection(addr, timeout=timeout); s.close()
            return (True, round((time.time() - t0) * 1000), "tcp")
        except Exception as ex: return (False, None, "tcp:%s" % type(ex).__name__)
    if n.get("tls"): return (None, None, "tls")
    pkt = bytes([0x38]) + os.urandom(8) + b"\x00" + b"\x00\x00\x00\x00"
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
        s.sendto(pkt, addr)
        data, _ = s.recvfrom(64); s.close()
        if data: return (True, round((time.time() - t0) * 1000), "udp")
        return (False, None, "udp-empty")
    except Exception as ex: return (False, None, "udp:%s" % type(ex).__name__)

# ---------------------------------------------------------------- openvpn manager
class Tunnel:
    def __init__(self):
        self.proc = None; self.lock = threading.Lock()
    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()
        self.proc = None
        # 跨进程兜底：CLI 与守护进程是两个进程，self.proc 够不着对方拉起的 openvpn，
        # 而对端死后 openvpn 会软重启死循环、一直占着 tun 设备，后续连接全灭。
        # pid 文件实测为空、不可靠；按命令行特征（本核心专属的 config 路径）扫杀才稳，
        # 其它 OpenVPN 实例用的是别的 config 路径，不会误伤。
        try:
            pat = "openvpn --config " + p("current.ovpn")
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=5).stdout.split()
            for pid_s in out:
                try: os.kill(int(pid_s), signal.SIGTERM)
                except Exception: pass
            for _ in range(16):
                if not os.path.exists("/sys/class/net/" + TUN_DEV): break
                time.sleep(0.5)
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=5).stdout.split()
            for pid_s in out:
                try: os.kill(int(pid_s), signal.SIGKILL)
                except Exception: pass
        except Exception: pass
        for _ in range(10):
            if not os.path.exists("/sys/class/net/" + TUN_DEV): break
            time.sleep(0.5)
    def start(self, n, timeout=20):
        self.stop()
        text = base64.b64decode(n["cfg"]).decode("utf-8", "replace")
        lines = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(("dev ", "dev-type", "redirect-gateway", "route ", "dhcp-option")): continue
            lines.append(line)
        lines += ["dev " + TUN_DEV, "dev-type tun", "route-nopull",
                  "auth-nocache", "server-poll-timeout 10", "connect-timeout 10",
                  "connect-retry 1 2", "mssfix 1360", "verb 3"]
        cfg_path = p("current.ovpn")
        with open(cfg_path, "w", encoding="utf-8") as f: f.write("\n".join(lines) + "\n")
        os.chmod(cfg_path, 0o600)
        log = open(p("openvpn.log"), "wb")  # 每轮截断：否则上一轮的 Completed 标记会让就绪判断假阳性
        self.proc = subprocess.Popen(
            ["openvpn", "--config", cfg_path, "--writepid", p("openvpn.pid")],
            stdout=log, stderr=log)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None: return False
            try:
                with open(p("openvpn.log"), "rb") as f:
                    ready = b"Initialization Sequence Completed" in f.read()
                if ready and os.path.exists("/sys/class/net/" + TUN_DEV): return True
            except Exception: pass
            time.sleep(0.7)
        return False

TUNNEL = Tunnel()

# ---------------------------------------------------------------- DNS via tunnel device
def dns_query_a(hostname, server="1.1.1.1", timeout=4.0):
    """经 tun 设备发 DNS 查询取 A 记录（避免本机解析泄漏/污染），失败返回 None"""
    tid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for part in hostname.rstrip(".").split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(timeout)
    try:
        s.setsockopt(socket.SOL_SOCKET, 25, TUN_DEV.encode() + b"\x00")  # SO_BINDTODEVICE
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(2048)
    except Exception: return None
    finally: s.close()
    try:
        ancount = struct.unpack(">H", data[6:8])[0]
        off = 12
        def skip_name(off):
            while True:
                ln = data[off]
                if ln == 0: return off + 1
                if ln & 0xC0 == 0xC0: return off + 2
                off += ln + 1
        off = skip_name(off) + 4
        for _ in range(ancount):
            off = skip_name(off)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10]); off += 10
            rdata = data[off:off + rdlen]; off += rdlen
            if rtype == 1 and rdlen == 4: return socket.inet_ntoa(rdata)
        return None
    except Exception: return None

_dns_cache = {}
_dns_lock = threading.Lock()

def resolve(host):
    """域名解析只走隧道内 DNS（fail-closed）：1.1.1.1、8.8.8.8 都失败就抛错，
    绝不回退本机系统解析，避免域名从宿主机网络泄漏出去。
    成功结果缓存 120 秒——重复连接同一域名免再付一轮隧道 DNS 往返。"""
    try:
        ipaddress.ip_address(host); return host
    except Exception: pass
    now = time.time()
    with _dns_lock:
        ent = _dns_cache.get(host)
        if ent and ent[1] > now: return ent[0]
    for server in ("1.1.1.1", "8.8.8.8"):
        ip = dns_query_a(host, server=server)
        if ip:
            with _dns_lock:
                if len(_dns_cache) > 512: _dns_cache.clear()
                _dns_cache[host] = (ip, now + 120)
            return ip
    raise ConnectionError("DNS fail-closed: %s" % host)

# ---------------------------------------------------------------- gateway (SOCKS5 + HTTP mixed)
def bound_socket(host, port, timeout=15):
    ip = resolve(host) if not _is_ip(host) else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(timeout)
    s.setsockopt(socket.SOL_SOCKET, 25, TUN_DEV.encode() + b"\x00")
    s.connect((ip, port)); s.settimeout(None)
    return s

def _is_ip(h):
    try: ipaddress.ip_address(h); return True
    except Exception: return False

def relay(a, b):
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data: break
                dst.sendall(data)
        except Exception: pass
        try: dst.shutdown(socket.SHUT_WR)
        except Exception: pass
    t = threading.Thread(target=pump, args=(a, b), daemon=True); t.start()
    pump(b, a)

def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk: raise ConnectionError("eof")
        buf += chunk
    return buf

def handle_udp_associate(conn):
    """SOCKS5 UDP ASSOCIATE：为本条控制连接开专属 UDP 中继口，出站 UDP 绑定
    tun 设备（与 TCP 同一条隧道）；控制连接断开即销毁关联。
    仅支持 FRAG=0 与 IPv4/域名目标（IPv6 目标丢弃，与 TCP 侧行为一致）。"""
    relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay_sock.bind(("127.0.0.1", 0)); relay_sock.settimeout(1.0)
    relay_port = relay_sock.getsockname()[1]
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, 25, TUN_DEV.encode() + b"\x00")
    out.settimeout(1.0)
    stop = threading.Event(); client_addr = [None]
    def to_remote():
        while not stop.is_set():
            try: data, addr = relay_sock.recvfrom(65536)
            except socket.timeout: continue
            except Exception: break
            if client_addr[0] is None: client_addr[0] = addr
            if addr != client_addr[0]: continue
            try:
                if len(data) < 4 or data[0] != 0 or data[1] != 0 or data[2] != 0: continue
                atyp = data[3]; off = 4
                if atyp == 1:
                    host = socket.inet_ntoa(data[off:off + 4]); off += 4
                elif atyp == 3:
                    ln = data[off]; off += 1
                    host = data[off:off + ln].decode(); off += ln
                else: continue
                dport = struct.unpack(">H", data[off:off + 2])[0]; off += 2
                out.sendto(data[off:], (resolve(host), dport))
            except Exception: continue
    def to_client():
        while not stop.is_set():
            try: data, (src_ip, src_port) = out.recvfrom(65536)
            except socket.timeout: continue
            except Exception: break
            if client_addr[0] is None: continue
            try:
                head = b"\x00\x00\x00\x01" + socket.inet_aton(src_ip) + struct.pack(">H", src_port)
                relay_sock.sendto(head + data, client_addr[0])
            except Exception: continue
    threading.Thread(target=to_remote, daemon=True).start()
    threading.Thread(target=to_client, daemon=True).start()
    conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", relay_port))
    try:
        while conn.recv(4096): pass
    except Exception: pass
    stop.set()
    for s in (relay_sock, out):
        try: s.close()
        except Exception: pass

def handle_socks(conn, cfg):
    try:
        ver, nmethods = _recv_exact(conn, 2)
        methods = _recv_exact(conn, nmethods)
        if cfg.get("gateway_user"):
            if 0x02 not in methods: conn.sendall(b"\x05\xff"); return
            conn.sendall(b"\x05\x02")
            aver, ulen = _recv_exact(conn, 2)
            user = _recv_exact(conn, ulen).decode("utf-8", "replace")
            plen = _recv_exact(conn, 1)[0]
            pw = _recv_exact(conn, plen).decode("utf-8", "replace")
            if user != cfg["gateway_user"] or pw != cfg["gateway_pass"]:
                conn.sendall(b"\x01\x01"); return
            conn.sendall(b"\x01\x00")
        else:
            conn.sendall(b"\x05\x00")
        ver, cmd, _rsv, atyp = _recv_exact(conn, 4)
        if atyp == 1: host = socket.inet_ntoa(_recv_exact(conn, 4))
        elif atyp == 3:
            ln = _recv_exact(conn, 1)[0]; host = _recv_exact(conn, ln).decode()
        elif atyp == 4: host = socket.inet_ntop(socket.AF_INET6, _recv_exact(conn, 16))
        else: return
        port = struct.unpack(">H", _recv_exact(conn, 2))[0]
        if cmd == 3:
            handle_udp_associate(conn); return
        if cmd != 1: conn.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00"); return
        try: remote = bound_socket(host, port)
        except Exception:
            conn.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00"); return
        conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        relay(conn, remote)
    except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass

def handle_http(conn, first):
    try:
        data = first
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = conn.recv(65536)
            if not chunk: return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, _, prt = target.partition(":")
            remote = bound_socket(host, int(prt or 443))
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest: remote.sendall(rest)
            relay(conn, remote); return
        m = re.match(r"http://([^/:]+)(?::(\d+))?(/.*)?$", target)
        if not m: conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n"); return
        host, prt, path = m.group(1), int(m.group(2) or 80), m.group(3) or "/"
        remote = bound_socket(host, prt)
        out = ["%s %s %s" % (method, path, version)]
        for ln in lines[1:]:
            if ln.lower().startswith(("proxy-",)): continue
            out.append(ln)
        remote.sendall(("\r\n".join(out) + "\r\n\r\n").encode("iso-8859-1") + rest)
        relay(conn, remote)
    except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass

def gateway_loop(cfg):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", cfg["gateway_port"])); srv.listen(128)
    print("[gateway] 127.0.0.1:%d （出站绑定 %s）" % (cfg["gateway_port"], TUN_DEV), flush=True)
    while True:
        conn, _ = srv.accept()
        def dispatch(c=conn):
            try:
                c.settimeout(20)
                first = c.recv(1, socket.MSG_PEEK)
                c.settimeout(None)
                if first == b"\x05": handle_socks(c, cfg)
                else:
                    data = c.recv(65536)
                    if data: handle_http(c, data)
            except Exception:
                try: c.close()
                except Exception: pass
        threading.Thread(target=dispatch, daemon=True).start()

# ---------------------------------------------------------------- exit check via own gateway
def exit_check(cfg, timeout=12):
    """经自己的 SOCKS 网关访问 ip-api，验证出口 IP/国家。返回 dict 或 None"""
    try:
        s = socket.create_connection(("127.0.0.1", cfg["gateway_port"]), timeout=timeout)
        s.settimeout(timeout)
        if cfg.get("gateway_user"):
            s.sendall(b"\x05\x01\x02")
            if s.recv(2) != b"\x05\x02": return None
            u = cfg["gateway_user"].encode(); pw = cfg["gateway_pass"].encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
            if s.recv(2) != b"\x01\x00": return None
        else:
            s.sendall(b"\x05\x01\x00"); s.recv(2)
        host = "ip-api.com"
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + struct.pack(">H", 80))
        rep = s.recv(10)
        if len(rep) < 2 or rep[1] != 0: return None
        s.sendall(b"GET /json/?fields=status,query,country,countryCode,isp,org HTTP/1.0\r\nHost: ip-api.com\r\n\r\n")
        buf = b""
        while b"\r\n\r\n" not in buf: 
            chunk = s.recv(4096)
            if not chunk: return None
            buf += chunk
        body = buf.split(b"\r\n\r\n", 1)[1]
        while True:
            try: return json.loads(body.decode("utf-8", "replace"))
            except Exception:
                chunk = s.recv(4096)
                if not chunk: return None
                body += chunk
    except Exception: return None

# ---------------------------------------------------------------- exit measurement（实测速度 / 网页延时）
def _socks_open(cfg, host, port, timeout=15):
    """经自家 SOCKS 网关开一条 TCP 流（域名形式，网关侧解析），失败返回 None"""
    try:
        s = socket.create_connection(("127.0.0.1", cfg["gateway_port"]), timeout=timeout)
        s.settimeout(timeout)
        if cfg.get("gateway_user"):
            s.sendall(b"\x05\x01\x02")
            if s.recv(2) != b"\x05\x02": s.close(); return None
            u = cfg["gateway_user"].encode(); pw = cfg["gateway_pass"].encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
            if s.recv(2) != b"\x01\x00": s.close(); return None
        else:
            s.sendall(b"\x05\x01\x00"); s.recv(2)
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + struct.pack(">H", port))
        rep = s.recv(10)
        if len(rep) < 2 or rep[1] != 0: s.close(); return None
        return s
    except Exception: return None

def measure_exit(cfg):
    """经隧道实测当前出口：网页延时 = 一次完整 ip-api 请求的耗时；实测速度 =
    经网关下载 1 MB 测试文件的吞吐（Cloudflare 测速端点优先，Tele2 备选）。
    返回 {"web_ms": int, "speed_mbps": float|缺省} 或 None（出口不通时）。"""
    t0 = time.time()
    info = exit_check(cfg)
    if not info or info.get("status") != "success": return None
    out = {"web_ms": int((time.time() - t0) * 1000)}
    for host, port, path, use_tls in (("speed.cloudflare.com", 443, "/__down?bytes=1000000", True),
                                      ("speedtest.tele2.net", 80, "/1MB.zip", False)):
        try:
            s = _socks_open(cfg, host, port, timeout=15)
            if s is None: continue
            if use_tls:
                s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
            s.sendall(("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: gatecore/1.0\r\n\r\n" % (path, host)).encode())
            total = 0; buf = b""; body = False
            t1 = time.time(); deadline = t1 + 20
            while time.time() < deadline:
                chunk = s.recv(65536)
                if not chunk: break
                if not body:
                    buf += chunk
                    if b"\r\n\r\n" in buf:
                        body = True
                        total += len(buf.split(b"\r\n\r\n", 1)[1])
                    continue
                total += len(chunk)
            s.close()
            dt = time.time() - t1
            if total >= 100000 and dt > 0:
                out["speed_mbps"] = round(total * 8 / dt / 1e6, 1)
                break
        except Exception: continue
    return out

def speed_loop(cfg):
    """每 10 分钟经隧道实测一次速度/网页延时，写进 state.current.exit 供面板展示。
    只测量、不动隧道；未连接时跳过。测速流量约 1 MB/次。"""
    time.sleep(60)
    while True:
        try:
            if get_state().get("current"):
                r = measure_exit(cfg)
                if r:
                    st = get_state(); cur = st.get("current")
                    if cur:
                        ex = cur.get("exit") or {}
                        ex["web_ms"] = r["web_ms"]
                        if "speed_mbps" in r:
                            ex["speed_mbps"] = r["speed_mbps"]
                            ex["speed_t"] = time.strftime("%H:%M:%S")
                        cur["exit"] = ex; st["current"] = cur; set_state(st)
                        print("[speed] 网页延时 %s ms，实测速度 %s" % (
                            r.get("web_ms"), ("%s Mbps" % r["speed_mbps"]) if "speed_mbps" in r else "未测出"), flush=True)
        except Exception as ex:
            print("[speed] error:", ex, flush=True)
        time.sleep(600)

# ---------------------------------------------------------------- connect logic
def _preprobe(cands, timeout=2.5):
    """并发轻量探测一池候选：明确无响应的本轮直接淘汰（省掉每个死节点一次
    完整握手超时）；有响应与探不了的（tls 等）保留，保持原有排序。
    探测失败不写历史惩罚——历史只记真实连接结果。"""
    results = {}
    def work(n):
        results[node_key(n)] = probe_node(n, timeout=timeout)
    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in cands]
    for t in threads: t.start()
    for t in threads: t.join(timeout + 2)
    resp, unk = [], []
    for n in cands:
        r = results.get(node_key(n))
        if r and r[0] is True: resp.append((n, r[1]))  # 带回探测时延，定本轮顺序
        elif r and r[0] is False: pass  # 本轮跳过
        else: unk.append(n)
    return resp, unk

def connect_node(cfg, target, cc_hint=""):
    """target: 具体节点 dict；或 None 表示按 cc_hint 自动选最优。逐个试候选，验出口。"""
    with TUNNEL.lock:
        nodes = enrich(fetch_nodes())
        hist = get_history()
        favs = [ip for ip in get_state().get("favorites", []) if ip]
        favset = set(favs)
        if target is not None:
            cands = [target]
        else:
            # 收藏优先（冷备队列）：收藏按名单顺序排在全局候选之前；带国家提示时
            # 本国收藏最先、全局本国候选其次、他国收藏垫底，全灭才轮到全局——不断网优先。
            by_ip = {n["ip"]: n for n in nodes}
            fav_nodes = [by_ip[ip] for ip in favs if ip in by_ip]
            if cc_hint:
                fav_in = [n for n in fav_nodes if n["cc"] == cc_hint]
                fav_out = [n for n in fav_nodes if n["cc"] != cc_hint]
            else:
                fav_in, fav_out = fav_nodes, []
            ranked = rank(nodes, cc_hint or None)
            res = [n for n in ranked if n.get("residential") is True and n["ip"] not in favset]
            oth = [n for n in ranked if n.get("residential") is not True and n["ip"] not in favset]
            pool = (res[:8] + oth[:3]) if res else [n for n in ranked if n["ip"] not in favset][:8]
            pool = fav_in + pool + fav_out
            resp, unk = _preprobe(pool)
            resp_keys = {node_key(n) for n, _m in resp}
            unk_keys = {node_key(n) for n in unk}
            def _trial_key(nm):
                n, pms = nm
                bonus = max(-20.0, min(20.0, (100.0 - pms) * 0.2)) if pms else 0.0
                return -(rank_score(n, get_history()) + bonus)
            g_resp = sorted(((n, m) for n, m in resp if n["ip"] not in favset), key=_trial_key)
            fav_trials = [n for n in (fav_in + fav_out)
                          if node_key(n) in resp_keys or node_key(n) in unk_keys]
            cands = (fav_trials + [n for n, _m in g_resp]
                     + [n for n in unk if n["ip"] not in favset])[:8]
            add_event("预探测：%d 个候选中 %d 个有响应、%d 个待试、%d 个无响应跳过"
                      % (len(pool), len(resp), len(unk), len(pool) - len(resp) - len(unk)))
            if favs:
                add_event("冷备队列：收藏 %d 个（池中 %d），本轮 %d 个进入候选"
                          % (len(favs), len(fav_nodes), len(fav_trials)))
            if not cands: cands = pool[:3]  # 探测全灭时兜底硬试前三名
        if not cands:
            add_event("无可用候选（国家 %s）" % (cc_hint or "全部")); return False
        for n in cands:
            add_event("尝试连接 %s (%s, %s:%s/%s)…" % (n["ip"], n["cc"], n.get("remote_host") or n["ip"], n.get("port"), n.get("proto")))
            t0 = time.time()
            ok = TUNNEL.start(n)
            ms = round((time.time() - t0) * 1000)
            if not ok:
                hist_update(node_key(n), False); add_event("✗ %s 隧道建立失败" % n["ip"]); continue
            info = exit_check(cfg)
            if not info or info.get("status") != "success":
                hist_update(node_key(n), False); add_event("✗ %s 出口验证失败" % n["ip"])
                TUNNEL.stop(); continue
            hist_update(node_key(n), True, ms)
            st = get_state()
            st["current"] = {"ip": n["ip"], "cc": n["cc"], "country": n["country"],
                             "port": n.get("port"), "proto": n.get("proto"),
                             "since": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "exit": {"ip": info.get("query"), "cc": info.get("countryCode"),
                                      "country": info.get("country"), "isp": info.get("isp") or info.get("org"),
                                      "checked": time.strftime("%H:%M:%S")}}
            st["fail_count"] = 0
            set_state(st)
            try:  # 经隧道实测时延只是记账，绝不能影响已成功的连接
                t_d = time.time(); dip = dns_query_a("example.com"); dms = int((time.time() - t_d) * 1000)
                if dip and 0 < dms < 5000: hist_latency(node_key(n), dms)
            except Exception: pass
            if target is None and favset and n["ip"] not in favset:
                add_event("收藏节点均不可用，已回落全局排行")
            add_event("✓ 已连接 %s，出口 %s (%s, %s)" % (n["ip"], info.get("query"), info.get("country"), info.get("isp") or ""))
            return True
        add_event("全部候选失败"); return False

def disconnect():
    with TUNNEL.lock:
        TUNNEL.stop()
        st = get_state(); st["current"] = None; set_state(st)
    add_event("已断开")

# ---------------------------------------------------------------- watch（只守候+通知，绝不自动连接）
def watch_check():
    """刷新列表，对守候国家做家宽出现检测：新节点出现 → 记录命中 + 面板事件。
    本函数只读列表与状态文件，绝不碰隧道与当前连接。"""
    st = get_state()
    watch = [c.upper() for c in st.get("watch", [])]
    if not watch: return
    nodes = enrich(fetch_nodes(force=True))
    hist = get_history()
    prev = load_json(p("watch_state.json"), {})
    hits = []
    for cc in watch:
        res = [n for n in nodes if n["cc"] == cc and n.get("residential") is True]
        res.sort(key=lambda n: -rank_score(n, hist))
        cur_ips = sorted(n["ip"] for n in res)
        old_ips = prev.get(cc, {}).get("ips", [])
        new_ips = [ip for ip in cur_ips if ip not in old_ips]
        if new_ips:
            top = next(n for n in res if n["ip"] == new_ips[0])
            hits.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "cc": cc,
                         "ip": top["ip"], "isp": top.get("isp", ""), "count": len(res),
                         "notified": False})
        prev[cc] = {"ips": cur_ips, "count": len(res), "checked": time.strftime("%H:%M:%S")}
    save_json(p("watch_state.json"), prev)
    if hits:
        st = get_state()
        st.setdefault("watch_hits", []).extend(hits)
        st["watch_hits"] = st["watch_hits"][-30:]
        set_state(st)
        for h in hits:
            add_event("守候命中：%s 家宽节点出现（%s，%s），当前共 %d 个 — 仅通知，未自动连接"
                      % (h["cc"], h["ip"], h["isp"] or "未知ISP", h["count"]))

# ---------------------------------------------------------------- favorites probe（冷备队列状态）
def fav_probe():
    """定时轻探收藏节点，把存活/时延写进 state.fav_status 供面板冷备队列显示。
    只探测、不连接、不动隧道。"""
    st = get_state()
    favs = [ip for ip in st.get("favorites", []) if ip]
    if not favs: return
    nodes = enrich(fetch_nodes())
    by_ip = {n["ip"]: n for n in nodes}
    targets = [by_ip[ip] for ip in favs if ip in by_ip]
    resp, unk = _preprobe(targets, timeout=2.5) if targets else ([], [])
    rmap = {n["ip"]: m for n, m in resp}
    umap = {n["ip"] for n in unk}
    now = time.strftime("%H:%M:%S")
    status = {}
    for ip in favs:
        if ip in rmap: status[ip] = {"ok": True, "ms": rmap[ip], "inpool": True, "t": now}
        elif ip in umap: status[ip] = {"ok": None, "ms": None, "inpool": True, "t": now}
        elif ip in by_ip: status[ip] = {"ok": False, "ms": None, "inpool": True, "t": now}
        else: status[ip] = {"ok": False, "ms": None, "inpool": False, "t": now}
    st = get_state(); st["fav_status"] = status; set_state(st)

# ---------------------------------------------------------------- health loop
def health_loop(cfg):
    last_watch = 0
    last_fav = 0
    while True:
        time.sleep(30)
        try:
            stw = get_state()
            if stw.get("watch") and time.time() - last_watch >= int(stw.get("watch_interval", 900)):
                last_watch = time.time()
                try: watch_check()
                except Exception as ex: print("[watch] error:", ex, flush=True)
            if stw.get("favorites") and time.time() - last_fav >= 300:
                last_fav = time.time()
                try: fav_probe()
                except Exception as ex: print("[fav] error:", ex, flush=True)
            st = get_state(); cur = st.get("current")
            if not cur: 
                if st.get("mode") == "auto": connect_node(cfg, None, "")
                continue
            t_h = time.time()
            info = exit_check(cfg)
            if info and info.get("status") == "success":
                if st.get("fail_count"):
                    st["fail_count"] = 0; set_state(st)
                old_ex = cur.get("exit") or {}
                new_ex = {"ip": info.get("query"), "cc": info.get("countryCode"),
                          "country": info.get("country"), "isp": info.get("isp") or info.get("org"),
                          "checked": time.strftime("%H:%M:%S"),
                          "web_ms": int((time.time() - t_h) * 1000)}
                if old_ex.get("speed_mbps") is not None:
                    new_ex["speed_mbps"] = old_ex["speed_mbps"]
                    new_ex["speed_t"] = old_ex.get("speed_t")
                cur["exit"] = new_ex
                st["current"] = cur; set_state(st)
            else:
                st["fail_count"] = st.get("fail_count", 0) + 1; set_state(st)
                add_event("健康检查失败（%d/2）" % st["fail_count"])
                if st["fail_count"] >= 2:
                    mode = st.get("mode", "manual")
                    if mode == "fixed-ip":
                        nodes = enrich(fetch_nodes())
                        tgt = next((n for n in nodes if n["ip"] == cur["ip"]), None)
                        if tgt: connect_node(cfg, tgt)
                    elif mode in ("auto", "fixed-country"):
                        connect_node(cfg, None, st.get("cc", "") if mode == "fixed-country" else "")
                    else:
                        add_event("手动模式：不自动切换，等面板操作")
                        st["fail_count"] = 0; set_state(st)
        except Exception as ex:
            print("[health] error:", ex, flush=True)

# ---------------------------------------------------------------- web panel
CSS = """*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;background:linear-gradient(180deg,#e9f1f8 0,#f7fafc 260px);color:#0f1f2e;margin:0;padding:20px 14px 44px}
.wrap{max-width:980px;margin:0 auto}
.top{display:flex;align-items:center;gap:11px;margin:2px 2px 16px}
.logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,#0a84ff,#00b894);color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 4px 14px rgba(10,132,255,.35)}
.top h1{font-size:19px;margin:0;letter-spacing:.2px}
.top .sub{color:#71808e;font-size:12px;margin-top:1px}
.pill{margin-left:auto;background:#fff;border:1px solid #dfe7ee;color:#44586a;border-radius:999px;padding:4px 12px;font-size:12px;white-space:nowrap}
.card{background:#fff;border:1px solid #e3eaf1;border-radius:18px;padding:18px 20px;margin:0 0 14px;box-shadow:0 6px 24px rgba(31,68,120,.06)}
.card h2{font-size:14px;margin:0 0 12px;color:#24384c;letter-spacing:.3px}
.hero{display:flex;gap:13px;align-items:flex-start}
.dot{flex:none;width:12px;height:12px;border-radius:50%;background:#1db954;box-shadow:0 0 0 5px rgba(29,185,84,.16);margin-top:9px}
.dot.off{background:#c3ced8;box-shadow:0 0 0 5px rgba(160,175,190,.18)}
.exitip{font-size:27px;font-weight:700;letter-spacing:.2px;font-variant-numeric:tabular-nums;line-height:1.15}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.chip{background:#eef4fb;color:#20507e;border-radius:999px;padding:3px 10px;font-size:12px}
.chip.green{background:#e7f7ee;color:#14713c}
.meta{display:flex;flex-wrap:wrap;gap:8px 28px;margin-top:14px;border-top:1px dashed #e3eaf1;padding-top:12px}
.meta div span{display:block;color:#8494a3;font-size:11px;margin-bottom:2px}
.meta div b{font-size:13px;font-weight:600}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:0 0 14px}
.stat{background:#fff;border:1px solid #e3eaf1;border-radius:16px;padding:12px 15px;box-shadow:0 4px 16px rgba(31,68,120,.05)}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums;line-height:1.2}
.stat span{color:#8494a3;font-size:11px}
@media(max-width:640px){.stats{grid-template-columns:repeat(2,1fr)}.exitip{font-size:23px}}
label{font-size:12px;color:#5a6d7f}
input,select{padding:7px 10px;border:1px solid #cfdae4;border-radius:10px;font-size:13px;background:#fbfcfe;color:#0f1f2e}
input:focus,select:focus{outline:2px solid rgba(10,132,255,.35);border-color:#0a84ff}
button{background:#0a84ff;color:#fff;border:0;border-radius:10px;padding:7px 14px;cursor:pointer;font-size:13px;font-weight:600}
button:hover{filter:brightness(1.08)}
button.gray{background:#edf2f7;color:#27435c}
button.red{background:#fff;color:#d3382c;border:1px solid #f0c6c2}
button.sm{padding:4px 11px;font-size:12px;border-radius:8px}
button.favb{background:#fff7e6;color:#a87b00;border:1px solid #ecd9a0}
form{display:inline}
.frow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:7px 0}
.tblwrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:780px}
table.plain{min-width:0}
th{color:#8494a3;font-weight:600;font-size:11px;text-align:left;padding:6px 8px;border-bottom:1px solid #e3eaf1;white-space:nowrap}
td{padding:8px;border-bottom:1px solid #f0f4f8;white-space:nowrap;font-variant-numeric:tabular-nums}
tbody tr:hover td{background:#f6fafd}
tr.cur td{background:#eef7ff}
.badge{display:inline-block;background:#e7f7ee;color:#14713c;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:600}
.badge.dc{background:#eef1f5;color:#66788a}
.badge.now{background:#0a84ff;color:#fff;margin-right:6px}
.rank{display:inline-block;min-width:30px;text-align:center;background:#f2f6fa;border-radius:8px;padding:2px 6px;font-weight:600;color:#33506e}
.mut{color:#8494a3;font-size:12px}
.note{color:#8494a3;font-size:12px;line-height:1.65;margin-top:10px}
pre{background:#f4f8fb;border:1px solid #e3eaf1;color:#39536b;border-radius:12px;padding:12px 14px;font-size:12px;line-height:1.7;overflow:auto;max-height:200px;margin:0}
.hit{padding:5px 0;border-bottom:1px dashed #e8eff5;font-size:12.5px}
.hit:last-child{border-bottom:0}
.err{background:#fdf0ef;border-color:#f3d2cf;color:#a33327}
"""

def esc(x): return str(x if x is not None else "").replace("&", "&amp;").replace("<", "&lt;")

def flag(cc):
    cc = (cc or "").upper()
    if len(cc) == 2 and cc.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)
    return ""

class Panel(BaseHTTPRequestHandler):
    cfg = None
    def log_message(self, *a): pass
    def _auth_ok(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "): return False
        try:
            u, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            return u == self.cfg["panel_user"] and pw == self.cfg["panel_pass"]
        except Exception: return False
    def _send(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def _page(self, inner):
        return "<!DOCTYPE html><html lang=zh><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>gatecore 面板</title><style>%s</style></head><body>%s</body></html>" % (CSS, inner)
    def do_GET(self):
        base = "/" + self.cfg["panel_path"]
        if not self.path.startswith(base):
            self._send("<h1>404</h1>", 404); return
        if not self._auth_ok():
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="gatecore"')
            self.end_headers(); return
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        cc = (q.get("cc") or [""])[0].upper()
        res_only = (q.get("res") or [""])[0] == "1"
        fav_only = (q.get("fav") or [""])[0] == "1"
        st = get_state(); cur = st.get("current")
        mode = st.get("mode", "manual")
        mode_label = {"manual": "手动模式", "auto": "智能自动", "fixed-country": "固定国家", "fixed-ip": "固定 IP"}.get(mode, mode)
        err_html = ""
        try:
            nodes = enrich(fetch_nodes())
            ranked = rank(nodes, cc or None)
            res_ranked = sum(1 for n in ranked if n.get("residential") is True)
            res_all = sum(1 for n in nodes if n.get("residential") is True)
            cc_all = len(set(n["cc"] for n in nodes))
            if res_only: ranked = [n for n in ranked if n.get("residential") is True]
            if fav_only:
                favset0 = set(st.get("favorites", []))
                ranked = [n for n in ranked if n["ip"] in favset0]
        except Exception as ex:
            nodes = []; ranked = []; res_ranked = 0; res_all = 0; cc_all = 0
            err_html = "<div class='card err'>节点列表拉取失败：%s</div>" % esc(ex)
        top = """<div class=top><div class=logo>G</div><div><h1>gatecore</h1><div class=sub>家宽出口核心 · VPN Gate</div></div><span class=pill>%s</span></div>""" % esc(mode_label)
        if cur:
            ex = cur.get("exit") or {}
            speed_s = ("%s Mbps" % ex.get("speed_mbps")) if ex.get("speed_mbps") is not None else "—"
            web_s = ("%s ms" % ex.get("web_ms")) if ex.get("web_ms") is not None else "—"
            status = """<div class=card><div class=hero><div class=dot></div><div>
            <div class=exitip>%s</div>
            <div class=chips><span class='chip green'>已连接</span><span class=chip>%s %s</span><span class=chip>%s</span></div>
            </div><form method=post action='%s/disconnect' style='margin-left:auto'><button class=red>断开</button></form></div>
            <div class=meta><div><span>经节点</span><b>%s</b></div><div><span>节点归属</span><b>%s</b></div><div><span>连接自</span><b>%s</b></div><div><span>最近验证</span><b>%s</b></div><div><span>实测速度</span><b>%s</b></div><div><span>网页延时</span><b>%s</b></div></div></div>""" % (
                esc(ex.get("ip", "—")), flag(cur.get("country", "")), esc(ex.get("country", "")),
                esc(ex.get("isp", "")), base,
                esc(cur.get("ip", "")), esc(cur.get("country", "")), esc(cur.get("since", "")), esc(ex.get("checked", "")),
                esc(speed_s), esc(web_s))
        else:
            status = """<div class=card><div class=hero><div class='dot off'></div><div>
            <div class=exitip>未连接</div>
            <div class=chips><span class=chip>出口未建立</span></div>
            <div class=mut style='margin-top:8px'>从下方节点列表选一个连接，或按国家自动选最优。</div>
            </div></div></div>"""
        stats = """<div class=stats><div class=stat><b>%d</b><span>节点总数</span></div><div class=stat><b>%d</b><span>其中家宽</span></div><div class=stat><b>%d</b><span>覆盖国家</span></div><div class=stat><b>%d</b><span>守候国家</span></div></div>""" % (
            len(nodes), res_all, cc_all, len(st.get("watch", [])))
        modes = "".join("<option value='%s'%s>%s</option>" % (v, " selected" if mode == v else "", label)
                        for v, label in [("manual", "手动（不自动切换）"), ("auto", "智能自动"), ("fixed-country", "固定国家"), ("fixed-ip", "固定 IP")])
        ctrl = """<div class=card><h2>控制</h2>
        <div class=frow><form method=post action='%s/mode'><label>模式</label> <select name=mode>%s</select>
        <label>国家</label> <input name=cc size=3 value='%s' placeholder='JP'> <button>保存模式</button></form>
        <form method=post action='%s/refresh'><button class=gray>刷新节点列表</button></form></div>
        <div class=frow><form method=get action='%s/'><label>筛选国家</label> <input name=cc size=3 value='%s' placeholder='JP/KR/US'>
        <label><input type=checkbox name=res value=1%s> 只看家宽</label>
        <label><input type=checkbox name=fav value=1%s> 只看收藏</label>
        <button class=gray>筛选</button></form></div></div>""" % (base, modes, esc(st.get("cc", "")), base, base, esc(cc), " checked" if res_only else "", " checked" if fav_only else "")
        acct = """<div class=card><h2>账号设置</h2>
        <div class=frow><form method=post action='%s/account_panel'><label>面板登录</label> <input name=user size=12 value='%s' autocomplete=username>
        <input type=password name=pass1 size=14 placeholder='新密码（至少 8 位）' autocomplete=new-password>
        <input type=password name=pass2 size=14 placeholder='再输一次' autocomplete=new-password> <button>修改面板凭据</button></form></div>
        <div class=note>面板凭据修改后，下次打开面板用新账号密码登录。网关凭据由安装时随机生成，不在此处修改：改它必须同步修改指向本网关的客户端配置（如 Xray 出站），操作不当会断链，属进阶操作（改 config.json 的 gateway_user / gateway_pass 并同步客户端）。</div></div>""" % (
            base, esc(self.cfg["panel_user"]))
        wst = load_json(p("watch_state.json"), {})
        wrows = []
        for ccw in st.get("watch", []):
            info = wst.get(ccw, {})
            wstat = ("<span class=badge>已出现 %d 个家宽</span>" % info["count"]) if info.get("count") else "<span class=mut>守候中（暂无）</span>"
            wrows.append("<tr><td>%s %s</td><td>%s</td><td>%s</td></tr>" % (flag(ccw), esc(ccw), wstat, esc(info.get("checked", "—"))))
        hits_html = "".join("<div class=hit>%s · %s %s 家宽出现：<b>%s</b>（%s），共 %d 个</div>" % (
            esc(h["t"]), flag(h["cc"]), esc(h["cc"]), esc(h["ip"]), esc(h["isp"]), h["count"]) for h in st.get("watch_hits", [])[-5:][::-1])
        watch_card = """<div class=card><h2>守候（只通知，不自动连接）</h2>
        <div class=frow><form method=post action='%s/watch'><label>国家名单（逗号分隔）</label> <input name=watch size=18 value='%s' placeholder='SG,HK,TW'>
        <label>间隔</label> <select name=winterval>%s</select> <button>保存守候</button></form></div>
        <div class=tblwrap><table class=plain><tr><th>国家</th><th>状态</th><th>最近检查</th></tr>%s</table></div>%s</div>""" % (
            base, esc(",".join(st.get("watch", []))),
            "".join("<option value='%d'%s>%d 分钟</option>" % (v, " selected" if int(st.get("watch_interval", 900)) == v else "", v // 60) for v in (300, 900, 1800)),
            "".join(wrows), hits_html)
        favs_list = [ip for ip in st.get("favorites", []) if ip]
        queue_card = ""
        if favs_list:
            fstat = st.get("fav_status", {})
            by_ip = {n["ip"]: n for n in nodes}
            qrows = []
            pos = 0
            for ip in favs_list:
                nd = by_ip.get(ip)
                fs = fstat.get(ip, {})
                if cur and ip == cur.get("ip"):
                    role = "<span class='badge now'>主节点</span>"
                else:
                    pos += 1
                    role = "<span class=mut>冷备 %d</span>" % pos
                if nd:
                    place = "%s %s" % (flag(nd["cc"]), esc(nd["cc"]))
                    ownq = esc((nd.get("isp") or "")[:16]) or "—"
                else:
                    place = "—"; ownq = "已下线（池中无）"
                if fs.get("ok") is True: live = "<span class=badge>在线 %s ms</span>" % fs.get("ms")
                elif fs and fs.get("ok") is None: live = "<span class=mut>待确认</span>"
                elif fs: live = "<span class=mut>无响应</span>"
                else: live = "<span class=mut>未探测</span>"
                qrows.append("<tr%s><td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td><form method=post action='%s/fav'><input type=hidden name=ip value='%s'><button class='sm red'>移除</button></form></td></tr>" % (
                    " class=cur" if cur and ip == cur.get("ip") else "", role, esc(ip), place, ownq, live, base, esc(ip)))
            queue_card = """<div class=card><h2>冷备队列（收藏驱动）</h2>
            <div class=tblwrap><table class=plain><tr><th>顺位</th><th>IP</th><th>国家</th><th>归属</th><th>状态</th><th></th></tr>%s</table></div>
            <div class=note>智能自动 / 固定国家模式下，主节点断开时按此队列顺序从收藏中顺延连接；收藏全部不可用时回落全局排行，不断网优先。手动 / 固定 IP 模式不自动顺延。状态每 5 分钟轻探刷新。</div></div>""" % "".join(qrows)
        hist = get_history()
        favset = set(st.get("favorites", []))
        rows = []
        for n in ranked[:60]:
            e = hist.get(node_key(n), {})
            tot = e.get("ok", 0) + e.get("fail", 0)
            rate = ("%d%%" % round(e["ok"] * 100 / tot)) if tot else "—"
            ms = e.get("ms"); ms = ("%s ms" % ms) if ms is not None else "—"
            if n.get("residential") is True: badge = "<span class=badge>家宽</span>"
            elif n.get("residential") is False: badge = "<span class='badge dc'>机房</span>"
            else: badge = "<span class=mut>未知</span>"
            own = badge + " " + esc((n.get("isp") or "")[:16])
            is_cur = bool(cur) and n["ip"] == cur.get("ip")
            now = "<span class='badge now'>当前</span>" if is_cur else ""
            rows.append("<tr%s><td><b>%s</b></td><td>%s %s</td><td>%s</td><td>%s</td><td>%s ms</td><td>%s Mbps</td><td>%s</td><td>%s / %s</td><td><span class=rank>%s</span></td>"
                        "<td>%s<form method=post action='%s/switch'><input type=hidden name=ip value='%s'><button class=sm>切换</button></form> <form method=post action='%s/fav'><input type=hidden name=ip value='%s'><button class='sm favb'>%s</button></form></td></tr>" % (
                " class=cur" if is_cur else "",
                esc(n["ip"]), flag(n["cc"]), esc(n["cc"]), own, esc(n.get("proto", "")), n["ping"], round(n["speed"] / 1e6),
                n["sessions"], ms, rate, n["rank"], now, base, esc(n["ip"]), base, esc(n["ip"]), "★" if n["ip"] in favset else "☆"))
        auto_btn = ""
        if cc:
            auto_btn = "<form method=post action='%s/auto'><input type=hidden name=cc value='%s'><button>连 %s 最优</button></form>" % (base, esc(cc), esc(cc))
        table = """<div class=card><h2>节点（%s，共 %d 个，家宽 %d 个%s）</h2><div class=frow>%s</div>
        <div class=tblwrap><table><tr><th>IP</th><th>国家</th><th>归属</th><th>协议</th><th>API延迟</th><th>速度</th><th>在线</th><th>实测 / 成功率</th><th>评分</th><th></th></tr>%s</table></div>
        <div class=note>归属按主机名 / 运营者 / ip-api 多信号判定：vpn+数字主机名或个人运营者 = 家宽，public-vpn 学术服务器与机房归属 = 机房。实测与成功率来自本机历史连接记录，越用越准；评分含家宽 / UDP / 实测时延加权。</div></div>""" % (
            esc(cc or "全部"), len(ranked), res_ranked, "" if cc else "，显示前 60", auto_btn, "".join(rows))
        ev = "\n".join("%s  %s" % (e["t"], e["msg"]) for e in st.get("events", [])[-15:][::-1])
        log = "<div class=card><h2>事件</h2><pre>%s</pre></div>" % esc(ev)
        self._send(self._page("<div class=wrap>" + top + status + queue_card + stats + err_html + ctrl + acct + watch_card + table + log + "</div>"))

    def do_POST(self):
        base = "/" + self.cfg["panel_path"]
        if not self.path.startswith(base) or not self._auth_ok():
            self._send("<h1>403</h1>", 403); return
        from urllib.parse import urlparse, parse_qs
        action = urlparse(self.path).path[len(base):]
        ln = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(ln).decode("utf-8", "replace"))
        def val(k): return (form.get(k) or [""])[0].strip()
        cfg = self.cfg
        def bg(fn, *a):
            threading.Thread(target=fn, args=a, daemon=True).start()
            self.send_response(303); self.send_header("Location", base + "/"); self.end_headers()
        if action == "/disconnect": bg(disconnect)
        elif action == "/refresh": bg(lambda: fetch_nodes(force=True)); 
        elif action == "/auto": bg(connect_node, cfg, None, val("cc").upper())
        elif action == "/switch":
            ip = val("ip")
            nodes = enrich(fetch_nodes())
            tgt = next((n for n in nodes if n["ip"] == ip), None)
            if tgt: bg(connect_node, cfg, tgt)
            else:
                self.send_response(303); self.send_header("Location", base + "/"); self.end_headers()
        elif action == "/mode":
            st = get_state(); st["mode"] = val("mode") or "manual"; st["cc"] = val("cc").upper(); set_state(st)
            add_event("模式设为 %s（国家 %s）" % (st["mode"], st["cc"] or "-"))
            self.send_response(303); self.send_header("Location", base + "/"); self.end_headers()
        elif action == "/watch":
            codes = [c for c in re.split(r"[,，\s]+", val("watch").upper()) if re.fullmatch(r"[A-Z]{2}", c)]
            st = get_state(); st["watch"] = codes
            try: st["watch_interval"] = int(val("winterval") or 900)
            except Exception: st["watch_interval"] = 900
            set_state(st)
            add_event("守候名单更新：%s（间隔 %d 分钟，仅通知不连接）" % (",".join(codes) or "空", st["watch_interval"] // 60))
            self.send_response(303); self.send_header("Location", base + "/"); self.end_headers()
        elif action == "/fav":
            ip = val("ip")
            st = get_state(); favs = [x for x in st.get("favorites", []) if x]
            removed = ip in favs
            if removed: favs.remove(ip)
            elif ip: favs.append(ip)
            st["favorites"] = favs; set_state(st)
            if removed: add_event("取消收藏 %s" % ip)
            elif ip: add_event("收藏节点 %s（冷备队列第 %d 位）" % (ip, len(favs)))
            self.send_response(303); self.send_header("Location", base + "/"); self.end_headers()
        elif action == "/account_panel":
            kind = "panel"
            nu, np1, np2 = val("user"), val("pass1"), val("pass2")
            err = None
            if not nu or len(nu) > 32 or ":" in nu or any(c.isspace() for c in nu):
                err = "用户名不合法：需 1–32 个字符，不能含空格或冒号。"
            elif len(np1) < 8 or len(np1) > 64:
                err = "密码长度需为 8–64 个字符。"
            elif np1 != np2:
                err = "两次输入的密码不一致。"
            if err:
                self._send(self._page("<div class=wrap><div class='card err'>%s</div><p><a href='%s/'>← 返回面板</a></p></div>" % (esc(err), base)))
            else:
                cfg[kind + "_user"] = nu; cfg[kind + "_pass"] = np1
                save_json(p("config.json"), cfg)
                try: os.chmod(p("config.json"), 0o600)
                except Exception: pass
                add_event("已修改面板凭据（用户 %s）" % nu)
                tip = "下次打开面板时，请用新账号密码登录。"
                self._send(self._page("<div class=wrap><div class=card><h2>✓ 已修改</h2><p>%s</p><p><a href='%s/'>← 返回面板</a></p></div></div>" % (tip, base)))
        else:
            self._send("<h1>404</h1>", 404)

# ---------------------------------------------------------------- CLI
def main():
    cfg = get_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "fetch":
        nodes = fetch_nodes(force=True)
        import collections
        print("节点 %d 个，国家 %d 个" % (len(nodes), len(set(collections.Counter(n['cc'] for n in nodes)))))
        print(collections.Counter(n["cc"] for n in nodes).most_common(10))
    elif cmd == "list":
        cc = (sys.argv[2] if len(sys.argv) > 2 else "").upper() or None
        for n in rank(enrich(fetch_nodes()), cc)[:10]:
            mark = "家宽" if n.get("residential") is True else ("机房" if n.get("residential") is False else "未知")
            print("%-16s %s %-4s [%s %s] ping=%-5s speed=%6.1fMbps sess=%-4s rank=%s" % (
                n["ip"], n["cc"], n.get("proto"), mark, (n.get("isp") or "")[:14],
                n["ping"], n["speed"] / 1e6, n["sessions"], n["rank"]))
    elif cmd == "probe":
        cc = (sys.argv[2] if len(sys.argv) > 2 else "JP").upper()
        for n in rank(enrich(fetch_nodes()), cc)[:6]:
            ok, ms, note = probe_node(n)
            print("%-16s %s -> ok=%s ms=%s (%s)" % (n["ip"], n["cc"], ok, ms, note))
            if ok: hist_update(node_key(n), True, ms)
    elif cmd == "sync":
        nodes = enrich(fetch_nodes(force=True))
        import collections
        res = [n for n in nodes if n.get("residential") is True]
        dc = [n for n in nodes if n.get("residential") is False]
        unk = [n for n in nodes if n.get("residential") is None]
        print("总 %d | 家宽 %d | 机房/项目 %d | 未知 %d" % (len(nodes), len(res), len(dc), len(unk)))
        print("家宽国家分布:", collections.Counter(n["cc"] for n in res).most_common(10))
        print("已同步 residential.json（家宽 %d 个）" % len(res))
    elif cmd == "connect":
        cc = (sys.argv[2] if len(sys.argv) > 2 else "JP").upper()
        ok = connect_node(cfg, None, cc)
        print("CONNECT", "OK" if ok else "FAIL")
        st = get_state(); print(json.dumps(st.get("current"), ensure_ascii=False))
    elif cmd == "disconnect": disconnect()
    elif cmd == "check":
        print(json.dumps(exit_check(cfg), ensure_ascii=False))
    elif cmd == "speedtest":
        print(json.dumps(measure_exit(cfg), ensure_ascii=False))
    elif cmd == "watchcheck":
        watch_check()
        st = get_state()
        print("watch:", st.get("watch"), "| hits:", len(st.get("watch_hits", [])))
        for h in st.get("watch_hits", [])[-5:]:
            print(h["t"], h["cc"], h["ip"], h["isp"], "NOTIFIED" if h["notified"] else "PENDING")
    elif cmd == "watchhits":
        st = get_state()
        print(json.dumps([h for h in st.get("watch_hits", []) if not h.get("notified")], ensure_ascii=False))
    elif cmd == "watchack":
        st = get_state(); n = 0
        for h in st.get("watch_hits", []):
            if not h.get("notified"): h["notified"] = True; n += 1
        set_state(st); print("acked", n)
    elif cmd == "watch":
        st = get_state(); wst = load_json(p("watch_state.json"), {})
        print("守候名单:", st.get("watch"), "| 间隔:", int(st.get("watch_interval", 900)) // 60, "分钟")
        for ccw in st.get("watch", []):
            info = wst.get(ccw, {})
            print(" ", ccw, "家宽", info.get("count", 0), "个 | 最近检查", info.get("checked", "—"))
    elif cmd == "serve":
        # 启动对账：state 记着已连接、但 tun 设备不在 = 关机/重启前的残影连接，
        # 清掉让面板与状态如实显示未连接（手动模式下也不会因此自动连接）
        st0 = get_state()
        if st0.get("current") and not os.path.exists("/sys/class/net/" + TUN_DEV):
            st0["current"] = None; st0["fail_count"] = 0; set_state(st0)
            add_event("启动对账：无隧道设备，已清除残留的连接状态")
        print("[gatecore] 面板 http://%s:%d/%s/ （账号见 config.json）" % (
            cfg["panel_bind"], cfg["panel_port"], cfg["panel_path"]), flush=True)
        threading.Thread(target=gateway_loop, args=(cfg,), daemon=True).start()
        threading.Thread(target=health_loop, args=(cfg,), daemon=True).start()
        threading.Thread(target=speed_loop, args=(cfg,), daemon=True).start()
        threading.Thread(target=lambda: enrich(fetch_nodes()), daemon=True).start()
        Panel.cfg = cfg
        ThreadingHTTPServer((cfg["panel_bind"], cfg["panel_port"]), Panel).serve_forever()
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
