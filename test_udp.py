#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gatecore UDP 自检：经网关的 SOCKS5 UDP ASSOCIATE 向 OpenDNS 查询
myip.opendns.com，返回的 A 记录就是 UDP 流量实际的出口 IP。
与 state.json 里的 TCP 出口一致，即证明 UDP 确实走了隧道。"""
import json, random, socket, struct

cfg = json.load(open("config.json"))
s = socket.create_connection(("127.0.0.1", cfg["gateway_port"]), timeout=15)
s.sendall(b"\x05\x01\x02"); assert s.recv(2) == b"\x05\x02", "auth method"
u = cfg["gateway_user"].encode(); pw = cfg["gateway_pass"].encode()
s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
assert s.recv(2) == b"\x01\x00", "auth fail"
s.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")  # UDP ASSOCIATE
rep = s.recv(10); assert len(rep) == 10 and rep[1] == 0, rep
bnd_port = struct.unpack(">H", rep[8:10])[0]
print("UDP 中继口:", bnd_port)

tid = random.randint(0, 65535)
q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
for part in "myip.opendns.com".split("."):
    q += bytes([len(part)]) + part.encode()
q += b"\x00" + struct.pack(">HH", 1, 1)

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); udp.settimeout(12)
pkt = b"\x00\x00\x00\x01" + socket.inet_aton("208.67.222.222") + struct.pack(">H", 53) + q
udp.sendto(pkt, ("127.0.0.1", bnd_port))
data, _ = udp.recvfrom(4096)
assert data[0] == 0 and data[3] == 1, "bad udp reply header"
payload = data[4 + 4 + 2:]

def skip_name(buf, o):
    while True:
        ln = buf[o]
        if ln == 0: return o + 1
        if ln & 0xC0 == 0xC0: return o + 2
        o += ln + 1

ancount = struct.unpack(">H", payload[6:8])[0]
o = skip_name(payload, 12) + 4
ip = None
for _ in range(ancount):
    o = skip_name(payload, o)
    rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", payload[o:o + 10]); o += 10
    rd = payload[o:o + rdlen]; o += rdlen
    if rtype == 1 and rdlen == 4: ip = socket.inet_ntoa(rd)
print("UDP 出口 IP:", ip)
cur = (json.load(open("state.json")).get("current") or {}).get("exit", {}).get("ip")
print("当前 TCP 出口:", cur, "| 一致:", ip is not None and ip == cur)
s.close()
