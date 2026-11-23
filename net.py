"""
Networking for Binance Price Viewer (standard library only).

- Resolver: looks up hostnames through a chosen public DNS server
  (Cloudflare, Google, Quad9, ...) using DNS-over-HTTPS or plain UDP DNS,
  instead of the system / ISP resolver.
- BinanceClient: keep-alive HTTPS client that connects to the resolved IP
  while still verifying the TLS certificate against the real hostname.
- PriceWorker / ChartWorker: background pollers that talk to the UI through
  a queue.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import struct
import threading
import time
from decimal import Decimal

USER_AGENT = "binance-price-viewer/1.1"
MAX_SYMBOLS_PER_REQUEST = 100
MIN_REFRESH_MS = 250

# key -> (label, [server IPs])
DNS_PROVIDERS: dict[str, tuple[str, list[str]]] = {
    "cloudflare": ("Cloudflare", ["1.1.1.1", "1.0.0.1"]),
    "google": ("Google", ["8.8.8.8", "8.8.4.4"]),
    "quad9": ("Quad9", ["9.9.9.9", "149.112.112.112"]),
    "adguard": ("AdGuard", ["94.140.14.14", "94.140.15.15"]),
    "opendns": ("OpenDNS", ["208.67.222.222", "208.67.220.220"]),
    "system": ("System (OS default)", []),
    "custom": ("Custom", []),
}
DNS_MODES = {
    "doh": "DNS over HTTPS (port 443)",
    "dot": "DNS over TLS (port 853)",
    "udp": "Plain DNS (UDP 53) - often hijacked by ISPs",
}
MODE_SHORT = {"doh": "DoH", "dot": "DoT", "udp": "UDP"}
# Encrypted modes fall back to each other, never to plain (hijackable) UDP.
MODE_ORDER = {"doh": ["doh", "dot"], "dot": ["dot", "doh"], "udp": ["udp"]}


# --------------------------------------------------------------------------
# Minimal DNS wire-format (RFC 1035) for A records
# --------------------------------------------------------------------------
def _build_query(name: str, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD=1
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.strip(".").split(".")) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN


def _skip_name(buf: bytes, off: int) -> int:
    while True:
        ln = buf[off]
        if ln == 0:
            return off + 1
        if ln & 0xC0 == 0xC0:  # compression pointer
            return off + 2
        off += ln + 1


def _parse_response(buf: bytes, qid: int | None) -> tuple[list[str], int]:
    rid, flags, qd, an, _, _ = struct.unpack(">HHHHHH", buf[:12])
    if qid is not None and rid != qid:
        raise ValueError("DNS id mismatch")
    rcode = flags & 0x000F
    if rcode != 0:
        raise ValueError({3: "NXDOMAIN", 2: "SERVFAIL", 5: "REFUSED"}.get(rcode, f"rcode {rcode}"))
    off = 12
    for _ in range(qd):
        off = _skip_name(buf, off) + 4
    ips, ttl_min = [], 3600
    for _ in range(an):
        off = _skip_name(buf, off)
        rtype, _rclass, ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(buf[off:off + 4]))
            ttl_min = min(ttl_min, ttl)
        off += rdlen
    if not ips:
        raise ValueError("no A records")
    return ips, max(30, ttl_min)


def _query_udp(server: str, name: str, timeout: float) -> tuple[list[str], int]:
    qid = random.randint(0, 0xFFFF)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(_build_query(name, qid), (server, 53))
        data, _ = s.recvfrom(4096)
    return _parse_response(data, qid)


def _query_doh(server: str, name: str, timeout: float) -> tuple[list[str], int]:
    # RFC 8484 POST to https://<server-ip>/dns-query; the TLS certificate of
    # these providers is valid for their IP address, so it is fully verified.
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(server, 443, timeout=timeout, context=ctx)
    try:
        conn.request("POST", "/dns-query", body=_build_query(name, 0),
                     headers={"Content-Type": "application/dns-message",
                              "Accept": "application/dns-message", "User-Agent": USER_AGENT})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise ValueError(f"DoH HTTP {resp.status}")
        return _parse_response(data, None)
    finally:
        conn.close()


def _query_dot(server: str, name: str, timeout: float) -> tuple[list[str], int]:
    # RFC 7858: DNS over TLS on port 853, length-prefixed messages.
    ctx = ssl.create_default_context()
    qid = random.randint(0, 0xFFFF)
    q = _build_query(name, qid)
    with socket.create_connection((server, 853), timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=server) as s:
            s.sendall(struct.pack(">H", len(q)) + q)
            buf = b""
            while len(buf) < 2 or len(buf) < 2 + struct.unpack(">H", buf[:2])[0]:
                chunk = s.recv(4096)
                if not chunk:
                    raise ValueError("DoT connection closed")
                buf += chunk
    return _parse_response(buf[2:2 + struct.unpack(">H", buf[:2])[0]], qid)


_QUERY_FN = {"doh": _query_doh, "dot": _query_dot, "udp": _query_udp}


# Public encrypted resolvers tried, in this order, when the chosen one is
# unreachable (office / campus firewalls often block 1.1.1.1 and 8.8.8.8).
FALLBACK_PROVIDERS = ["cloudflare", "quad9", "adguard", "opendns", "google"]


def _race(candidates, host: str, timeout: float):
    """Query all (label, server, mode, delay) candidates in parallel; each
    starts after its `delay` seconds unless another has already answered.

    Returns (ips, ttl, label, server, mode) from the first that answers, or
    raises OSError listing every failure."""
    if not candidates:
        raise OSError("no DNS servers to query")
    done = threading.Event()
    lock = threading.Lock()
    result: list = []
    errors: list[str] = []
    remaining = [len(candidates)]

    def run(label, server, mode, delay):
        try:
            if delay and done.wait(delay):
                return
            ips, ttl = _QUERY_FN[mode](server, host, timeout)
            with lock:
                if not result:
                    result.append((ips, ttl, label, server, mode))
            done.set()
        except Exception as e:
            with lock:
                errors.append(f"{server}/{mode}: {e}")
        finally:
            with lock:
                remaining[0] -= 1
                if remaining[0] == 0:
                    done.set()

    for c in candidates:
        threading.Thread(target=run, args=c, daemon=True).start()
    done.wait(max(c[3] for c in candidates) + timeout * 2 + 1)
    with lock:
        if result:
            return result[0]
        raise OSError("; ".join(errors) or "timed out")


class Resolver:
    """Thread-safe resolver with a TTL cache.

    With `fallback` on, a blocked encrypted provider is replaced by the first
    other public provider that answers, and that one is used from then on."""

    def __init__(self, provider: str = "cloudflare", mode: str = "doh",
                 custom_servers: list[str] | None = None, timeout: float = 4.0,
                 fallback: bool = True):
        self.provider = provider if provider in DNS_PROVIDERS else "cloudflare"
        self.mode = mode if mode in DNS_MODES else "doh"
        self.timeout = timeout
        self.fallback = fallback and self.mode != "udp" and self.provider != "system"
        if self.provider == "custom":
            self.servers = [s.strip() for s in (custom_servers or []) if s.strip()]
        else:
            self.servers = list(DNS_PROVIDERS[self.provider][1])
        self.active: str | None = None   # fallback provider in use, if any
        self._cache: dict[str, tuple[list[str], float, str]] = {}
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        name = DNS_PROVIDERS[self.provider][0]
        if self.provider == "system":
            return name
        servers = ", ".join(self.servers) or "(none)"
        if self.provider == "custom":
            text = f"{servers} ({MODE_SHORT[self.mode]})"
        else:
            text = f"{name} {servers} ({MODE_SHORT[self.mode]})"
        return text + (" + auto fallback" if self.fallback else "")

    def _candidates(self, provider: str):
        name = DNS_PROVIDERS[provider][0] if provider != "custom" else "Custom"
        servers = self.servers if provider == self.provider else DNS_PROVIDERS[provider][1]
        return [(name, s, m) for m in MODE_ORDER[self.mode] for s in servers]

    def resolve(self, host: str) -> tuple[list[str], str]:
        """Return (ips, how) where `how` describes which server answered."""
        try:  # already an IP literal
            socket.inet_aton(host)
            return [host], "literal IP"
        except OSError:
            pass
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(host)
            if hit and hit[1] > now:
                return hit[0], hit[2]

        if self.provider == "system" or not self.servers:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            ips = list(dict.fromkeys(i[4][0] for i in infos))
            ttl, how = 60, "System DNS"
        else:
            ips, ttl, how = self._resolve_encrypted(host)
        with self._lock:
            self._cache[host] = (ips, now + ttl, how)
        return ips, how

    # Seconds the preferred provider gets before the fallbacks join the race.
    HEAD_START = 1.5

    def _resolve_encrypted(self, host: str):
        # The provider that worked last (the chosen one, or a fallback) goes
        # first; if it hasn't answered after HEAD_START, every other public
        # encrypted resolver joins the race and the first real answer wins.
        first = self.active or self.provider
        cands = [c + (0.0, first) for c in self._candidates(first)]
        if self.fallback:
            for p in dict.fromkeys([self.provider] + FALLBACK_PROVIDERS):
                if p != first:
                    cands += [c + (self.HEAD_START, p) for c in self._candidates(p)]
        owner = {(c[1], c[2]): c[4] for c in reversed(cands)}
        try:
            ips, ttl, name, server, mode = _race([c[:4] for c in cands], host, self.timeout)
        except OSError as e:
            raise OSError(f"DNS lookup for {host} failed: {e}") from None
        used = owner[(server, mode)]
        self.active = None if used == self.provider else used
        tag = " (fallback)" if self.active else ""
        return ips, ttl, f"{name} {server} ({MODE_SHORT[mode]}){tag}"

    def invalidate(self, host: str):
        with self._lock:
            self._cache.pop(host, None)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a pre-resolved IP; SNI + cert check use the hostname."""

    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


