"""KMStation 活动扫描器。

通过 /index/choicenessList 接口一次拉取全部上架商品，
筛选含签售/应募关键词的商品，返回新发现的 campaign dict 列表。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from config import BASE_DIR, CAMPAIGNS_DIR, HTTPS_PROXY, KMS_HEADERS

KEYWORDS = ["签售", "合影", "应募", "见面会", "线上签", "线下签", "fansign", "hi-touch"]
# 含以下关键词视为周边/普通商品，即使命中 KEYWORDS 也排除
EXCLUDE_KEYWORDS = ["官方周边", "OFFICIAL MD", "official md", "周边", "演唱会"]
KNOWN_IDS_FILE = BASE_DIR / "known_prod_ids.json"
PAGE_SIZE = 50
TIMEOUT = 10


def _proxies() -> dict | None:
    return {"http": HTTPS_PROXY, "https": HTTPS_PROXY} if HTTPS_PROXY else None


def _session() -> requests.Session:
    s = requests.Session()
    px = _proxies()
    if px:
        s.proxies = px
    return s


def _load_known() -> set[str]:
    if not KNOWN_IDS_FILE.exists():
        return set()
    try:
        return set(json.loads(KNOWN_IDS_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_known(ids: set[str]) -> None:
    KNOWN_IDS_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def mark_known(prod_ids: list[str] | set[str] | tuple[str, ...]) -> None:
    """把已成功保存/启动的 prodId 标记为已处理。"""
    known = _load_known()
    known.update(str(pid) for pid in prod_ids if str(pid).strip())
    _save_known(known)


def _fetch_all_products(session: requests.Session) -> list[dict]:
    products = []
    page = 1
    while True:
        try:
            r = session.get(
                "https://kms.kmstation.net/index/choicenessList",
                params={"current": page, "size": PAGE_SIZE},
                headers=KMS_HEADERS,
                timeout=TIMEOUT,
            )
            records = r.json().get("records") or []
            if not records:
                break
            products.extend(records)
            if len(records) < PAGE_SIZE:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"[kms_scan] 第{page}页失败: {e}")
            break
    return products


def _fetch_detail(session: requests.Session, prod_id: str) -> dict | None:
    try:
        r = session.get(
            "https://kms.kmstation.net/prod/prodInfo",
            params={"prodId": prod_id},
            headers=KMS_HEADERS,
            timeout=TIMEOUT,
        )
        d = r.json()
        return d if d.get("prodName") else None
    except Exception:
        return None


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w一-鿿가-힯]+", "_", text).strip("_").lower()
    return text[:50] or "campaign"


def _sku_display_name(sku: dict) -> str:
    return str(
        sku.get("skuName")
        or sku.get("properties")
        or sku.get("propertiesName")
        or sku.get("skuNameCn")
        or sku.get("skuId")
        or ""
    ).strip()


def _build_campaign(prod_id: str, detail: dict) -> dict:
    name = detail.get("prodName", "")
    publish = detail.get("publishTime", "")
    date_tag = datetime.now().strftime("%Y%m%d")
    campaign_id = f"{date_tag}_{_slugify(name)}_{prod_id}"

    skus_info = [
        {
            "prod_id":       prod_id,
            "sku_id":        str(s["skuId"]),
            "sku_name":      _sku_display_name(s),
            "initial_stock": s.get("stocks", 0),
        }
        for s in (detail.get("skuList") or [])
    ]

    return {
        "campaign_id": campaign_id,
        "name": name,
        "prod_ids": [prod_id],
        "skus": skus_info,
        "poll_interval_seconds": 60,
        "stop_monitor_at": "",
        "publish_time": publish,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }


def scan(include_known: bool = False) -> list[dict]:
    """
    拉取 KMStation 全部商品，找出新的签售/应募活动。
    每个 prodId 独立为一个 campaign，返回新发现的列表。
    """
    known = _load_known()
    session = _session()

    print("[kms_scan] 拉取商品列表...")
    products = _fetch_all_products(session)
    print(f"[kms_scan] 共 {len(products)} 个商品")

    def _is_fansign(name: str) -> bool:
        return (
            any(kw in name for kw in KEYWORDS)
            and not any(ex in name for ex in EXCLUDE_KEYWORDS)
        )

    fansign = [p for p in products if _is_fansign(p.get("prodName") or "")]
    print(f"[kms_scan] 命中关键词: {len(fansign)} 个")

    new_campaigns = []
    for p in fansign:
        prod_id = str(p["prodId"])
        if prod_id in known and not include_known:
            continue

        detail = _fetch_detail(session, prod_id)
        time.sleep(0.2)
        if not detail:
            continue

        campaign = _build_campaign(prod_id, detail)
        new_campaigns.append(campaign)

    print(f"[kms_scan] 新发现 {len(new_campaigns)} 个活动")
    return new_campaigns


def init_mode() -> None:
    """把当前所有应募活动标记为已知，避免首次部署时重复处理旧活动。"""
    found = scan(include_known=True)
    known = {pid for c in found for pid in c["prod_ids"]}
    _save_known(known)
    print(f"[kms_scan] 已初始化，标记 {len(known)} 个 prodId 为已知")


if __name__ == "__main__":
    import argparse as _ap
    p = _ap.ArgumentParser()
    p.add_argument("--init", action="store_true", help="把当前活动标记为已知")
    p.add_argument("--all",  action="store_true", help="包含已知活动一并输出")
    args = p.parse_args()
    if args.init:
        init_mode()
    else:
        found = scan(include_known=args.all)
        if found and not args.all:
            mark_known({pid for c in found for pid in c["prod_ids"]})
        print(json.dumps(found, ensure_ascii=False, indent=2))
