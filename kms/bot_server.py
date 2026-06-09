#!/usr/bin/env python3
"""
飞书 webhook 服务，接收飞书机器人消息并执行指令。

可用指令：
  status                          查看所有监控状态
  start <campaign_id>             启动监控
  stop <campaign_id>              停止监控
  set <campaign_id> YYYY-MM-DD HH:MM   设置截止时间并启动
  sort <campaign_id>              生成排序报告
  scan                            立即扫描 KMStation 新活动
  help                            显示帮助
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

import feishu
import orchestrator
from config import BOT_PORT, CAMPAIGNS_DIR, FEISHU_VERIFY_TOKEN
from kms_sort import sort_campaign

app = Flask(__name__)


def _find_campaign(identifier: str) -> Path | None:
    identifier = identifier.strip()
    for folder in orchestrator.campaign_folders():
        cfg = orchestrator.load_campaign(folder)
        if identifier in {folder.name, cfg.get("campaign_id", "")}:
            return folder
    return None


def _normalize_time(value: str) -> str | None:
    value = value.strip()
    if len(value) == 16:
        value += ":00"
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return value
    except ValueError:
        try:
            datetime.fromisoformat(value)
            return value
        except ValueError:
            return None


def handle_command(text: str) -> None:
    text = text.strip()
    lower = text.lower()

    if lower == "status":
        feishu.notify("\n".join(orchestrator.status_lines()))
        return

    if lower == "scan":
        feishu.notify("🔍 开始扫描 KMStation...")
        from kms_scan import mark_known, scan
        found = scan()
        if not found:
            feishu.notify("扫描完成，未发现新活动")
        else:
            for cfg in found:
                folder = orchestrator.save_new_campaign(cfg)
                mark_known(cfg.get("prod_ids", []))
                feishu.notify(
                    f"🆕 发现新活动\n名称: {cfg.get('name', '')}\n"
                    f"prod_ids: {cfg.get('prod_ids', [])}\n"
                    f"请用 set {cfg['campaign_id']} YYYY-MM-DD HH:MM 设置截止时间"
                )
        return

    if lower.startswith("start "):
        cid = text.split(maxsplit=1)[1]
        folder = _find_campaign(cid)
        if folder is None:
            feishu.notify(f"未找到 campaign: {cid}")
            return
        pid = orchestrator.start_monitor(folder)
        feishu.notify(f"✅ KMS 监控已启动\ncampaign: {folder.name}\nPID: {pid}")
        return

    if lower.startswith("stop "):
        cid = text.split(maxsplit=1)[1]
        folder = _find_campaign(cid)
        if folder is None:
            feishu.notify(f"未找到 campaign: {cid}")
            return
        stopped = orchestrator.stop_monitor(folder)
        feishu.notify(f"{'⛔ 已停止' if stopped else '未在运行'}\ncampaign: {folder.name}")
        return

    if lower.startswith("set "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            feishu.notify("格式：set <campaign_id> YYYY-MM-DD HH:MM")
            return
        cid, stop_at = parts[1], _normalize_time(parts[2])
        if stop_at is None:
            feishu.notify("截止时间格式错误，请用：YYYY-MM-DD HH:MM")
            return
        folder = _find_campaign(cid)
        if folder is None:
            feishu.notify(f"未找到 campaign: {cid}")
            return
        cfg = orchestrator.load_campaign(folder)
        cfg["stop_monitor_at"] = stop_at
        orchestrator.save_campaign(folder, cfg)
        pid = orchestrator.start_monitor(folder, force=True)
        feishu.notify(
            f"✅ 截止时间已设置，监控已重启\n"
            f"campaign: {folder.name}\n截止: {stop_at}\nPID: {pid}"
        )
        return

    if lower.startswith("sort "):
        cid = text.split(maxsplit=1)[1]
        folder = _find_campaign(cid)
        if folder is None:
            feishu.notify(f"未找到 campaign: {cid}")
            return
        text_report, xlsx_path = sort_campaign(folder / "campaign.json")
        feishu.notify(text_report)
        if xlsx_path:
            feishu.notify_file(xlsx_path)
        return

    if lower in {"help", "帮助"}:
        feishu.notify(
            "KMS 可用指令（前缀 kms）：\n\n"
            "kms status\n  查看所有监控状态\n\n"
            "kms scan\n  立即扫描 KMStation 新活动\n\n"
            "kms start <campaign_id>\n  启动监控\n\n"
            "kms stop <campaign_id>\n  停止监控\n\n"
            "kms set <campaign_id> YYYY-MM-DD HH:MM\n  设置截止时间并启动\n\n"
            "kms sort <campaign_id>\n  生成排序报告\n\n"
            "kms help\n  显示帮助"
        )
        return

    feishu.notify("未知指令，发送 help 查看可用命令")


@app.route("/feishu/webhook", methods=["POST"])
def webhook():
    body = request.json or {}

    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})

    if FEISHU_VERIFY_TOKEN and body.get("header", {}).get("token", "") != FEISHU_VERIFY_TOKEN:
        return jsonify({"code": 1, "msg": "invalid token"}), 403

    header = body.get("header", {})
    event  = body.get("event", {})
    if header.get("event_type") == "im.message.receive_v1":
        sender  = event.get("sender", {})
        message = event.get("message", {})
        open_id = sender.get("sender_id", {}).get("open_id", "")

        bound_id = feishu.get_user_id()
        if open_id and not bound_id:
            feishu.save_user_id(open_id)
            feishu.notify("KMS 已绑定，发送 help 查看可用指令")
            bound_id = open_id

        if open_id != bound_id:
            return jsonify({"code": 0})

        try:
            text = json.loads(message.get("content", "{}")).get("text", "").strip()
        except Exception:
            text = ""

        if text:
            lower_text = text.strip().lower()
            # 只处理 "kms ..." 前缀或单独的 "kms"
            if lower_text == "kms" or lower_text.startswith("kms "):
                cmd = text.strip()[3:].strip()  # 去掉 "kms" 前缀
                threading.Thread(target=handle_command, args=(cmd or "help",), daemon=True).start()

    return jsonify({"code": 0})


if __name__ == "__main__":
    Path(CAMPAIGNS_DIR).mkdir(parents=True, exist_ok=True)
    print(f"[KMS Bot] 监听端口 {BOT_PORT}")
    app.run(host="0.0.0.0", port=BOT_PORT)
