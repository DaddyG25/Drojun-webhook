import hashlib
import hmac
import os
import time
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request


app = Flask(__name__)

ALLOWED_PATHS = {
    "",
    "health",
    "admin/credentials",
    "broker/status",
    "journal.csv",
    "webhook",
    "kite/login",
    "kite/callback",
    "kite/credentials",
}
REQUEST_HEADERS = {"Authorization", "Content-Type", "Cookie", "User-Agent"}
RESPONSE_HEADERS = {"Content-Type", "Location", "Set-Cookie", "WWW-Authenticate"}


def canonical_path() -> str:
    return request.full_path[:-1] if request.full_path.endswith("?") else request.full_path


def signature(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    payload = f"{timestamp}\n{method.upper()}\n{path}\n".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def proxy(path: str):
    if path not in ALLOWED_PATHS:
        return jsonify(error="not found"), 404
    base_url = os.getenv("EXECUTOR_BASE_URL", "").rstrip("/") + "/"
    shared_secret = os.getenv("GATEWAY_SHARED_SECRET")
    if not base_url.startswith("https://") or not shared_secret:
        return jsonify(error="gateway is not configured"), 503
    body = request.get_data(cache=True)
    timestamp = str(int(time.time()))
    path_and_query = canonical_path()
    headers = {name: value for name, value in request.headers.items() if name in REQUEST_HEADERS}
    headers["X-Drojun-Gateway-Timestamp"] = timestamp
    headers["X-Drojun-Gateway-Signature"] = signature(
        shared_secret,
        timestamp,
        request.method,
        path_and_query,
        body,
    )
    headers["X-Forwarded-For"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    headers["X-Forwarded-Proto"] = "https"
    try:
        upstream = requests.request(
            method=request.method,
            url=urljoin(base_url, path_and_query.lstrip("/")),
            headers=headers,
            data=body,
            timeout=20,
            allow_redirects=False,
        )
    except requests.RequestException:
        return jsonify(error="executor unavailable"), 502
    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name in RESPONSE_HEADERS
    }
    return Response(upstream.content, status=upstream.status_code, headers=response_headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

