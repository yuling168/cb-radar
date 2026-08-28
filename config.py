from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "cb_history.db"

TPEX_BASE_URL = "https://www.tpex.org.tw"
TPEX_REPORT_INDEX_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/cbDaily"
TPEX_REPORT_CODE = "rsta0113"
TPEX_SOURCE = "TPEx:RSta0113"
HTTP_TIMEOUT_SECONDS = 30
LOOKBACK_DAYS = 14

