"""KMStation SKU 库存监控器。

对指定 campaign 的所有 prod_id，轮询 prodInfo 接口，
发现库存变化时写日志。启动时从 stock.log 恢复上次状态，重启不丢数据。

stock.log 只有两种行格式：
  [ts] prodId=X skuId=Y name 库存=N          — 记录某时刻的库存
  [ts] prodId=X skuId=Y name OLD → NEW (Δ±n) — 记录库存变化
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import feishu
from config import HTTPS_PROXY, KMS_HEADERS

TZ = ZoneInfo("Asia/Shanghai")
HEARTBEAT_INTERVAL = 5 * 60   # 每5分钟写一条心跳日志
FINAL_REPORT_DELAY = 30 * 60  # 活动结束后等30分钟发报告

# 变化行：[ts] prodId=X skuId=Y name OLD → NEW (Δ±n)
_CHANGE_RE = re.compile(
    r"prodId=(\S+) skuId=(\S+) .+? (\d+) → (\d+) \(Δ[+-]\d+\)"
)
# 库存行：[ts] prodId=X skuId=Y name 库存=N（含旧格式的 [初始]/[启动]/[STARTUP] 标记）
_STOCK_RE = re.compile(
    r"prodId=(\S+) skuId=(\S+) .+ 库存=(\d+)"
)


def _proxies() -> dict | None:
    return {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None


def _session() -> requests.Session:
    s = requests.Session()
    px = _proxies()
    if px:
        s.proxies = px
    return s


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        cfg = json.load(f)
    for key in ("campaign_id", "prod_ids", "poll_interval_seconds"):
        if key not in cfg:
            raise ValueError(f"campaign.json 缺少字段: {key}")
    return cfg


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


def _dynamic_interval(stop_at: datetime | None, default: int) -> int:
    if stop_at is None:
        return default
    now = datetime.now(TZ)
    if now >= stop_at:
        return 10
    secs = (stop_at - now).total_seconds()
    if secs <= 20 * 60:  return 2
    if secs <= 60 * 60:  return 5
    if secs <= 2 * 3600: return 10
    return default


def _fetch_skus(session: requests.Session, prod_id: str) -> list[dict]:
    """拉取 prodInfo，返回 skuList，失败返回空列表。"""
    try:
        r = session.get(
            "https://kms.kmstation.net/prod/prodInfo",
            params={"prodId": prod_id},
            headers=KMS_HEADERS,
            timeout=10,
        )
        data = r.json()
        return data.get("skuList") or []
    except Exception as e:
        print(f"[monitor] prodId={prod_id} 拉取失败: {e}")
        return []


def _sku_display_name(sku: dict) -> str:
    return str(
        sku.get("skuName")
        or sku.get("properties")
        or sku.get("propertiesName")
        or sku.get("skuNameCn")
        or sku.get("skuId")
        or ""
    ).strip()


def _load_last_stocks(stock_log: Path) -> dict[str, dict[str, int]]:
    """从 stock.log 读出各 SKU 最后一次记录的库存，作为重启后的基线。"""
    last: dict[str, dict[str, int]] = {}
    if not stock_log.exists():
        return last
    for line in stock_log.read_text(encoding="utf-8").splitlines():
        # 变化行：取变化后的新值
        m = _CHANGE_RE.search(line)
        if m:
            prod_id, sku_id, _, new = m.groups()
            last.setdefault(prod_id, {})[sku_id] = int(new)
            continue
        # 库存行：取记录的库存值（兼容旧格式中含 [初始]/[启动]/[STARTUP] 的行）
        m = _STOCK_RE.search(line)
        if m:
            prod_id, sku_id, stock = m.groups()
            last.setdefault(prod_id, {})[sku_id] = int(stock)
    return last


def monitor_loop(cfg: dict, run_once: bool = False) -> bool:
    """返回 True 表示自然到期，False 表示 --once 单次退出。"""
    session = _session()
    campaign_id = cfg["campaign_id"]
    prod_ids: list[str] = [str(p) for p in cfg["prod_ids"]]
    default_interval = cfg["poll_interval_seconds"]
    stop_at = _parse_dt(cfg.get("stop_monitor_at", ""))

    campaign_dir = Path(cfg.get("campaign_dir", "."))
    logs_dir    = campaign_dir / "logs"
    runtime_dir = campaign_dir / "runtime"
    logs_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    heartbeat_path = runtime_dir / "heartbeat.json"
    heartbeat_log  = logs_dir / "heartbeat.log"
    stock_log      = logs_dir / "stock.log"

    # 从 log 恢复上次状态
    last_stocks = _load_last_stocks(stock_log)
    last_hb_log = datetime.now(TZ)

    print(f"[monitor] 启动 {campaign_id}，prod_ids={prod_ids}，stop_at={stop_at}")

    # ── 启动快照：记录当前库存，断点期间有变化则写变化行 ─────────────
    now = datetime.now(TZ)
    ts  = now.strftime("%Y-%m-%d %H:%M:%S")
    for prod_id in prod_ids:
        skus = []
        for _attempt in range(3):
            skus = _fetch_skus(session, prod_id)
            if skus:
                break
            if _attempt < 2:
                time.sleep(5)
        if not skus:
            print(f"[monitor] prodId={prod_id} 启动快照连续失败，跳过")
            continue
        prev = last_stocks.setdefault(prod_id, {})
        with stock_log.open("a", encoding="utf-8") as f:
            for sku in skus:
                sku_id   = str(sku["skuId"])
                stock    = int(sku.get("stocks", 0))
                sku_name = _sku_display_name(sku) or sku_id
                old = prev.get(sku_id)
                prev[sku_id] = stock
                if old is not None and stock != old:
                    # 断点期间有变化，补录为变化行
                    diff = stock - old
                    line = (
                        f"[{ts}] prodId={prod_id} skuId={sku_id} "
                        f"{sku_name} {old} → {stock} (Δ{diff:+d})"
                    )
                else:
                    # 首次或无变化，记录当前库存
                    line = (
                        f"[{ts}] prodId={prod_id} skuId={sku_id} "
                        f"{sku_name} 库存={stock}"
                    )
                print(line)
                f.write(line + "\n")

    while True:
        now = datetime.now(TZ)
        if stop_at and now >= stop_at:
            print(f"[monitor] {campaign_id} 到期，退出")
            heartbeat_path.write_text(
                json.dumps({"campaign_id": campaign_id, "updated_at": now.isoformat()})
            )
            return True

        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        for prod_id in prod_ids:
            skus = _fetch_skus(session, prod_id)
            if not skus:
                continue

            prev = last_stocks.setdefault(prod_id, {})
            for sku in skus:
                sku_id   = str(sku["skuId"])
                stock    = int(sku.get("stocks", 0))
                sku_name = _sku_display_name(sku) or sku_id

                old = prev.get(sku_id)
                if old is None:
                    # 轮询中途出现的新 SKU，静默记录
                    prev[sku_id] = stock
                    continue

                if stock == old:
                    continue

                # 库存有变化
                diff = stock - old
                prev[sku_id] = stock
                line = (
                    f"[{ts}] prodId={prod_id} skuId={sku_id} "
                    f"{sku_name} {old} → {stock} (Δ{diff:+d})"
                )
                print(line)
                with stock_log.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")

        # 心跳
        heartbeat_path.write_text(
            json.dumps({"campaign_id": campaign_id, "updated_at": now.isoformat()})
        )
        if (now - last_hb_log).total_seconds() >= HEARTBEAT_INTERVAL:
            hb = f"[{ts}] 💓 心跳 {campaign_id}"
            with heartbeat_log.open("a", encoding="utf-8") as f:
                f.write(hb + "\n")
            print(hb)
            last_hb_log = now

        if run_once:
            return False

        interval = _dynamic_interval(stop_at, default_interval)
        if stop_at:
            remaining = (stop_at - datetime.now(TZ)).total_seconds()
            interval = max(1, min(interval, remaining))
        time.sleep(interval)


def send_final_report(cfg: dict, config_path: Path) -> None:
    name = cfg.get("name") or cfg["campaign_id"]
    print(f"[monitor] {name} 活动结束，等待 {FINAL_REPORT_DELAY // 60} 分钟后发报告")
    time.sleep(FINAL_REPORT_DELAY)
    from kms_sort import sort_campaign
    try:
        text, xlsx_path = sort_campaign(config_path)
        feishu.notify(f"📊 KMS 活动结束报告\n{name}\n\n{text}")
        if xlsx_path:
            feishu.notify_file(xlsx_path)
    except Exception as e:
        feishu.notify(f"⚠️ KMS {name} 已结束，生成报告时出错：{e}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = load_config(config_path)
    cfg.setdefault("campaign_dir", str(config_path.parent))

    try:
        expired = monitor_loop(cfg, run_once=args.once)
    except Exception:
        import traceback
        name = cfg.get("name") or cfg.get("campaign_id", "unknown")
        feishu.notify(
            f"🚨 KMS 监控崩溃\n活动: {name}\n\n{traceback.format_exc()[-600:]}"
        )
        raise

    if expired:
        send_final_report(cfg, config_path)


if __name__ == "__main__":
    main()
