#!/usr/bin/env python3
"""Local HTTP-CONNECT proxy dat verzoeken tunnelt naar een upstream SOCKS5-with-auth.

Playwright/Chromium ondersteunt SOCKS5 alleen zonder authenticatie. Deze bridge
biedt een lokale unauth HTTP CONNECT proxy die de auth-heavy SOCKS5 upstream
(bijv. NordVPN nl.socks.nordhold.net) afhandelt.

Usage:
  ./socks5_bridge.py --listen 127.0.0.1:1088
  (leest kensa.nordvpn_socks5 uit /home/pi/.openclaw/secrets.json)
"""

import argparse
import json
import socket
import socketserver
import struct
import sys
import threading
from pathlib import Path

SECRETS = Path("/home/pi/.openclaw/secrets.json")


def _upstream() -> dict:
    n = json.loads(SECRETS.read_text()).get("kensa", {}).get("nordvpn_socks5") or {}
    return {"host": n["host"], "port": int(n["port"]),
            "user": n["username"], "pw": n["password"]}


def _socks5_connect(target_host: str, target_port: int, up: dict) -> socket.socket:
    """Open SOCKS5 tunnel with user/pw auth to (target_host, target_port)."""
    s = socket.create_connection((up["host"], up["port"]), timeout=30)
    # Greeting: version 5, 1 method (0x02 = user/pw)
    s.sendall(b"\x05\x01\x02")
    resp = s.recv(2)
    if resp != b"\x05\x02":
        s.close()
        raise OSError(f"SOCKS5 method reject: {resp!r}")
    # Auth sub-negotiation (RFC1929)
    u = up["user"].encode()
    p = up["pw"].encode()
    s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
    resp = s.recv(2)
    if resp != b"\x01\x00":
        s.close()
        raise OSError(f"SOCKS5 auth fail: {resp!r}")
    # Connect request: ver, cmd=CONNECT, rsv, atyp=DOMAIN
    h = target_host.encode()
    if len(h) > 255:
        s.close()
        raise OSError("host too long")
    req = b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack("!H", target_port)
    s.sendall(req)
    hdr = s.recv(4)
    if len(hdr) < 4 or hdr[1] != 0x00:
        s.close()
        raise OSError(f"SOCKS5 connect fail: {hdr!r}")
    # Skip bound address (atyp-dependent)
    atyp = hdr[3]
    if atyp == 1:
        s.recv(4 + 2)
    elif atyp == 3:
        ln = s.recv(1)[0]
        s.recv(ln + 2)
    elif atyp == 4:
        s.recv(16 + 2)
    return s


def _splice(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try: a.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: b.shutdown(socket.SHUT_RDWR)
        except OSError: pass


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(60)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 16384:
                    return
            first = data.split(b"\r\n", 1)[0].decode("latin-1")
            parts = first.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            hostport = parts[1]
            if ":" not in hostport:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            host, _, port_s = hostport.rpartition(":")
            port = int(port_s)
            try:
                up = _socks5_connect(host, port, self.server.up)
            except OSError as e:
                client.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{e}".encode())
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            t = threading.Thread(target=_splice, args=(client, up), daemon=True)
            t.start()
            _splice(up, client)
            t.join(timeout=5)
        except Exception as e:
            try: client.sendall(f"HTTP/1.1 500 {e}\r\n\r\n".encode())
            except OSError: pass


class ThreadingTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:1088")
    args = ap.parse_args()
    host, port_s = args.listen.split(":")
    port = int(port_s)
    up = _upstream()
    print(f"[socks-bridge] listening {host}:{port} → socks5://{up['host']}:{up['port']}",
          file=sys.stderr)
    srv = ThreadingTCP((host, port), ConnectHandler)
    srv.up = up
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
