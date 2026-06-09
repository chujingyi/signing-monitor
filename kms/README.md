# KMStation 应募活动监控

自动扫描 [KMStation](http://page.kmstation.net) 全部上架商品，发现签售/合影/应募活动后自动启动库存监控，变化记录写入日志，到期后自动发送排序报告。

---

## 架构说明

KMS 和 imini、K4 共用同一个飞书机器人（运行在 `imini/bot_server.py`），**KMS 的 `bot_server.py` 不需要单独启动**。所有飞书指令通过 `kms <指令>` 前缀发送。

```
cron（每小时）
    ↓
orchestrator.py
    ├── kms_scan.py     拉取 choicenessList，筛选应募活动
    ├── kms_monitor.py  单活动库存监控（子进程，按活动独立运行）
    └── feishu.py       推送通知

飞书消息
    ↓
imini/bot_server.py（:8080，systemd 管理）
    ├── imini xxx → imini 逻辑
    ├── k4 xxx    → k4 逻辑
    └── kms xxx   → kms 逻辑
```

---

## 文件说明

| 文件 | 用途 |
|------|------|
| `orchestrator.py` | 调度器：扫描新活动、启动/恢复监控、停止过期监控、发送日报 |
| `kms_scan.py` | 扫描 KMStation 全部商品，筛选应募活动并返回新 campaign 列表 |
| `kms_monitor.py` | 单活动库存监控进程，轮询 prodInfo，发现变化推飞书并写日志 |
| `kms_sort.py` | 日志排序：统计各 SKU 累计应募量，生成排序报告 |
| `bot_server.py` | **已弃用**，由 `imini/bot_server.py` 统一替代，保留备用 |
| `feishu.py` | 飞书 API 客户端（监控进程推送用） |
| `config.py` | 路径、飞书凭证、KMStation 请求配置 |
| `requirements.txt` | Python 依赖（flask + requests） |
| `campaign.sample.json` | 活动配置样例 |
| `known_prod_ids.json` | 已处理过的 prodId 列表，自动维护 |
| `orchestrator.log` | 调度器日志 |
| `campaigns/` | 每个 prodId 一个子目录，内含 campaign.json、日志、报告 |

---

## 目录结构

```text
campaigns/<campaign_id>/
  campaign.json           # 活动配置
  logs/
    stock.log             # 库存变化日志
    heartbeat.log         # 心跳日志（每5分钟写一条）
  runtime/
    monitor.pid           # 监控进程 PID
    monitor.stdout.log    # 进程标准输出
    monitor.stderr.log    # 进程标准错误
    heartbeat.json        # 最后一次心跳时间
  exports/
    20260527_rank.log     # 排序报告
```

---

## 活动筛选规则

choicenessList 返回全部上架商品，只保留名称中含以下关键词的：

| 关键词 | 对应活动类型 |
|--------|------------|
| `签售` | 线下/线上签售 |
| `合影` | 拍照合影 |
| `应募` | 应募活动 |
| `见面会` | 粉丝见面会 |
| `线上签` / `线下签` | 签售会 |
| `fansign` / `hi-touch` | 英文活动名 |

每个 prodId 独立为一个 campaign，个人签售、团体签售、合影分别单独监控，在 status 中分条显示。

---

## campaign.json 格式

```json
{
  "campaign_id": "20260527_个人签售_nct_wish_hk3_4013",
  "name": "【个人签售】NCT WISH HK场3 线下签名会",
  "prod_ids": ["4013"],
  "poll_interval_seconds": 60,
  "stop_monitor_at": "2026-05-29T23:59:00+08:00"
}
```

| 字段 | 说明 |
|------|------|
| `prod_ids` | 单个 KMStation prodId（每个 campaign 对应一个 prodId） |
| `poll_interval_seconds` | 默认轮询间隔（秒），临近截止时自动加速 |
| `stop_monitor_at` | 监控停止时间，支持 ISO 8601；为空时监控持续运行直到手动停止 |

---

## 请求间隔规则

监控进程根据距截止时间自动调整轮询频率：

| 距截止时间 | 轮询间隔 |
|-----------|---------|
| 超过 2 小时 | 配置值（默认 60s） |
| 1 ~ 2 小时 | 10s |
| 20 ~ 60 分钟 | 5s |
| ≤ 20 分钟 | 2s |
| 已截止（退单监控期） | 10s |

---

## 截止时间处理

- **扫描发现新活动**：无论有没有截止时间都自动启动监控并飞书推送。
- **截止时间未知**：监控持续运行，推送提醒用 `kms set` 补充截止时间。
- **手动补时间**：飞书发送 `kms set <campaign_id> YYYY-MM-DD HH:MM`，更新截止时间。

---

## 监控恢复与手动停止

- **异常崩溃**：进程意外退出（pid 有值但进程不存在），下次 cron 自动重启并飞书告警。
- **手动停止**（`kms stop`）：pid 清零，cron 不会重启，需手动 `kms start` 恢复。

---

## status 显示规则

- 运行中：始终显示
- 已停止 < 12 小时：显示（方便确认刚结束的活动）
- 已停止 > 12 小时：自动隐藏（数据和日志文件永久保留）

---

## 自动推送时机

| 时机 | 内容 |
|------|------|
| 发现新活动 | 活动名、prod_ids、截止时间（或提示补时间） |
| 库存发生变化 | SKU 名称、变化前后库存、差值 |
| 监控进程崩溃恢复 | 告警 + 已重启 |
| 活动到期后 30 分钟 | 最终排序报告（仅自然到期触发，手动 stop 不触发） |
| 每天 22:00 | 所有运行中活动的状态 + 排序报告 |

---

## 飞书指令

所有 KMS 指令需加 `kms` 前缀。`campaign_id` 支持模糊匹配。

| 指令 | 说明 |
|------|------|
| `kms status` | 查看 KMS 监控状态 |
| `kms scan` | 立即扫描 KMStation，发现新活动 |
| `kms start <campaign_id>` | 启动或恢复监控 |
| `kms stop <campaign_id>` | 停止监控（手动停止，不会被 cron 重启） |
| `kms set <campaign_id> YYYY-MM-DD HH:MM` | 设置截止时间 |
| `kms sort <campaign_id>` | 生成排序报告 |
| `kms log1 <campaign_id>` | 获取库存监控日志（stock.log） |
| `kms log2 <campaign_id>` | 获取排序导出文件 |
| `kms log orchestrator` | 获取调度器日志 |
| `status` | 同时查看所有平台状态 |
| `help` | 查看全部可用指令 |

示例：

```text
kms set 20260527_个人签售_nct_wish_hk3_4013 2026-05-29 23:59
kms sort 4013
kms log1 4013
kms log orchestrator
```

---

## 使用方法

### 1. 安装依赖

```bash
pip3 install -r requirements.txt
```

### 2. 首次初始化

把当前 KMStation 上已有应募活动标记为已知，避免重复处理：

```bash
python3 kms_scan.py --init
```

### 3. 手动运行

```bash
python3 orchestrator.py           # 扫描 + 启动/恢复监控
python3 orchestrator.py --status  # 打印状态
python3 orchestrator.py --report  # 发送日报
python3 kms_sort.py --config campaigns/<campaign_id>/campaign.json
```

---

## 部署

### 环境变量

写入 `/etc/kms.env`：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFY_TOKEN=xxx
# 如需走代理访问 KMStation（大陆服务器必须）：
HTTPS_PROXY=http://127.0.0.1:7897
```

### feishu_state.json

KMS 监控进程推送用，从 imini 复制：

```bash
cp /root/imini/feishu_state.json /root/kms/feishu_state.json
```

### 不需要启动 KMS 的 bot server

KMS 的飞书指令由 imini 的 `imini-bot.service` 统一处理，无需单独启动 `bot_server.py`。

### Cron

```cron
# 每天 09:00-19:00 每小时扫描一次
0 9-19 * * * cd /root/kms && set -a && . /etc/kms.env && set +a && /usr/bin/python3 orchestrator.py 2>>/root/kms/orchestrator.log

# 每天 22:00 发送晚报
0 22 * * * cd /root/kms && set -a && . /etc/kms.env && set +a && /usr/bin/python3 orchestrator.py --report 2>>/root/kms/orchestrator.log
```

---

## 日志

```bash
tail -f orchestrator.log
tail -f campaigns/<campaign_id>/logs/stock.log
tail -f campaigns/<campaign_id>/logs/heartbeat.log
tail -f campaigns/<campaign_id>/runtime/monitor.stdout.log
```

---

## 数据来源

| 接口 | 用途 |
|------|------|
| `GET https://kms.kmstation.net/index/choicenessList?current=1&size=50` | 全量商品列表（分页） |
| `GET https://kms.kmstation.net/prod/prodInfo?prodId=<id>` | 单商品详情 + SKU 库存 |
