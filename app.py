"""Web app for whatkilled.me. Keeps the WCL credentials on the server.

Run with `python app.py`. HOST and PORT are read from the environment.
"""

import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict, defaultdict, deque

import requests
from flask import Flask, jsonify, request, send_from_directory
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix

from analyze import RAID_DIFFICULTIES, AnalysisError, analyze_fight, fight_from_url
from wcl_client import WCLClient, extract_report_code, load_env

log = logging.getLogger("whatkilled")

REPORT_CODE_RE = re.compile(r"^(a:)?[A-Za-z0-9]{8,32}$")
MAX_PLAYER_NAME_LENGTH = 40
CACHE_TTL_S = 15 * 60
CACHE_MAX_ENTRIES = 200
# Each uncached analysis spends the app's shared WCL API points
RATE_LIMIT_REQUESTS = 12
RATE_LIMIT_WINDOW_S = 60
MAX_URL_LENGTH = 300

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Behind a host's proxy every request comes from the proxy, so the rate limit
# needs the visitor address it adds. Only the proxy's own entry is trusted.
if os.environ.get("TRUST_PROXY"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

load_env()
client = WCLClient(os.environ.get("WCL_CLIENT_ID", ""), os.environ.get("WCL_CLIENT_SECRET", ""))


class TTLCache:
    def __init__(self, ttl_s: int, max_entries: int):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.monotonic() - stored_at > self.ttl_s:
                del self.entries[key]
                return None
            self.entries.move_to_end(key)
            return value

    def set(self, key, value):
        with self.lock:
            self.entries[key] = (time.monotonic(), value)
            self.entries.move_to_end(key)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)


class RateLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit = limit
        self.window_s = window_s
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            hits = self.hits[key]
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


cache = TTLCache(CACHE_TTL_S, CACHE_MAX_ENTRIES)
limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_S)


def error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


def parse_report(value: str | None) -> str | None:
    if not value or len(value) > MAX_URL_LENGTH:
        return None
    code = extract_report_code(value)
    return code if REPORT_CODE_RE.match(code) else None


def client_ip() -> str:
    return request.remote_addr or "unknown"


def cached_call(key: tuple, fn):
    """Return a cached result, or run fn if this visitor is within the rate limit."""
    value = cache.get(key)
    if value is not None:
        return value, None
    if not limiter.allow(client_ip()):
        return None, error("Too many requests. Wait a minute and try again.", 429)
    value = fn()
    cache.set(key, value)
    return value, None


@app.errorhandler(RuntimeError)
def on_graphql_error(e):
    # WCL answers GraphQL errors for private, deleted and mistyped reports alike
    log.warning("WCL query failed: %s", e)
    return error("Couldn't open that report. Check the link, and that the log isn't private.", 404)


@app.errorhandler(requests.RequestException)
def on_upstream_error(e):
    response = getattr(e, "response", None)
    status = response.status_code if response is not None else None
    log.warning("WCL request failed with status %s", status)
    if status == 429:
        return error("Warcraft Logs is limiting requests right now. Try again later.", 503)
    return error("Couldn't reach Warcraft Logs. Try again in a moment.", 502)


@app.errorhandler(AnalysisError)
def on_analysis_error(e):
    return error(str(e), 400, players=e.players)


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/report")
def get_report():
    raw = request.args.get("url", "")
    code = parse_report(raw)
    if code is None:
        return error("That doesn't look like a Warcraft Logs report link.", 400)

    def load():
        report = client.get_report_fights_and_players(code)
        fights = [
            {
                "id": f["id"],
                "name": f["name"],
                "duration_s": (f["endTime"] - f["startTime"]) / 1000,
                "kill": bool(f.get("kill")),
                "keystone": f.get("keystoneLevel"),
                "difficulty": RAID_DIFFICULTIES.get(f.get("difficulty")),
                "boss_pct": f.get("fightPercentage") or 0,
                "encounter": (f.get("encounterID") or 0) > 0,
            }
            for f in report.get("fights") or []
        ]
        return {"code": code, "title": report.get("title"), "fights": fights}

    value, failure = cached_call(("report", code), load)
    if failure:
        return failure
    return jsonify({**value, "fight_hint": fight_from_url(raw)})


@app.get("/api/analyze")
def get_analysis():
    code = parse_report(request.args.get("report"))
    if code is None:
        return error("That doesn't look like a Warcraft Logs report link.", 400)
    fight = request.args.get("fight", type=int)
    if fight is None:
        return error("Pick a fight.", 400)
    compare = request.args.get("compare", type=int)
    if compare == fight:
        compare = None
    player = request.args.get("player") or None
    # Logs can hold anonymized names, so this only bounds the length
    if player and len(player) > MAX_PLAYER_NAME_LENGTH:
        return error("That isn't a valid character name.", 400)

    key = ("analysis", code, fight, (player or "").lower(), compare)
    value, failure = cached_call(key, lambda: analyze_fight(client, code, fight, player, compare))
    return failure or jsonify(value)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not client.client_id or not client.client_secret:
        sys.exit("Set WCL_CLIENT_ID and WCL_CLIENT_SECRET in .env or the environment.")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    log.info("Serving on http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=8)
