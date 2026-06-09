import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CAMPAIGNS_DIR = BASE_DIR / "campaigns"
ORCH_LOG = BASE_DIR / "orchestrator.log"
FEISHU_STATE = BASE_DIR / "feishu_state.json"

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.getenv("FEISHU_VERIFY_TOKEN", "")

# KMS 独立 bot 备用端口。主 README 里 KMS 由 imini 统一接管，
# 但 webhook_relay.py 仍会把 kms 指令转发到 8082。
BOT_PORT = int(os.getenv("BOT_PORT", "8082"))

# KMStation 请求配置
HTTPS_PROXY = (
    os.getenv("HTTPS_PROXY")
    or os.getenv("https_proxy")
    or os.getenv("HTTP_PROXY")
    or os.getenv("http_proxy")
    or ""
)
KMS_HEADERS = {
    "accept": "*/*",
    "locale": "zh_KO",
    "origin": "http://page.kmstation.net",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Mobile Safari/537.36"
    ),
}
