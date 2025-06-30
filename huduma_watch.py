#!/usr/bin/env python3
"""
Kenya Service Status Bot (Twitter only)
"""

from __future__ import annotations
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from threading import Thread
from typing import Deque, Dict, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string
from prometheus_client import CollectorRegistry, Gauge, generate_latest

try:
    import tweepy
except ImportError:  # pragma: no cover
    tweepy = None

# Configuration
SITES: Dict[str, str] = {
    "KRA iTax": "https://itax.kra.go.ke",
    "eCitizen": "https://accounts.ecitizen.go.ke",
    "NTSA TIMS": "https://timsvirl.ntsa.go.ke",
    "HELB": "https://studentportal.helb.co.ke",
    "KUCCPS": "https://students.kuccps.net",
}

TIMEOUT_SECONDS = 10
SLOW_THRESHOLD_MS = 4_000
CHECK_INTERVAL_MINUTES = 5
HISTORY_ENTRIES = 288
STATE_FILE = "status_history.json"

# Load environment
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s – %(message)s",
)

history: Dict[str, Deque[Tuple[float, int, int]]] = {
    name: deque(maxlen=HISTORY_ENTRIES) for name in SITES
}

registry = CollectorRegistry()
g_latency = Gauge("service_latency_ms", "Latency in milliseconds", ["service"], registry=registry)
g_up = Gauge("service_up", "1 = up, 0 = down", ["service"], registry=registry)

def twitter_client():
    if not tweepy:
        return None
    try:
        auth = tweepy.OAuth1UserHandler(
            os.getenv("TW_CONSUMER_KEY"),
            os.getenv("TW_CONSUMER_SECRET"),
            os.getenv("TW_ACCESS_TOKEN"),
            os.getenv("TW_ACCESS_SECRET"),
        )
        return tweepy.API(auth)
    except Exception as exc:
        logging.warning("Twitter disabled: %s", exc)
        return None

tw_api = twitter_client()

def measure(url: str, timeout: int = TIMEOUT_SECONDS) -> Tuple[Optional[int], Optional[int]]:
    start = time.perf_counter()
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True)
        latency = int((time.perf_counter() - start) * 1000)
        return r.status_code, latency
    except Exception:
        return None, None

def classify(code: Optional[int], latency: Optional[int]) -> str:
    if code is None or latency is None:
        return "DOWN"
    if code >= 500 or latency > SLOW_THRESHOLD_MS:
        return "SLOW"
    return "OK"

def _last_status(entries: Deque[Tuple[float, int, int]]):
    if not entries:
        return None, None
    _, code, lat = entries[-1]
    return (None if code == 0 else code, None if lat == -1 else lat)

def compose_message(service: str, state: str, code: Optional[int], latency: Optional[int]) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%b %d %H:%M")
    if state == "DOWN":
        return f"❌ {service} seems DOWN (timeout >{TIMEOUT_SECONDS}s) – {now}"
    if state == "SLOW":
        return (
            f"⚠️ {service} is *slow* ({latency/1000:.1f}s, HTTP {code}) – {now}\n"
            "Hang tight or try off-peak 🕒"
        )
    return ""

def send_alert(message: str):
    if tw_api:
        try:
            tw_api.update_status(message)
            logging.info("Tweeted → %s", message)
        except Exception as exc:
            logging.error("Twitter error: %s", exc)

def check_services():
    for name, url in SITES.items():
        prev_code, prev_latency = _last_status(history[name])
        prev_state = classify(prev_code, prev_latency)

        code, latency = measure(url)
        state = classify(code, latency)

        history[name].append((time.time(), code or 0, latency or -1))

        g_up.labels(service=name).set(1 if state == "OK" else 0)
        if latency is not None:
            g_latency.labels(service=name).set(latency)

        if state in {"DOWN", "SLOW"} and state != prev_state:
            send_alert(compose_message(name, state, code, latency))

def load_history():
    if not os.path.isfile(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        for name, entries in data.items():
            dq = history.get(name) or deque(maxlen=HISTORY_ENTRIES)
            for ts, code, lat in entries[-HISTORY_ENTRIES:]:
                dq.append((ts, code, lat))
            history[name] = dq
        logging.info("History restored from %s", STATE_FILE)
    except Exception as exc:
        logging.warning("Could not load history: %s", exc)

def save_history():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fp:
            json.dump({n: list(d) for n, d in history.items()}, fp)
        logging.debug("History saved (%s)", STATE_FILE)
    except Exception as exc:
        logging.error("Failed to save history: %s", exc)

app = Flask(__name__)

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bulma@1.0.0/css/bulma.min.css">
  <title>Kenya Service Status</title>
</head>
<body>
<section class="section">
  <div class="container">
    <h1 class="title is-3">Kenya Service Status</h1>
    <p class="subtitle is-6">Updated {{ updated }}</p>
    <table class="table is-striped is-fullwidth">
      <thead>
        <tr><th>Service</th><th>Status</th><th>Latency (ms)</th><th>HTTP</th></tr>
      </thead>
      <tbody>
        {% for svc in services %}
        <tr>
          <td>{{ svc.name }}</td>
          <td>
            {% if svc.state == 'OK' %}<span class="tag is-success">OK</span>{% endif %}
            {% if svc.state == 'SLOW' %}<span class="tag is-warning">SLOW</span>{% endif %}
            {% if svc.state == 'DOWN' %}<span class="tag is-danger">DOWN</span>{% endif %}
          </td>
          <td>{{ '{:,}'.format(svc.latency) if svc.latency else '—' }}</td>
          <td>{{ svc.code if svc.code else '—' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
</body>
</html>"""

@app.route("/")
def dashboard():
    services = []
    for name, entries in history.items():
        code, latency = _last_status(entries)
        services.append({
            "name": name,
            "code": code,
            "latency": latency,
            "state": classify(code, latency),
        })
    return render_template_string(
        _HTML_TEMPLATE,
        services=sorted(services, key=lambda s: s["name"]),
        updated=datetime.now().strftime("%b %d %H:%M:%S"),
    )

@app.route("/metrics")
def metrics():
    return generate_latest(registry), 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}

def start_dashboard():
    from waitress import serve
    serve(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    load_history()
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_services, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(save_history, "interval", minutes=5)
    scheduler.start()
    check_services()

    Thread(target=start_dashboard, daemon=True).start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        save_history()
        logging.info("Shutting down")
