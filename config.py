from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "cb_history.db"

TPEX_BASE_URL = "https://www.tpex.org.tw"
TPEX_REPORT_INDEX_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/cbDaily"
TPEX_REPORT_CODE = "rsta0113"
TPEX_SOURCE = "TPEx:RSta0113"
HTTP_TIMEOUT_SECONDS = 30
HTTP_CONNECT_TIMEOUT_SECONDS = 10
HTTP_REQUEST_TIMEOUT = (HTTP_CONNECT_TIMEOUT_SECONDS, HTTP_TIMEOUT_SECONDS)
LOOKBACK_DAYS = 14

TWSE_DAILY_MARKET_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
# The legacy /exchangeReport routes can intermittently self-redirect through
# the CDN.  These are the equivalent first-party routes declared by the TWSE
# historical-report pages and retain the same report codes/payload schema.
TWSE_INTRADAY_ODD_LOT_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/TWTC7U"
TWSE_POST_ODD_LOT_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/TWT53U"
TWSE_FIXED_PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BFT41U"
TPEX_DAILY_MARKET_URL = f"{TPEX_BASE_URL}/www/zh-tw/afterTrading/otc"
TPEX_DAILY_QUOTES_URL = f"{TPEX_BASE_URL}/www/zh-tw/afterTrading/dailyQuotes"
TPEX_INTRADAY_ODD_LOT_URL = f"{TPEX_BASE_URL}/www/zh-tw/afterTrading/oddQuote"
TPEX_POST_ODD_LOT_URL = f"{TPEX_BASE_URL}/www/zh-tw/afterTrading/odd"
TPEX_FIXED_PRICE_URL = f"{TPEX_BASE_URL}/www/zh-tw/afterTrading/fixPricing"
TPEX_BLOCK_TRADE_URL = f"{TPEX_BASE_URL}/www/zh-tw/blockTrade/quote"

TPEX_CB_ISSUE_URL = f"{TPEX_BASE_URL}/openapi/v1/bond_ISSBD5_data"
TPEX_CB_LISTED_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/convSearch"
TPEX_CB_DELISTED_URL = f"{TPEX_BASE_URL}/www/zh-tw/bond/convDelist"
MOPS_BASE_URL = "https://mopsov.twse.com.tw"
MOPS_CB_ANNOUNCEMENT_URL = f"{MOPS_BASE_URL}/mops/web/ajax_t108sb08_1"
TDCC_BOOK_ENTRY_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-16"
