"""
Futu 美股下单封装（Guru Tracker 专用）

移植自 US_YiDong_AutoTrader/futu_executor.py 的关键 bug 修复：
- 2026-08-21: cancel_order 必须用 ModifyOrderOp.CANCEL 枚举，不能传裸 int
- 2026-09-02: 撤单后 can_sell_qty 有异步延迟，需 wait_for_sellable 轮询
- 2026-09-03: FILLED_ALL 回报后立即移除 callback 会丢失紧随的成交回报，
              改为 15s 宽限期退休
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 美东时区偏移（非夏令时 -5，夏令时 -4；简化用 pytz 或 zoneinfo）
def _us_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        from datetime import timezone
        return timezone(timedelta(hours=-5))


def _now_us() -> datetime:
    return datetime.now(_us_tz())


def round_to_us_tick(price: float) -> float:
    if price <= 0:
        return price
    if price >= 1.0:
        return round(round(price / 0.01) * 0.01, 4)
    return round(round(price / 0.0001) * 0.0001, 6)


# ── 成交回报全局状态 ──────────────────────────────────────────────
_CB_RETIRE_GRACE_SEC = 15.0     # FILLED_ALL 后保留 callback 的宽限期

_fill_callbacks: dict[str, Callable] = {}
# 2026-09-03 fix: 不立即删，而是记退休时刻，_retire_loop 延迟清理
_cb_retire_at: dict[str, float] = {}
_delivered_qty: dict[str, int] = {}

_late_fills: dict[str, dict] = {}  # order_id → {qty, price, first_seen}
_late_fills_lock = threading.Lock()

_cb_lock = threading.Lock()


def _register_fill_callback(order_id: str, cb: Callable) -> None:
    with _cb_lock:
        _fill_callbacks[order_id] = cb
        _cb_retire_at.pop(order_id, None)
        _delivered_qty.pop(order_id, None)
    with _late_fills_lock:
        info = _late_fills.pop(order_id, None)
    if info:
        # 有积压的 late fill，立即消费
        try:
            cb(info["qty"], info["price"])
        except Exception as e:
            logger.error("[futu] late-fill immediate cb error order=%s: %s", order_id, e)


def _dispatch_fill(order_id: str, cumulative_qty: int, avg_price: float, is_final: bool) -> None:
    with _cb_lock:
        cb = _fill_callbacks.get(order_id)
        if cb is None:
            # callback 已退休或尚未注册 → 放入 late_fills
            with _late_fills_lock:
                if order_id not in _late_fills:
                    _late_fills[order_id] = {
                        "qty": cumulative_qty,
                        "price": avg_price,
                        "first_seen": time.monotonic(),
                    }
                else:
                    _late_fills[order_id]["qty"] = cumulative_qty
                    _late_fills[order_id]["price"] = avg_price
            return

        # 防重复交付
        if cumulative_qty > 0 and _delivered_qty.get(order_id, 0) >= cumulative_qty:
            return
        _delivered_qty[order_id] = max(_delivered_qty.get(order_id, 0), cumulative_qty)

    try:
        cb(cumulative_qty, avg_price)
    except Exception as e:
        logger.error("[futu] fill cb error order=%s: %s", order_id, e)

    if is_final:
        with _cb_lock:
            had_cb = order_id in _fill_callbacks
            if had_cb and order_id not in _cb_retire_at:
                _cb_retire_at[order_id] = time.monotonic() + _CB_RETIRE_GRACE_SEC


def _late_fill_retry_loop() -> None:
    """后台线程：重试 late_fills + 清理已退休 callbacks"""
    while True:
        time.sleep(1)
        try:
            with _late_fills_lock:
                pending = list(_late_fills.items())

            for oid, info in pending:
                with _cb_lock:
                    cb = _fill_callbacks.get(oid)
                if cb:
                    with _late_fills_lock:
                        if _late_fills.pop(oid, None) is None:
                            continue
                    try:
                        cb(info["qty"], info["price"])
                    except Exception as e:
                        logger.error("[futu] late-fill retry cb error order=%s: %s", oid, e)

            with _late_fills_lock:
                expired = [oid for oid, info in _late_fills.items()
                           if time.monotonic() - info["first_seen"] > 10]
                for oid in expired:
                    _late_fills.pop(oid, None)
                    logger.warning("[futu] late-fill expired (no cb registered) order=%s", oid)

            now_mono = time.monotonic()
            with _cb_lock:
                retired = [oid for oid, dl in _cb_retire_at.items() if now_mono >= dl]
                for oid in retired:
                    _cb_retire_at.pop(oid, None)
                    _fill_callbacks.pop(oid, None)
                    _delivered_qty.pop(oid, None)
        except Exception as e:
            logger.error("[futu] _late_fill_retry_loop error: %s", e)


threading.Thread(target=_late_fill_retry_loop, daemon=True, name="guru_fill_retry").start()


# ── StopOrderMonitor ──────────────────────────────────────────────
_STOP_TIMEOUT_SEC = 30
_STOP_GAP_PCT = 0.003   # 0.3% 价格偏离升级为市价


class StopOrderMonitor:
    """限价止损单超时/价差自动升级为市价"""

    def __init__(self):
        self._stops: dict[str, dict] = {}  # order_id → info
        self._lock = threading.Lock()
        self._trader_ref: Optional["GuruFutuTrader"] = None
        threading.Thread(target=self._monitor_loop, daemon=True, name="guru_stop_monitor").start()

    def register(self, order_id: str, code: str, qty: int, ref_price: float,
                 trader: "GuruFutuTrader") -> None:
        self._trader_ref = trader
        with self._lock:
            self._stops[order_id] = {
                "code": code,
                "qty": qty,
                "ref_price": ref_price,
                "placed_at": time.monotonic(),
            }

    def remove(self, order_id: str) -> None:
        with self._lock:
            self._stops.pop(order_id, None)

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(5)
            try:
                if not self._trader_ref:
                    continue
                now = time.monotonic()
                with self._lock:
                    items = list(self._stops.items())

                for order_id, info in items:
                    age = now - info["placed_at"]
                    if age < _STOP_TIMEOUT_SEC:
                        continue
                    # 超时 → 升级市价
                    logger.warning("[stop] order=%s timeout %.0fs, escalating to market", order_id, age)
                    self._escalate(order_id, info)
            except Exception as e:
                logger.error("[stop] monitor loop error: %s", e)

    def _escalate(self, order_id: str, info: dict) -> None:
        trader = self._trader_ref
        if not trader:
            return
        with self._lock:
            if order_id not in self._stops:
                return
        cancelled = trader.cancel_order(order_id)
        if not cancelled:
            logger.error("[stop] escalate: cancel failed order=%s", order_id)
            return
        trader.wait_for_sellable(info["code"], min_qty=info["qty"])
        new_order_id = trader.place_market_sell(info["code"], info["qty"], purpose="stop_escalate")
        with self._lock:
            self._stops.pop(order_id, None)
        if new_order_id:
            logger.info("[stop] escalated order=%s → market=%s", order_id, new_order_id)


stop_order_monitor = StopOrderMonitor()


# ── GuruFutuTrader ────────────────────────────────────────────────
_ENTRY_SLIPPAGE = 0.005    # 买入+0.5%
_SELL_SLIPPAGE = -0.005    # 卖出-0.5%


class GuruFutuTrader:
    """Guru Tracker 专用 Futu 美股下单封装"""

    def __init__(self):
        self.ctx = None
        self.acc_id: Optional[int] = None
        self._connected = False

    def connect(self) -> None:
        from futu import TrdEnv, TrdMarket, OpenSecTradeContext

        host = os.getenv("FUTU_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_PORT", "11112"))
        pwd_md5 = os.getenv("FUTU_TRADE_PWD_MD5", "")
        env_str = os.getenv("GURU_TRADE_ENV", "SIMULATE")
        self.trd_env = TrdEnv.REAL if env_str.upper() == "REAL" else TrdEnv.SIMULATE

        logger.info("[futu][API] OpenSecTradeContext(host=%s, port=%d, market=US)", host, port)
        self.ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=host,
            port=port,
            is_encrypt=False,
        )
        if pwd_md5:
            logger.info("[futu][API] unlock_trade()")
            unlock_ret = self.ctx.unlock_trade(pwd_md5, is_unlock=True)
            logger.info("[futu][API] unlock_trade result=%s", unlock_ret)

        self._resolve_acc_id()
        self._connected = True
        logger.info("[futu] connected env=%s acc_id=%s", env_str, self.acc_id)

    def close(self) -> None:
        if self.ctx:
            logger.info("[futu][API] ctx.close()")
            self.ctx.close()
            self._connected = False
        logger.info("[futu] disconnected")

    def _resolve_acc_id(self) -> None:
        from futu import RET_OK, TrdEnv, TrdMarket

        env_acc = os.getenv("FUTU_ACCOUNT_US", "").strip()

        logger.info("[futu][API] get_acc_list()")
        ret, df = self.ctx.get_acc_list()
        if ret != RET_OK:
            logger.error("[futu][API] get_acc_list failed: %s", df)
            raise RuntimeError(f"[futu] get_acc_list failed: {df}")
        logger.info("[futu][API] get_acc_list → %d accounts", len(df))

        def _total_assets(acc_row) -> float:
            try:
                ret2, df2 = self.ctx.accinfo_query(
                    trd_env=self.trd_env, acc_id=int(acc_row["acc_id"])
                )
                if ret2 != RET_OK or df2.empty:
                    return 0.0
                return float(df2.iloc[0].get("total_assets", 0) or 0)
            except Exception:
                return 0.0

        acc_ids = [int(r["acc_id"]) for _, r in df.iterrows()]

        if env_acc:
            target = int(env_acc)
            if target in acc_ids:
                self.acc_id = target
                ta = _total_assets(df[df["acc_id"] == target].iloc[0])
                logger.info("[futu] using FUTU_ACCOUNT_US=%s total_assets=%.0f", target, ta)
                return
            logger.warning("[futu] FUTU_ACCOUNT_US=%s not in acc_list, auto-picking", target)

        # 自动选资产最多的美股账户
        best_id, best_ta = None, -1.0
        for _, row in df.iterrows():
            ta = _total_assets(row)
            if ta > best_ta:
                best_ta = ta
                best_id = int(row["acc_id"])

        self.acc_id = best_id
        logger.info("[futu] auto-selected acc_id=%s total_assets=%.0f", best_id, best_ta)

    def get_position(self, code: str) -> int:
        """返回 US.CODE 当前持仓股数（can_sell_qty）"""
        from futu import RET_OK
        logger.debug("[futu][API] position_list_query(code=%s)", code)
        ret, df = self.ctx.position_list_query(
            code=code, trd_env=self.trd_env, acc_id=self.acc_id
        )
        if ret != RET_OK or df.empty:
            logger.debug("[futu][API] position_list_query(%s) → 无持仓 (ret=%s)", code, ret)
            return 0
        row = df[df["code"] == code]
        if row.empty:
            return 0
        qty = int(row.iloc[0].get("can_sell_qty", 0) or 0)
        logger.debug("[futu][API] position_list_query(%s) → can_sell_qty=%d", code, qty)
        return qty

    def wait_for_sellable(self, code: str, min_qty: int = 1,
                          poll_interval: float = 0.3, timeout: float = 2.5) -> int:
        """撤单后轮询直到 can_sell_qty >= min_qty 或超时（2026-09-02 fix）"""
        logger.info("[futu] wait_for_sellable(%s, min_qty=%d, timeout=%.1fs) 开始轮询", code, min_qty, timeout)
        deadline = time.monotonic() + timeout
        polls = 0
        while time.monotonic() < deadline:
            qty = self.get_position(code)
            polls += 1
            if qty >= min_qty:
                logger.info("[futu] wait_for_sellable(%s) 达标 qty=%d (轮询%d次)", code, qty, polls)
                return qty
            time.sleep(poll_interval)
        final_qty = self.get_position(code)
        logger.warning("[futu] wait_for_sellable(%s) 超时未达标 qty=%d/%d (轮询%d次)",
                       code, final_qty, min_qty, polls)
        return final_qty

    def _adjust_sell_qty(self, code: str, qty: int, purpose: str) -> int:
        """总是向券商查询真实 can_sell_qty，不依赖本地记录"""
        real = self.get_position(code)
        if real <= 0:
            logger.warning("[futu] %s: can_sell_qty=0 for %s, abort", purpose, code)
            return 0
        if real < qty:
            logger.warning("[futu] %s: adjusting sell qty %d→%d for %s", purpose, qty, real, code)
        return min(qty, real)

    def place_entry_order(self, ticker: str, budget_usd: float,
                          ref_price: float,
                          on_fill: Optional[Callable] = None) -> Optional[str]:
        """限价买入，+0.5% 滑点"""
        from futu import RET_OK, TrdSide, OrderType

        code = f"US.{ticker}"
        limit_price = round_to_us_tick(ref_price * (1 + _ENTRY_SLIPPAGE))
        qty = max(1, int(budget_usd / limit_price))

        logger.info("[futu][API] place_order(BUY code=%s qty=%d price=%.4f ref_price=%.4f budget=%.0f env=%s)",
                    code, qty, limit_price, ref_price, budget_usd, self.trd_env)
        ret, df = self.ctx.place_order(
            price=limit_price,
            qty=qty,
            code=code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,
            trd_env=self.trd_env,
            acc_id=self.acc_id,
        )
        if ret != RET_OK or df.empty:
            logger.error("[futu][API] place_order(BUY) failed ticker=%s: %s", ticker, df)
            return None

        order_id = str(df.iloc[0]["order_id"])
        if on_fill:
            _register_fill_callback(order_id, on_fill)
        logger.info("[futu] BUY 下单成功 order=%s %s qty=%d price=%.4f", order_id, ticker, qty, limit_price)
        return order_id

    def place_sell_order(self, ticker: str, qty: int, ref_price: float,
                         purpose: str = "sell",
                         on_fill: Optional[Callable] = None) -> Optional[str]:
        """限价卖出，-0.5% 滑点，注册止损升级监控"""
        from futu import RET_OK, TrdSide, OrderType

        code = f"US.{ticker}"
        qty = self._adjust_sell_qty(code, qty, purpose)
        if qty <= 0:
            logger.warning("[futu] SELL 中止 %s (%s): 可卖数量为0", ticker, purpose)
            return None

        limit_price = round_to_us_tick(ref_price * (1 + _SELL_SLIPPAGE))

        logger.info("[futu][API] place_order(SELL code=%s qty=%d price=%.4f ref_price=%.4f purpose=%s env=%s)",
                    code, qty, limit_price, ref_price, purpose, self.trd_env)
        ret, df = self.ctx.place_order(
            price=limit_price,
            qty=qty,
            code=code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.NORMAL,
            trd_env=self.trd_env,
            acc_id=self.acc_id,
        )
        if ret != RET_OK or df.empty:
            logger.error("[futu][API] place_order(SELL) failed %s %s: %s", purpose, ticker, df)
            return None

        order_id = str(df.iloc[0]["order_id"])
        if on_fill:
            _register_fill_callback(order_id, on_fill)
        stop_order_monitor.register(order_id, code, qty, ref_price, self)
        logger.info("[futu] SELL 下单成功 order=%s %s qty=%d price=%.4f (%s)",
                    order_id, ticker, qty, limit_price, purpose)
        return order_id

    def place_market_sell(self, ticker: str, qty: int, purpose: str = "market_sell") -> Optional[str]:
        """市价卖出（止损升级 / 紧急出局）"""
        from futu import RET_OK, TrdSide, OrderType

        code = f"US.{ticker}"
        qty = self._adjust_sell_qty(code, qty, purpose)
        if qty <= 0:
            logger.warning("[futu] MARKET_SELL 中止 %s (%s): 可卖数量为0", ticker, purpose)
            return None

        logger.info("[futu][API] place_order(MARKET_SELL code=%s qty=%d purpose=%s env=%s)",
                    code, qty, purpose, self.trd_env)
        ret, df = self.ctx.place_order(
            price=0,
            qty=qty,
            code=code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.MARKET,
            trd_env=self.trd_env,
            acc_id=self.acc_id,
        )
        if ret != RET_OK or df.empty:
            logger.error("[futu][API] place_order(MARKET_SELL) failed %s %s: %s", purpose, ticker, df)
            return None

        order_id = str(df.iloc[0]["order_id"])
        logger.info("[futu] MARKET_SELL 下单成功 order=%s %s qty=%d (%s)", order_id, ticker, qty, purpose)
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        """撤单（2026-08-21 fix: 必须用 ModifyOrderOp.CANCEL 枚举）"""
        from futu import RET_OK, ModifyOrderOp

        logger.info("[futu][API] modify_order(CANCEL order_id=%s)", order_id)
        ret, df = self.ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=self.trd_env,
            acc_id=self.acc_id,
        )
        ok = ret == RET_OK
        if ok:
            logger.info("[futu] 撤单成功 order=%s", order_id)
        else:
            logger.error("[futu][API] modify_order(CANCEL) failed order=%s: %s", order_id, df)
        stop_order_monitor.remove(order_id)
        return ok

    def get_price(self, ticker: str) -> Optional[float]:
        """获取最新成交价"""
        try:
            from futu import OpenQuoteContext, RET_OK
            host = os.getenv("FUTU_HOST", "127.0.0.1")
            port = int(os.getenv("FUTU_PORT", "11112"))
            logger.debug("[futu][API] get_market_snapshot(US.%s)", ticker)
            qctx = OpenQuoteContext(host=host, port=port)
            ret, df = qctx.get_market_snapshot([f"US.{ticker}"])
            qctx.close()
            if ret != RET_OK or df.empty:
                logger.warning("[futu][API] get_market_snapshot(US.%s) 无数据 ret=%s", ticker, ret)
                return None
            price = float(df.iloc[0]["last_price"])
            logger.debug("[futu][API] get_market_snapshot(US.%s) → last_price=%.4f", ticker, price)
            return price
        except Exception as e:
            logger.error("[futu][API] get_market_snapshot(US.%s) 异常: %s", ticker, e)
            return None
