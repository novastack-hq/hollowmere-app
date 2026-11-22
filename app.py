"""Hollowmere Festival app: the line-up and your wristband, on your phone."""

import json
import logging
import os
import random
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import segno
from flask import Flask, g, jsonify, make_response, redirect, render_template, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# Every running box can say which release it is. CI builds the image from the
# tagged commit on the production branch.
APP_VERSION = "1.0.1"

app = Flask(__name__)
# Behind a tunnel or Cloud Run, the visitor's address arrives in X-Forwarded-*
# headers. Trust one proxy so links (the QR code) use the public https address.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Settings live outside the image: Compose (or Cloud Run) passes them in.
FEST_NAME = os.environ.get("FEST_NAME", "Hollowmere Festival")
# Release 1.0.1 (lab FEST-46): make /lineup slow on purpose, to test a latency
# alert. SLOW_MS=800 waits 0.8 seconds before every /lineup answer; 0 (the
# default) changes nothing. A setting, so ops/compose.yaml decides, not the image.
SLOW_MS = int(os.environ.get("SLOW_MS", "0"))
LINEUP = json.loads(Path(__file__).with_name("lineup.json").read_text(encoding="utf-8"))

# The ticketing system knows these two tickets by number (the made-up story's
# regulars). Phones that scan the QR code get a random ticket instead.
TICKETS = {"102": "General", "103": "VIP"}

# ---------------------------------------------------------------------------
# Logs: one JSON line per event, written to stdout (Docker collects stdout).
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """Turn a log record into one line of JSON that a machine can search."""

    def format(self, record):
        line = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "event": record.getMessage(),
            "version": APP_VERSION,
        }
        line.update(getattr(record, "fields", {}))
        return json.dumps(line)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("fest")
log.addHandler(handler)
# The level is a setting, not code: LOG_LEVEL=DEBUG shows more.
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
log.propagate = False
# Flask's own access lines would include visitors' IP addresses: switch them off.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Metrics: numbers Prometheus collects every few seconds from /metrics.
# ---------------------------------------------------------------------------
disable_created_metrics()
REQUESTS = Counter(
    "fest_requests", "Requests answered, by route and status code", ["path", "status"]
)
LATENCY = Histogram(
    "fest_request_duration_seconds", "How long each request took, in seconds", ["path"]
)

# Routes that are noise in the logs: health checks and the projector page.
QUIET = {"/health", "/crew/stats"}


@app.before_request
def start_timer():
    # A request ID ties together everything that happened for one request.
    # Keep the caller's ID if it sent one (X-Request-ID); otherwise make one up.
    g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
    g.started = time.perf_counter()


@app.after_request
def log_and_count(response):
    seconds = time.perf_counter() - g.started
    # The route ("/lineup"), not the full URL: one label per page, not per visitor.
    path = request.url_rule.rule if request.url_rule else "other"
    if path not in ("/metrics", "/static/<path:filename>"):
        REQUESTS.labels(path=path, status=str(response.status_code)).inc()
        LATENCY.labels(path=path).observe(seconds)
        fields = {
            "request_id": g.request_id,
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "ms": round(seconds * 1000, 1),
        }
        if g.get("ticket"):
            fields["ticket"] = g.ticket
            fields["tier"] = g.tier
        level = logging.DEBUG if path in QUIET else logging.INFO
        log.log(level, "request", extra={"fields": fields})
    response.headers["X-Request-ID"] = g.request_id
    return response


