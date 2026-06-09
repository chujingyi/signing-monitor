#!/usr/bin/env python3
"""
KMS monitor orchestrator.

用法：
  python3 orchestrator.py            # 扫描 + 自动启动/恢复监控
  python3 orchestrator.py --status   # 打印当前状态
  python3 orchestrator.py --report   # 发送每日报告
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import feishu
from config import BASE_DIR, CAMPAIGNS_DIR, ORCH_LOG
from kms_scan import mark_known, scan as run_scan
from kms_sort import sort_campaign

TZ = ZoneInfo("Asia/Shanghai")


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    ORCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ORCH_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt
    except Exception:
        return None


def _slugify(text: str) -> str:
    import re
    text = re.sub(r"[^\w一-鿿가-힯]+", "_", text).strip("_").lower()
    return text[:50] or "campaign"


def _legacy_deadline(raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if len(raw) == 14 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=TZ).isoformat()
        except ValueError:
            return raw
    return raw


def migrate_legacy_activities() -> None:
    """把旧后台 data/activities.json 里的 KMStation ID 接到 campaigns/。"""
    legacy_path = BASE_DIR / "data" / "activities.json"
    if not legacy_path.exists():
        return
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"旧活动读取失败: {e}")
        return
    if not isinstance(legacy, dict):
        return

    for activity in legacy.values():
        cfg = activity.get("platform_config") or {}
        prod_ids = [str(pid) for pid in (cfg.get("kmsapp_ids") or []) if str(pid).strip()]
        if not prod_ids:
            continue
        title = activity.get("title") or activity.get("id") or "legacy"
        campaign_id = f"legacy_{_slugify(title)}_{'_'.join(prod_ids)}"
        folder = CAMPAIGNS_DIR / campaign_id
        if (folder / "campaign.json").exists():
            continue
        migrated = {
            "campaign_id": campaign_id,
            "name": title,
            "prod_ids": prod_ids,
            "poll_interval_seconds": int(activity.get("interval_seconds") or 60),
            "stop_monitor_at": _legacy_deadline(activity.get("deadline", "")),
            "legacy_activity_id": activity.get("id", ""),
        }
        folder.mkdir(parents=True, exist_ok=True)
        save_campaign(folder, migrated)
        ensure_dirs(folder, migrated)
        if activity.get("status") == "stopped":
            write_pid(folder, 0)
        log(f"迁移旧活动到 campaigns: {campaign_id}")


# ── Campaign 目录管理 ─────────────────────────────────────────────────────────

def campaign_folders() -> list[Path]:
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(f for f in CAMPAIGNS_DIR.iterdir() if (f / "campaign.json").exists())


def load_campaign(folder: Path) -> dict:
    with (folder / "campaign.json").open(encoding="utf-8-sig") as f:
        return json.load(f)


def save_campaign(folder: Path, cfg: dict) -> None:
    (folder / "campaign.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ensure_dirs(folder: Path, cfg: dict) -> dict:
    if cfg.get("campaign_dir") != str(folder):
        cfg["campaign_dir"] = str(folder)
        save_campaign(folder, cfg)
    for sub in ("logs", "runtime", "exports"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    return cfg


# ── 进程管理 ──────────────────────────────────────────────────────────────────

def _pid_path(folder: Path) -> Path:
    return folder / "runtime" / "monitor.pid"


def read_pid(folder: Path) -> int:
    try:
        return int(_pid_path(folder).read_text().strip())
    except Exception:
        return 0


def write_pid(folder: Path, pid: int) -> None:
    (folder / "runtime").mkdir(exist_ok=True)
    _pid_path(folder).write_text(str(pid))


def is_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_expired(cfg: dict) -> bool:
    dt = _parse_dt(cfg.get("stop_monitor_at", ""))
    if dt is None:
        return False
    return datetime.now(TZ) >= dt


def remaining_text(cfg: dict) -> str:
    dt = _parse_dt(cfg.get("stop_monitor_at", ""))
    if dt is None:
        return "未设截止时间"
    now = datetime.now(TZ)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "已截止"
    d, s = delta.days, delta.seconds
    h, m = s // 3600, (s % 3600) // 60
    return f"{d}天{h}小时" if d else (f"{h}小时{m}分钟" if h else f"{m}分钟")


def start_monitor(folder: Path, force: bool = False) -> int:
    cfg = ensure_dirs(folder, load_campaign(folder))
    pid = read_pid(folder)
    if pid and is_running(pid):
        if not force:
            return pid
        os.kill(pid, 15)
        write_pid(folder, 0)
        for _ in range(10):
            time.sleep(0.3)
            if not is_running(pid):
                break
    runtime = folder / "runtime"
    with (runtime / "monitor.stdout.log").open("ab") as out, \
         (runtime / "monitor.stderr.log").open("ab") as err:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE_DIR / "kms_monitor.py"),
             "--config", str(folder / "campaign.json")],
            stdout=out, stderr=err, cwd=str(BASE_DIR),
        )
    write_pid(folder, proc.pid)
    log(f"启动监控: {folder.name} (PID {proc.pid})")
    return proc.pid


def stop_monitor(folder: Path) -> bool:
    pid = read_pid(folder)
    if not (pid and is_running(pid)):
        return False
    os.kill(pid, 15)
    write_pid(folder, 0)  # 清零，区别于崩溃
    log(f"停止监控: {folder.name} (PID {pid})")
    return True


# ── 扫描 & 新活动处理 ──────────────────────────────────────────────────────────

def save_new_campaign(cfg: dict) -> Path:
    folder = CAMPAIGNS_DIR / cfg["campaign_id"]
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "campaign.json").exists():
        save_campaign(folder, cfg)
    ensure_dirs(folder, load_campaign(folder))
    return folder


# ── 状态 ──────────────────────────────────────────────────────────────────────

def _stopped_over_12h(cfg: dict) -> bool:
    dt = _parse_dt(cfg.get("stop_monitor_at", ""))
    if dt is None:
        return False
    return (datetime.now(TZ) - dt).total_seconds() > 12 * 3600


def status_lines() -> list[str]:
    migrate_legacy_activities()
    lines = ["KMS 当前监控状态："]
    folders = campaign_folders()
    shown = 0
    for folder in folders:
        cfg = load_campaign(folder)
        pid = read_pid(folder)
        running = is_running(pid)
        if not running and _stopped_over_12h(cfg):
            continue
        shown += 1
        state = "🟢 运行中" if running else "🔴 已停止"
        lines.append(
            f"{state}  {cfg.get('campaign_id', folder.name)}\n"
            f"  名称: {cfg.get('name', folder.name)}\n"
            f"  截止: {cfg.get('stop_monitor_at') or '未设置'}\n"
            f"  剩余: {remaining_text(cfg)}"
        )
    if shown == 0:
        lines.append("暂无活跃活动")
    return lines


# ── 主流程 ────────────────────────────────────────────────────────────────────

def restart_active_monitors() -> None:
    for folder in campaign_folders():
        cfg = ensure_dirs(folder, load_campaign(folder))
        if is_expired(cfg):
            continue
        pid = read_pid(folder)
        if pid and is_running(pid):
            continue  # 正常运行
        if not pid:
            continue  # 手动停止（pid=0），不重启
        # pid 有值但进程不在 → 异常崩溃，重启
        name = cfg.get("name") or folder.name
        log(f"监控异常退出，重启: {folder.name} (上次 PID {pid})")
        feishu.notify(f"⚠️ KMS 监控异常退出，已自动重启\n活动: {name}")
        start_monitor(folder)


def stop_expired() -> None:
    for folder in campaign_folders():
        cfg = load_campaign(folder)
        if not is_expired(cfg):
            continue
        pid = read_pid(folder)
        if pid and is_running(pid):
            # 到期后的监控进程还要等待 FINAL_REPORT_DELAY 后发送最终报告，
            # 这里不主动杀进程，避免报告还没发出就被 cron 停掉。
            continue
        if pid and not is_running(pid):
            write_pid(folder, 0)


def daily_report() -> None:
    feishu.notify("\n".join(status_lines()))
    for folder in campaign_folders():
        cfg = load_campaign(folder)
        if is_running(read_pid(folder)):
            try:
                text, xlsx_path = sort_campaign(folder / "campaign.json")
                feishu.notify(text)
                if xlsx_path:
                    feishu.notify_file(xlsx_path)
            except Exception as e:
                log(f"报告生成失败 {folder.name}: {e}")
    log("每日报告已发送")


def main() -> None:
    log("=== orchestrator 启动 ===")
    migrate_legacy_activities()
    stop_expired()
    restart_active_monitors()

    new = run_scan()
    for cfg in new:
        folder = save_new_campaign(cfg)
        pid = start_monitor(folder)
        mark_known(cfg.get("prod_ids", []))
        stop_info = cfg.get("stop_monitor_at") or "未知，请用 kms set 补充"
        feishu.notify(
            f"🆕 KMS 发现新活动，监控已启动\n"
            f"名称: {cfg.get('name', '')}\n"
            f"prod_ids: {cfg.get('prod_ids', [])}\n"
            f"截止: {stop_info}\n"
            f"PID: {pid}"
        )
    log(f"=== 完成，新活动 {len(new)} 个 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status",  action="store_true")
    parser.add_argument("--report",  action="store_true")
    args = parser.parse_args()
    try:
        if args.status:
            migrate_legacy_activities()
            print("\n".join(status_lines()))
        elif args.report:
            migrate_legacy_activities()
            daily_report()
        else:
            main()
    except Exception:
        import traceback
        msg = traceback.format_exc()
        log(f"orchestrator 崩溃: {msg[-400:]}")
        try:
            feishu.notify(f"🚨 KMS orchestrator 崩溃\n\n{msg[-800:]}")
        except Exception:
            pass
        raise
