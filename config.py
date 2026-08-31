from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "cb_history.db"

TPEX_BASE_URL = "https://www.tpex.org.tw"
TPEX_REPORT_INDEX_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/cbDaily"
TPEX_REPORT_CODE = "rsta0113"
TPEX_SOURCE = "TPEx:RSta0113"
HTTP_TIMEOUT_SECONDS = 30
LOOKBACK_DAYS = 14

TWSE_DAILY_MARKET_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
TPEX_DAILY_MARKET_URL = f"{TPEX_BASE_URL}/www/zh-tw/afterTrading/otc"

TPEX_CB_ISSUE_URL = f"{TPEX_BASE_URL}/openapi/v1/bond_ISSBD5_data"
TPEX_CB_LISTED_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/convSearch"
TPEX_CB_DELISTED_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/convDelist"
MOPS_BASE_URL = "https://mopsov.twse.com.tw"
MOPS_CB_ANNOUNCEMENT_URL = f"{MOPS_BASE_URL}/mops/web/ajax_t108sb08_1"
TDCC_BOOK_ENTRY_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-16"