# --------------------------------------------------------------------------
# Binance REST client
# --------------------------------------------------------------------------
class BinanceClient:
    """Keep-alive REST client. `conn_info` describes the live connection."""

    def __init__(self, host: str, resolver: Resolver, timeout: float = 8.0):
        self.host = host
        self.resolver = resolver
        self.timeout = timeout
        self._conn: http.client.HTTPSConnection | None = None
        self.conn_info: dict | None = None

    def _connect(self):
        ips, how = self.resolver.resolve(self.host)
        last_err = None
        for ip in ips[:4]:
            conn = _PinnedHTTPSConnection(self.host, ip, self.timeout)
            try:
                conn.connect()
            except OSError as e:
                last_err = e
                conn.close()
                continue
            self._conn = conn
            self.conn_info = {"host": self.host, "ip": ip, "dns": how}
            return
        self.resolver.invalidate(self.host)
        raise OSError(f"cannot connect to {self.host} ({', '.join(ips[:4])}): {last_err}")

    def _get(self, path: str):
        for attempt in (1, 2):
            if self._conn is None:
                self._connect()
            try:
                self._conn.request("GET", path, headers={
                    "Accept": "application/json", "Connection": "keep-alive",
                    "User-Agent": USER_AGENT})
                resp = self._conn.getresponse()
                body = resp.read()
            except (http.client.HTTPException, OSError):
                self.close()  # stale keep-alive connection: reconnect once
                if attempt == 2:
                    raise
                continue
            if resp.status != 200:
                try:
                    msg = json.loads(body).get("msg", body[:200])
                except Exception:
                    msg = body[:200]
                raise RuntimeError(f"HTTP {resp.status}: {msg}")
            return json.loads(body)
        raise RuntimeError("unreachable")

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self.conn_info = None

    def exchange_info(self) -> dict[str, dict]:
        data = self._get("/api/v3/exchangeInfo?permissions=SPOT&symbolStatus=TRADING")
        out = {}
        for s in data.get("symbols", []):
            tick = None
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick = Decimal(f["tickSize"]).normalize()
            out[s["symbol"]] = {"tick": tick, "base": s.get("baseAsset", ""),
                                "quote": s.get("quoteAsset", "")}
        return out

    def tickers(self, symbols: list[str]) -> list[dict]:
        result = []
        for i in range(0, len(symbols), MAX_SYMBOLS_PER_REQUEST):
            arr = json.dumps(symbols[i:i + MAX_SYMBOLS_PER_REQUEST], separators=(",", ":"))
            arr = arr.replace('"', "%22").replace("[", "%5B").replace("]", "%5D").replace(",", "%2C")
            result.extend(self._get(f"/api/v3/ticker/24hr?symbols={arr}&type=FULL"))
        return result

    def klines(self, symbol: str, interval: str, limit: int = 500) -> list[tuple]:
        rows = self._get(f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
        # (open_time_ms, o, h, l, c, volume, close_time_ms)
        return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), r[6])
                for r in rows]


