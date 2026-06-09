#!/usr/bin/env python3
"""
飞书 webhook 中转服务。
接收飞书消息后，同时转发给所有后端 bot（imini/kms 等）。
"""

import os
import threading

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# 所有需要接收消息的后端
BACKENDS = [
    "http://127.0.0.1:8080/feishu/webhook",  # imini
    "http://127.0.0.1:8082/feishu/webhook",  # kms
]

RELAY_PORT = int(os.getenv("RELAY_PORT", "8079"))


def _forward(url: str, body: dict) -> None:
    try:
        requests.post(url, json=body, timeout=5)
    except Exception as e:
        print(f"[relay] 转发失败 {url}: {e}")


@app.route("/feishu/webhook", methods=["POST"])
def relay():
    body = request.get_json(force=True) or {}

    # URL 验证直接返回，不需要转发
    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})

    for url in BACKENDS:
        threading.Thread(target=_forward, args=(url, body), daemon=True).start()

    return jsonify({"code": 0})


if __name__ == "__main__":
    print(f"[relay] 监听端口 {RELAY_PORT}，转发至 {BACKENDS}")
    app.run(host="0.0.0.0", port=RELAY_PORT)
