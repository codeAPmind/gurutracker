import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# FMP (Financial Modeling Prep) API — 可用于获取股票基本面、SEC数据等
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
FMP_STABLE_URL = "https://financialmodelingprep.com/stable"

# 飞书：信号卡片走 FEISHU_WEBHOOK_URL；下单/成交通知走交易机器人
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_WEBHOOK_SECRET = os.getenv("FEISHU_WEBHOOK_SECRET", "")
FEISHU_TRADE_WEBHOOK = os.getenv(
    "FEISHU_TRADE_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/6753df3d-6097-4fd4-8cbe-b3d817a8a15b",
)

# 数据库
DB_PATH = str(BASE_DIR / "data" / "signals.db")

# 日志
LOG_DIR = str(BASE_DIR / "logs")
LOG_FILE = str(BASE_DIR / "logs" / "guru_tracker.log")

# 调度频率（供参考，实际由 cron 控制）
SCHEDULE = {
    "sec_13f":      {"cron": "0 8 * * 1"},
    "sec_form4":    {"interval_hours": 6},
    "ark_trades":   {"cron": "0 7,20 * * 1-5"},
    "congress":     {"interval_hours": 12},
    "social_media": {"interval_minutes": 30},
}

# 置信度阈值：低于此值不推送
MIN_CONFIDENCE_TO_PUSH = "中"  # "低" / "中" / "高"

# QuiverQuant API（国会交易数据）
QUIVER_API_KEY = os.getenv("QUIVER_API_KEY", "")

# 雪球 Cookie
XUEQIU_COOKIE = os.getenv("XUEQIU_COOKIE", "")

# Futu OpenD（与 US_YiDong_AutoTrader 共用同一套 OpenD / 美股账户）
FUTU_HOST = os.getenv("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.getenv("FUTU_PORT", "11112"))
FUTU_TRADE_PWD_MD5 = os.getenv("FUTU_TRADE_PWD_MD5", "").strip()
FUTU_TRADE_PWD = os.getenv("FUTU_TRADE_PWD", "").strip()  # 实盘解锁用明文；MD5 优先
FUTU_ACCOUNT_US = os.getenv("FUTU_ACCOUNT_US", "").strip()
FUTU_ACCOUNT_US_SIM = os.getenv("FUTU_ACCOUNT_US_SIM", "").strip()

# Futu 模拟盘执行（executor/ 模块）
# 参数来自 2026-06~09 历史信号 sweep：回调>=30% + 仓位>0.10%，持仓10日
# 胜率67%，均值收益+9.2%，Sharpe 2.78，PF 2.25（n=30）
# 单笔1000；2 槽同时持仓，占用分析见 STRATEGY.md §5.7
GURU_TRADE_ENV = os.getenv("GURU_TRADE_ENV", "SIMULATE")  # SIMULATE / REAL
GURU_BUDGET_USD = float(os.getenv("GURU_BUDGET_USD", "1000"))
GURU_MAX_POSITIONS = int(os.getenv("GURU_MAX_POSITIONS", "2"))
GURU_MIN_SCORE = float(os.getenv("GURU_MIN_SCORE", "50"))
GURU_DRAWDOWN_PCT = float(os.getenv("GURU_DRAWDOWN_PCT", "30"))
GURU_MIN_POSITION_PCT = float(os.getenv("GURU_MIN_POSITION_PCT", "0.10"))
GURU_MAX_HOLD_DAYS = int(os.getenv("GURU_MAX_HOLD_DAYS", "20"))   # D1下仅作兜底
GURU_BOLL_PERIOD = int(os.getenv("GURU_BOLL_PERIOD", "20"))       # 布林中轨周期
GURU_TAKE_PROFIT_PCT = float(os.getenv("GURU_TAKE_PROFIT_PCT", "0"))  # 0=禁用止盈
GURU_STOP_LOSS_PCT = float(os.getenv("GURU_STOP_LOSS_PCT", "-0.15"))
