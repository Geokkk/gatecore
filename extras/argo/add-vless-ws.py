#!/usr/bin/env python3
"""把一个回环 VLESS+WS 后端入站合并进现有 Xray 配置 (幂等)。

由 install-argo.sh 调用, 也可单独运行。只做一件事: 若配置里还没有
tag=vless-ws-in 的入站, 就追加一个只听 127.0.0.1 的 VLESS+WS 入站
(无 TLS —— TLS 由 Cloudflare 边缘终结, 回环段是 cloudflared 的明文
HTTP); 已有则原样复用。状态 (uuid/端口/路径/域名) 写到 --state 指定
的 JSON 文件, 供生成客户端链接。

用法:
  add-vless-ws.py --config /usr/local/etc/xray/config.json \
                  --state  /usr/local/etc/xray/argo.json \
                  --domain argo.example.com [--preferred cf.090227.xyz] \
                  [--port 31301] [--path /vlessws]
"""
import argparse
import json
import os
import sys
import uuid

TAG = "vless-ws-in"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--preferred", default="")
    ap.add_argument("--port", type=int, default=31301)
    ap.add_argument("--path", default="/vlessws")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    inbound = None
    for ib in cfg.get("inbounds", []):
        if ib.get("tag") == TAG:
            inbound = ib
            break

    if inbound is None:
        uid = str(uuid.uuid4())
        inbound = {
            "tag": TAG,
            "listen": "127.0.0.1",
            "port": args.port,
            "protocol": "vless",
            "settings": {"clients": [{"id": uid}], "decryption": "none"},
            "streamSettings": {
                "network": "ws",
                "security": "none",
                "wsSettings": {"path": args.path},
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }
        cfg.setdefault("inbounds", []).append(inbound)
        tmp = args.config + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, args.config)
        changed = True
    else:
        changed = False

    state = {
        "uuid": inbound["settings"]["clients"][0]["id"],
        "port": inbound["port"],
        "path": inbound["streamSettings"]["wsSettings"]["path"],
        "domain": args.domain,
        "preferred": args.preferred,
        "changed": changed,
    }
    with open(args.state, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.chmod(args.state, 0o600)
    print(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
