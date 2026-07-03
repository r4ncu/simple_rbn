#!/usr/bin/env python3
import http.server
import http.client
import json
import socket
import ssl
import sys
import time
import threading
import urllib.parse

import os
PORT = int(os.environ.get("PORT", 8080))
RBN_HOST = "reversebeacon.net"
INITIAL_HASH = os.environ.get("RBN_HASH", "ab6db5")

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

current_hash = INITIAL_HASH
hash_lock = threading.Lock()

class PersistentRBN:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None
        self._last_used = 0
        self._cache = {}
        self._cache_lock = threading.Lock()

    def _connect(self):
        if self._conn:
            try:
                self._conn.request("HEAD", "/")
                self._conn.getresponse().read()
            except Exception:
                self._conn = None
        if not self._conn:
            self._conn = http.client.HTTPSConnection(
                RBN_HOST, timeout=10, context=ssl_ctx
            )
        self._last_used = time.time()

    def get(self, path, timeout=8):
        with self._lock:
            try:
                self._connect()
                self._conn.request("GET", path, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Connection": "keep-alive"
                })
                resp = self._conn.getresponse()
                data = resp.read()
                if resp.status == 200:
                    return resp.status, data
                else:
                    self._conn = None
                    return resp.status, data
            except Exception as e:
                self._conn = None
                print(f"[RBN] Error: {e}", flush=True)
                return None, None

rbn = PersistentRBN()

def update_hash_from_response(data):
    global current_hash
    try:
        obj = json.loads(data)
        new_hash = obj.get("ver_h")
        if new_hash and new_hash != current_hash:
            with hash_lock:
                current_hash = new_hash
            print(f"[HASH] Updated: {new_hash}", flush=True)
            return True
    except Exception:
        pass
    return False

def rewrite_hash_in_qs(path, new_hash):
    if "h=" not in path:
        return path
    parts = path.split("?")
    if len(parts) < 2:
        return path
    base = parts[0]
    qs = parts[1]
    params = []
    for kv in qs.split("&"):
        if kv.startswith("h="):
            params.append("h=" + new_hash)
        else:
            params.append(kv)
    return base + "?" + "&".join(params)

class DualStackHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/hash"):
            h = current_hash
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"hash": h}).encode())
        elif self.path.startswith("/api/spots"):
            qs = self.path[len("/api/spots"):]
            with hash_lock:
                h = current_hash
            path = "/spots.php" + rewrite_hash_in_qs(qs, h)
            print(f"[PROXY] {path}")
            sys.stdout.flush()
            status, data = rbn.get(path)
            if status == 200 and data:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            elif data and update_hash_from_response(data):
                h2 = current_hash
                path2 = "/spots.php" + rewrite_hash_in_qs(qs, h2)
                print(f"[PROXY] retry with new hash: {path2}")
                sys.stdout.flush()
                status2, data2 = rbn.get(path2)
                if status2 == 200 and data2:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data2)
                else:
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Proxy error: upstream unavailable")
            else:
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Proxy error: upstream unavailable")
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/spotted.html"
            super().do_GET()
        else:
            super().do_GET()

    def log_message(self, format, *args):
        print(format % args)

def keepalive():
    while True:
        time.sleep(30)
        with rbn._lock:
            if rbn._conn and time.time() - rbn._last_used > 60:
                try:
                    rbn._conn.close()
                except Exception:
                    pass
                rbn._conn = None

if __name__ == "__main__":
    threading.Thread(target=keepalive, daemon=True).start()
    server = DualStackHTTPServer(("::", PORT), ProxyHandler)
    print(f"Server running at http://localhost:{PORT}")
    server.serve_forever()