@app.errorhandler(Exception)
def something_went_wrong(error):
    # A 404 or 405 is the visitor's mistake, not ours: answer it as Flask would.
    if isinstance(error, HTTPException):
        return error
    # Log the whole story once, with the request ID; give the visitor a short
    # apology and a reference they can quote to festival support.
    log.error(
        "unhandled error",
        extra={
            "fields": {
                "request_id": g.get("request_id", "-"),
                "path": request.path,
                "ticket": g.get("ticket"),
                "tier": g.get("tier"),
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
        },
    )
    reference = g.get("request_id", "-")
    if wants_page():
        return render_template("error.html", fest=FEST_NAME, reference=reference), 500
    return jsonify(error="Sorry, something went wrong.", reference=reference), 500


# ---------------------------------------------------------------------------
# Wristbands. Scanning the QR code opens /join, which deals a random ticket
# number and remembers it in a cookie. No names, no accounts, no personal data.
# ---------------------------------------------------------------------------
# Ticket numbers that look like HTTP status codes would confuse a grep.
NOT_DEALT = {200, 201, 204, 301, 302, 304, 400, 401, 403, 404, 405, 429, 500, 502, 503, 504}
_lock = threading.Lock()
_joined = 0
_seen = {}  # ticket -> (tier, last time its phone asked for the line-up)


def deal_ticket():
    """A new ticket number and tier. Exactly one wristband in five is VIP."""
    global _joined
    with _lock:
        _joined += 1
        tier = "VIP" if _joined % 5 == 0 else "General"
        while True:
            number = random.randint(200, 999)
            if number not in NOT_DEALT and str(number) not in _seen:
                break
        _seen[str(number)] = (tier, time.time())
    return str(number), tier


def who_is_asking():
    """The ticket and tier of this request: ?ticket= wins, then the cookie."""
    ticket = request.args.get("ticket") or request.cookies.get("ticket")
    if not ticket:
        return None, None
    tier = TICKETS.get(ticket) or request.cookies.get("tier")
    return ticket, tier


def wants_page():
    # A browser asks for HTML; curl and scripts get JSON (or add ?format=json).
    return "text/html" in request.headers.get("Accept", "") and request.args.get("format") != "json"


# ---------------------------------------------------------------------------
# The line-up.
# ---------------------------------------------------------------------------
def lineup_for(tier):
    """The sets this wristband can see. Release 1.0: everyone sees the same."""
    return LINEUP


@app.get("/")
def home():
    if request.cookies.get("ticket"):
        return redirect("/lineup")
    return render_template("welcome.html", fest=FEST_NAME, version=APP_VERSION)


@app.get("/join")
def join():
    response = make_response(redirect("/lineup"))
    if not request.cookies.get("ticket"):  # scanning twice keeps your wristband
        ticket, tier = deal_ticket()
        for name, value in (("ticket", ticket), ("tier", tier)):
            response.set_cookie(name, value, max_age=2 * 24 * 3600, samesite="Lax")
    return response


@app.get("/lineup")
def lineup():
    if SLOW_MS > 0:  # the headliner reveal: everyone asks at once
        time.sleep(SLOW_MS / 1000)
    g.ticket, g.tier = who_is_asking()
    if wants_page():
        if not g.ticket:
            return redirect("/join")
        return render_template("lineup.html", fest=FEST_NAME, version=APP_VERSION)
    sets = lineup_for(g.tier)
    if g.ticket and request.cookies.get("ticket") == g.ticket:
        with _lock:
            _seen[g.ticket] = (g.tier, time.time())
    return jsonify(
        festival=FEST_NAME, version=APP_VERSION, ticket=g.ticket, tier=g.tier, sets=sets
    )


# ---------------------------------------------------------------------------
# The crew's projector page: a QR code to join, and who is online.
# ---------------------------------------------------------------------------
@app.get("/crew")
def crew():
    join_url = os.environ.get("PUBLIC_URL", request.host_url).rstrip("/") + "/join"
    qr = segno.make(join_url, error="m").svg_inline(scale=12, dark="#0b0716", light="#ffffff")
    return render_template("crew.html", fest=FEST_NAME, version=APP_VERSION, qr=qr, url=join_url)


@app.get("/crew/stats")
def crew_stats():
    now = time.time()
    with _lock:
        online = {t: tier for t, (tier, seen) in _seen.items() if now - seen < 30}
    vips = sorted(t for t, tier in online.items() if tier == "VIP")
    return jsonify(version=APP_VERSION, festival=FEST_NAME, online=len(online), vip=vips)


# ---------------------------------------------------------------------------
# For the crew and their tools.
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    # Liveness: "the process is up and answering". Docker asks every 5 seconds.
    return jsonify(status="ok")


@app.get("/version")
def version():
    # Which release is running? The first question in any incident.
    return jsonify(version=APP_VERSION)


@app.get("/metrics")
def metrics():
    # Prometheus reads this page. Open it in a browser to see the raw numbers.
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))  # Cloud Run sets PORT
    print(f"{FEST_NAME} app {APP_VERSION} is ready: http://localhost:{port}", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)