# --------------------------------------------------------------------------
# Background workers
# --------------------------------------------------------------------------
class _Worker(threading.Thread):
    def __init__(self, out_q, host: str, resolver: Resolver):
        super().__init__(daemon=True)
        self.out_q = out_q
        self._lock = threading.Lock()
        self._host = host
        self._resolver = resolver
        self._net_gen = 0
        self._client: BinanceClient | None = None
        self._client_gen = -1
        self._wake = threading.Event()
        self._stop_evt = threading.Event()

    def set_network(self, host: str, resolver: Resolver):
        with self._lock:
            self._host = host
            self._resolver = resolver
            self._net_gen += 1
        self._wake.set()

    def refresh_now(self):
        self._wake.set()

    def stop(self):
        self._stop_evt.set()
        self._wake.set()

    def _get_client(self) -> BinanceClient:
        with self._lock:
            gen, host, resolver = self._net_gen, self._host, self._resolver
        if self._client is None or self._client_gen != gen:
            if self._client:
                self._client.close()
            self._client = BinanceClient(host, resolver)
            self._client_gen = gen
        return self._client

    def _sleep(self, seconds: float):
        self._wake.wait(max(0.0, seconds))
        self._wake.clear()


class PriceWorker(_Worker):
    """Polls 24h tickers for the favorites; fetches the symbol list once per host."""

    def __init__(self, out_q, host: str, resolver: Resolver):
        super().__init__(out_q, host, resolver)
        self._symbols: list[str] = []
        self._interval = 1.0
        self._info_gen = -1

    def configure(self, symbols=None, interval_ms=None):
        with self._lock:
            if symbols is not None:
                self._symbols = list(symbols)
            if interval_ms is not None:
                self._interval = max(MIN_REFRESH_MS, int(interval_ms)) / 1000.0
        self._wake.set()

    def run(self):
        last_conn = None
        while not self._stop_evt.is_set():
            with self._lock:
                symbols, interval = list(self._symbols), self._interval
            client = self._get_client()
            started = time.perf_counter()
            try:
                data = client.tickers(symbols) if symbols else []
                latency = (time.perf_counter() - started) * 1000
                self.out_q.put(("tickers", data, latency))
            except Exception as e:
                self.out_q.put(("error", str(e)))
                client.close()
            conn = dict(client.conn_info) if client.conn_info else None
            if conn != last_conn:
                last_conn = conn
                self.out_q.put(("conn", conn))
            if self._info_gen != self._client_gen:
                # The symbol list is large (several seconds): load it on a
                # side thread so price updates aren't held up.
                self._info_gen = self._client_gen
                threading.Thread(target=self._load_exchange_info, daemon=True,
                                 args=(self._client_gen, client.host, client.resolver)).start()
            self._sleep(interval - (time.perf_counter() - started))
        if self._client:
            self._client.close()

    def _load_exchange_info(self, gen: int, host: str, resolver: Resolver):
        client = BinanceClient(host, resolver, timeout=20)
        try:
            info = client.exchange_info()
            if gen == self._client_gen:
                self.out_q.put(("exchange_info", info))
        except Exception as e:
            if gen == self._client_gen:
                self._info_gen = -1  # retry on next cycle
                self.out_q.put(("error", f"symbol list: {e}"))
        finally:
            client.close()


