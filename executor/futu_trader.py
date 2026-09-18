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
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from config.settings import (
    FUTU_ACCOUNT_US,
    FUTU_ACCOUNT_US_SIM,
    FUTU_HOST,
    FUTU_PORT,
    FUTU_TRADE_PWD,
    FUTU_TRADE_PWD_MD5,
    GURU_TRADE_ENV,
)
from notifier.feishu_bot import notify_order_filled, notify_order_placed, notify_trade_error

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

# 下单元数据：成交通知 / 轮询补漏都靠它
_order_meta: dict[str, dict] = {}
_notified_fill_state: dict[str, tuple] = {}
_notified_fill_lock = threading.Lock()


def _remember_order(order_id: str, **meta) -> None:
    _order_meta[order_id] = {"order_id": order_id, **meta}


def _pending_fill_ids() -> list[str]:
    with _cb_lock:
        return [oid for oid in _fill_callbacks if oid not in _cb_retire_at]


def _handle_fill_push(order_id: str, status: str, filled_qty: int, filled_price: float) -> None:
    """统一处理成交推送：callback + 飞书通知。轮询和 TradeOrderHandler 共用。"""
    is_final = status in ("FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED")
    fill_statuses = ("FILLED_PART", "FILLED_ALL", "CANCELLED_PART")

    if status in fill_statuses and filled_qty > 0:
        _dispatch_fill(order_id, filled_qty, filled_price, is_final=status == "FILLED_ALL")
        dedup_key = (status, filled_qty)
        with _notified_fill_lock:
            is_dup = _notified_fill_state.get(order_id) == dedup_key
            if not is_dup:
                _notified_fill_state[order_id] = dedup_key
        if not is_dup:
            meta = _order_meta.get(order_id, {})
            notify_order_filled({
                "order_id": order_id,
                "ticker": meta.get("ticker", ""),
                "code": meta.get("code", ""),
                "side": meta.get("side", ""),
                "purpose": meta.get("purpose", ""),
                "filled_qty": filled_qty,
                "filled_price": filled_price,
            })
        else:
            logger.debug("[futu] skip duplicate fill notify order=%s status=%s qty=%d",
                         order_id, status, filled_qty)

    if is_final:
        stop_order_monitor.remove(order_id)


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
        self.env_str = GURU_TRADE_ENV
        self._fill_handler_set = False

    def connect(self) -> None:
        from futu import TrdEnv, TrdMarket, OpenSecTradeContext

        self.env_str = GURU_TRADE_ENV
        self.trd_env = TrdEnv.REAL if self.env_str.upper() == "REAL" else TrdEnv.SIMULATE

        logger.info("[futu][API] OpenSecTradeContext(host=%s, port=%d, market=US)", FUTU_HOST, FUTU_PORT)
        self.ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=FUTU_HOST,
            port=FUTU_PORT,
            is_encrypt=False,
        )
        self._unlock()
        self._resolve_acc_id()
        self._setup_order_handler()
        self._connected = True
        logger.info("[futu] connected env=%s acc_id=%s", self.env_str, self.acc_id)

    def _unlock(self) -> None:
        """实盘必须解锁。对齐 US_YiDong_AutoTrader：优先 MD5，否则用 FUTU_TRADE_PWD 明文。"""
        from futu import RET_OK

        is_real = self.env_str.upper() == "REAL"
        if FUTU_TRADE_PWD_MD5:
            logger.info("[futu][API] unlock_trade(password_md5)")
            ret, data = self.ctx.unlock_trade(password_md5=FUTU_TRADE_PWD_MD5)
            if ret != RET_OK:
                raise RuntimeError(f"[futu] 解锁交易失败(MD5): {data}")
            logger.info("[futu] 交易已解锁（MD5）env=%s", self.env_str)
            return
        if FUTU_TRADE_PWD:
            logger.info("[futu][API] unlock_trade(FUTU_TRADE_PWD)")
            ret, data = self.ctx.unlock_trade(FUTU_TRADE_PWD)
            if ret != RET_OK:
                raise RuntimeError(f"[futu] 解锁交易失败: {data}")
            logger.info("[futu] 交易已解锁（明文密码）env=%s", self.env_str)
            return
        if is_real:
            raise RuntimeError(
                "[futu] 实盘必须配置 FUTU_TRADE_PWD 或 FUTU_TRADE_PWD_MD5"
                "（与 US_YiDong_AutoTrader 相同）"
            )
        logger.warning("[futu] 未设置交易密码，仿真盘可能不需要解锁")

    def _setup_order_handler(self) -> None:
        """订阅 Futu 成交推送。没有这个 handler，_dispatch_fill 永远不会被调用。"""
        from futu import TradeOrderHandlerBase, RET_OK

        class _OrderHandler(TradeOrderHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != RET_OK or data is None or data.empty:
                    return ret, data
                for _, row in data.iterrows():
                    order_id = str(row.get("order_id", ""))
                    status = str(row.get("order_status", ""))
                    filled_qty = int(row.get("dealt_qty", 0) or 0)
                    filled_price = float(row.get("dealt_avg_price", 0) or 0)
                    logger.info("[futu] 订单推送 order=%s status=%s filled=%d@%.4f",
                                order_id, status, filled_qty, filled_price)
                    if not order_id:
                        continue
                    if not _order_meta.get(order_id):
                        code = str(row.get("code", "") or "")
                        side = str(row.get("trd_side", "") or "")
                        _remember_order(
                            order_id,
                            ticker=code.replace("US.", "") if code else "",
                            code=code,
                            side="SELL" if "SELL" in side.upper() else "BUY",
                            purpose=str(row.get("remark", "") or ""),
                        )
                    _handle_fill_push(order_id, status, filled_qty, filled_price)
                return ret, data

        self.ctx.set_handler(_OrderHandler())
        self._fill_handler_set = True
        logger.info("[futu] TradeOrderHandler 已注册")

    def wait_for_pending_fills(self, timeout: float = 90.0) -> None:
        """cron 短进程：下单后等成交推送/轮询，避免还没收到成交就退出。"""
        from futu import RET_OK

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = _pending_fill_ids()
            if not pending:
                return
            logger.info("[futu] 等待成交回报 %d 笔: %s", len(pending), pending)
            try:
                ret, df = self.ctx.order_list_query(trd_env=self.trd_env, acc_id=self.acc_id)
                if ret == RET_OK and df is not None and not df.empty:
                    for _, row in df.iterrows():
                        oid = str(row.get("order_id", ""))
                        if oid not in pending:
                            continue
                        status = str(row.get("order_status", ""))
                        filled_qty = int(row.get("dealt_qty", 0) or 0)
                        filled_price = float(row.get("dealt_avg_price", 0) or 0)
                        if filled_qty > 0 or status in (
                            "FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED"
                        ):
                            _handle_fill_push(oid, status, filled_qty, filled_price)
            except Exception as e:
                logger.error("[futu] order_list_query 轮询失败: %s", e)
            time.sleep(1)

        leftover = _pending_fill_ids()
        if leftover:
            logger.warning("[futu] 等待成交超时(%.0fs)，仍未完成: %s", timeout, leftover)

    def close(self) -> None:
        if self.ctx:
            logger.info("[futu][API] ctx.close()")
            self.ctx.close()
            self._connected = False
        logger.info("[futu] disconnected")

    def _resolve_acc_id(self) -> None:
        """选出与当前 trd_env 匹配的可用账户。

        2026-09-18 修复：原实现用**全部**账户列表校验 FUTU_ACCOUNT_US，
        没按 trd_env 过滤。结果 SIMULATE 模式下实盘账户ID"校验通过"，
        直到 place_order 才报 "Nonexisting acc_id"，导致连续多日静默空转。
        acc_list 同时包含 REAL / SIMULATE 两套账户，两者 acc_id 完全不同。
        """
        from futu import RET_OK, TrdEnv

        is_sim = self.trd_env == TrdEnv.SIMULATE
        env_key = "FUTU_ACCOUNT_US_SIM" if is_sim else "FUTU_ACCOUNT_US"
        env_acc = (FUTU_ACCOUNT_US_SIM if is_sim else FUTU_ACCOUNT_US).strip()
        want_env = "SIMULATE" if is_sim else "REAL"

        logger.info("[futu][API] get_acc_list()")
        ret, df = self.ctx.get_acc_list()
        if ret != RET_OK:
            logger.error("[futu][API] get_acc_list failed: %s", df)
            raise RuntimeError(f"[futu] get_acc_list failed: {df}")
        logger.info("[futu][API] get_acc_list → %d accounts", len(df))

        # 只保留 trd_env 匹配且状态 ACTIVE 的账户
        cand = df[df["trd_env"].astype(str).str.upper() == want_env]
        if "acc_status" in cand.columns:
            cand = cand[cand["acc_status"].astype(str).str.upper() == "ACTIVE"]
        if cand.empty:
            raise RuntimeError(
                f"[futu] OpenD 中没有可用的 {want_env} 美股账户（共{len(df)}个账户）。"
                f"请检查 OpenD 登录状态，或确认该环境下已开通模拟/实盘交易权限。"
            )
        logger.info("[futu] %s 环境下可用账户: %s", want_env,
                    [int(r["acc_id"]) for _, r in cand.iterrows()])

        def _total_assets(acc_id: int) -> float:
            try:
                ret2, df2 = self.ctx.accinfo_query(trd_env=self.trd_env, acc_id=acc_id)
                if ret2 != RET_OK or df2.empty:
                    return 0.0
                return float(df2.iloc[0].get("total_assets", 0) or 0)
            except Exception:
                return 0.0

        cand_ids = [int(r["acc_id"]) for _, r in cand.iterrows()]

        if not is_sim and not env_acc:
            raise RuntimeError(
                "[futu] 实盘必须显式配置 FUTU_ACCOUNT_US，禁止自动选择账户"
                f"（当前 REAL 可用账户: {cand_ids}）"
            )

        if env_acc:
            target = int(env_acc)
            if target in cand_ids:
                ta = _total_assets(target)
                if not is_sim and ta <= 0:
                    raise RuntimeError(
                        f"[futu] 实盘账户 {target} 总资产为 0，拒绝下单。"
                        "请确认 FUTU_ACCOUNT_US 是否为有资金的 REAL 账户。"
                    )
                self.acc_id = target
                logger.info("[futu] 使用 %s=%s (env=%s) total_assets=%.0f",
                            env_key, target, want_env, ta)
                return
            msg = (
                f"[futu] {env_key}={target} 不是可用的 {want_env} 账户"
                f"（该环境可用: {cand_ids}）"
            )
            if not is_sim:
                raise RuntimeError(msg + "。实盘禁止自动改选账户。")
            logger.warning("%s，改为自动选择。", msg)

        best_id, best_ta = None, -1.0
        for aid in cand_ids:
            ta = _total_assets(aid)
            if ta > best_ta:
                best_ta, best_id = ta, aid

        self.acc_id = best_id
        logger.info("[futu] 自动选定 acc_id=%s (env=%s) total_assets=%.0f",
                    best_id, want_env, best_ta)

    def list_positions(self) -> list[dict]:
        """券商当前美股持仓快照。qty 用 can_sell_qty（可卖），成本用 cost_price。"""
        from futu import RET_OK

        logger.info("[futu][API] position_list_query(all)")
        ret, df = self.ctx.position_list_query(trd_env=self.trd_env, acc_id=self.acc_id)
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query failed: {df}")
        if df is None or df.empty:
            logger.info("[futu][API] position_list_query → 无持仓")
            return []
        out = []
        for _, row in df.iterrows():
            code = str(row.get("code", "") or "")
            if not code.startswith("US."):
                continue
            qty = int(row.get("can_sell_qty", 0) or row.get("qty", 0) or 0)
            if qty <= 0:
                continue
            cost = float(row.get("cost_price", 0) or row.get("diluted_cost", 0) or 0)
            ticker = code.replace("US.", "", 1)
            out.append({"code": code, "ticker": ticker, "qty": qty, "cost_price": cost})
        logger.info("[futu] 券商美股持仓 %d 只: %s",
                    len(out), ", ".join(f"{p['ticker']}:{p['qty']}" for p in out) or "-")
        return out

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
            notify_trade_error(
                f"买入下单失败 {ticker}",
                f"**股票**: `{ticker}`\n**参考价**: ${ref_price:.2f}\n**预算**: ${budget_usd:,.0f}\n**原因**: {df}",
            )
            return None

        order_id = str(df.iloc[0]["order_id"])
        _remember_order(
            order_id,
            ticker=ticker,
            code=code,
            side="BUY",
            qty=qty,
            price=limit_price,
            purpose="ENTRY",
            env=self.env_str,
        )
        if on_fill:
            _register_fill_callback(order_id, on_fill)
        notify_order_placed({
            "order_id": order_id,
            "ticker": ticker,
            "code": code,
            "side": "BUY",
            "qty": qty,
            "price": limit_price,
            "purpose": "ENTRY",
            "env": self.env_str,
        })
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
            notify_trade_error(
                f"卖出下单失败 {ticker}",
                f"**股票**: `{ticker}`\n**用途**: {purpose}\n**数量**: {qty} 股\n**参考价**: ${ref_price:.2f}\n**原因**: {df}",
            )
            return None

        order_id = str(df.iloc[0]["order_id"])
        _remember_order(
            order_id,
            ticker=ticker,
            code=code,
            side="SELL",
            qty=qty,
            price=limit_price,
            purpose=purpose,
            env=self.env_str,
        )
        if on_fill:
            _register_fill_callback(order_id, on_fill)
        stop_order_monitor.register(order_id, code, qty, ref_price, self)
        notify_order_placed({
            "order_id": order_id,
            "ticker": ticker,
            "code": code,
            "side": "SELL",
            "qty": qty,
            "price": limit_price,
            "purpose": purpose,
            "env": self.env_str,
        })
        logger.info("[futu] SELL 下单成功 order=%s %s qty=%d price=%.4f (%s)",
                    order_id, ticker, qty, limit_price, purpose)
        return order_id

    def place_market_sell(self, ticker: str, qty: int, purpose: str = "market_sell") -> Optional[str]:
        """市价卖出（止损升级 / 紧急出局）"""
        from futu import RET_OK, TrdSide, OrderType

        ticker = str(ticker).replace("US.", "")
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
            notify_trade_error(
                f"市价卖出失败 {ticker}",
                f"**股票**: `{ticker}`\n**用途**: {purpose}\n**数量**: {qty} 股\n**原因**: {df}",
            )
            return None

        order_id = str(df.iloc[0]["order_id"])
        _remember_order(
            order_id,
            ticker=ticker,
            code=code,
            side="SELL",
            qty=qty,
            price=0,
            purpose=purpose,
            env=self.env_str,
        )
        notify_order_placed({
            "order_id": order_id,
            "ticker": ticker,
            "code": code,
            "side": "SELL",
            "qty": qty,
            "price": 0,
            "purpose": purpose,
            "env": self.env_str,
        })
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
            host = FUTU_HOST
            port = FUTU_PORT
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
