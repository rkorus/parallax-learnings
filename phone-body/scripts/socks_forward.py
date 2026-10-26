#!/usr/bin/env python3
"""Bridge a local TCP port through the Tailscale SOCKS5 proxy to a tailnet host.
Usage: socks_forward.py <local_port> <remote_host> <remote_port> [socks_port]
Lets adb (no native SOCKS support) reach the phone over userspace-mode Tailscale:
adb connects to 127.0.0.1:<local_port>; we tunnel it through socks5 localhost:1055.
"""
import sys, socket, threading, socks

LOCAL_PORT = int(sys.argv[1])
REMOTE_HOST = sys.argv[2]
REMOTE_PORT = int(sys.argv[3])
SOCKS_PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 1055
SOCKS_HOST = "localhost"

def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try: s.close()
            except Exception: pass

def handle(client):
    try:
        remote = socks.socksocket()
        remote.set_proxy(socks.SOCKS5, SOCKS_HOST, SOCKS_PORT, rdns=True)
        remote.settimeout(15)
        remote.connect((REMOTE_HOST, REMOTE_PORT))
        remote.settimeout(None)
    except Exception as e:
        print(f"[forward] connect to {REMOTE_HOST}:{REMOTE_PORT} failed: {e}", flush=True)
        try: client.close()
        except Exception: pass
        return
    threading.Thread(target=pipe, args=(client, remote), daemon=True).start()
    threading.Thread(target=pipe, args=(remote, client), daemon=True).start()

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", LOCAL_PORT))
srv.listen(16)
print(f"[forward] 127.0.0.1:{LOCAL_PORT} -> socks5://{SOCKS_HOST}:{SOCKS_PORT} -> {REMOTE_HOST}:{REMOTE_PORT}", flush=True)
while True:
    client, _ = srv.accept()
    threading.Thread(target=handle, args=(client,), daemon=True).start()