class ChartWorker(_Worker):
    """Fetches candlesticks for the selected symbol; refreshes every few seconds."""

    REFRESH_S = 5.0

    def __init__(self, out_q, host: str, resolver: Resolver):
        super().__init__(out_q, host, resolver)
        self._target: tuple[str, str] | None = None

    def set_target(self, symbol: str | None, interval: str):
        with self._lock:
            self._target = (symbol, interval) if symbol else None
        self._wake.set()

    def run(self):
        while not self._stop_evt.is_set():
            with self._lock:
                target = self._target
            if target:
                client = self._get_client()
                try:
                    candles = client.klines(*target)
                    self.out_q.put(("klines", target[0], target[1], candles))
                except Exception as e:
                    self.out_q.put(("chart_error", target[0], target[1], str(e)))
                    client.close()
                self._sleep(self.REFRESH_S)
            else:
                self._sleep(3600)
        if self._client:
            self._client.close()


if __name__ == "__main__":  # quick manual test: py net.py
    for key in ("cloudflare", "google", "quad9", "adguard", "opendns"):
        for mode in ("doh", "dot", "udp"):
            r = Resolver(key, mode, fallback=False)
            t = time.perf_counter()
            try:
                ips, how = r.resolve("api.binance.com")
                print(f"{key:10} {mode}: {ips[:2]} via {how}  {1000 * (time.perf_counter() - t):.0f} ms")
            except Exception as e:
                print(f"{key:10} {mode}: FAIL {e}")
    r = Resolver()  # defaults: Cloudflare DoH with auto fallback
    for i in range(2):
        t = time.perf_counter()
        ips, how = r.resolve("api.binance.com")
        r.invalidate("api.binance.com")
        print(f"default #{i + 1}: {ips[:2]} via {how}  {1000 * (time.perf_counter() - t):.0f} ms")
