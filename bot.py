#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║   AUTONOMOUS BITCOIN TRADING SYSTEM — v13.0 FINAL                    ║
║   Single File · Plug & Play · Production Ready · Railway.app         ║
╠══════════════════════════════════════════════════════════════════════╣
║  WHAT'S NEW IN V13 vs V12:                                           ║
║  • 4H Dominant Trend Bias: +15pts strong / +8pts moderate to winner  ║
║  • Score tie-break: when scores within 15%, 4H trend decides         ║
║  • Bull score + Bear score logged every scan                          ║
║  • Dashboard Connection Health Panel: Binance status, last scan       ║
║  • Dashboard Signal Monitor: bull/bear score, 4H trend, last signal   ║
║  • Binance ping health check every 5 minutes                          ║
║  • Scan counter per hour — proves bot is actively scanning            ║
║  • Last rejection reason shown on dashboard                           ║
║  • All V12 features: dynamic risk, leverage, TP1 15%, runner 85%     ║
╠══════════════════════════════════════════════════════════════════════╣
║  DEPLOY IN 4 STEPS:                                                  ║
║  1. Edit settings.env — replace YOUR_... with real values            ║
║  2. Railway → Volume → mount at /data                                ║
║  3. Push folder to private GitHub repo                               ║
║  4. Railway → New Project → Deploy from GitHub → add env vars        ║
║  ONLY manual tasks forever: add/withdraw money, renew API key        ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════
# IMPORTS
# ═══════════════════════════════════════════════════════════════════
import os, sys, asyncio, uuid, json, time, signal, math, logging
import traceback, hashlib
import requests
from datetime    import datetime, timedelta, date
from dataclasses import dataclass, field
from typing      import Optional, Tuple, List, Dict
from zoneinfo    import ZoneInfo

import pandas  as pd
import numpy   as np
from dotenv    import load_dotenv
from loguru    import logger
from tenacity  import retry, stop_after_attempt, wait_exponential

from sqlalchemy import (create_engine, Column, Integer, Float,
                        String, Boolean, DateTime, Text, Date)
from sqlalchemy.orm import declarative_base, sessionmaker

from binance.client     import Client
from binance.exceptions import BinanceAPIException

from ta.trend      import EMAIndicator, MACD, ADXIndicator
from ta.momentum   import RSIIndicator, StochRSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume     import OnBalanceVolumeIndicator

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security        import HTTPBasic, HTTPBasicCredentials
from fastapi.responses       import HTMLResponse
import uvicorn
import secrets as _secrets

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════
load_dotenv("settings.env")

# ── IST timezone for daily resets ──────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")
def now_ist() -> datetime: return datetime.now(IST)
def today_ist() -> date:   return now_ist().date()

def _f(k, d): return float(os.getenv(k, d))
def _i(k, d): return int(os.getenv(k, d))
def _s(k, d): return str(os.getenv(k, d))
def _b(k, d): return os.getenv(k, d).lower() == "true"

CFG = {
    # Binance
    "API_KEY":           _s("BINANCE_API_KEY",           ""),
    "API_SECRET":        _s("BINANCE_API_SECRET",        ""),
    "SYMBOL":            _s("TRADING_PAIR",              "BTCUSDT"),
    "TESTNET":           _b("USE_TESTNET",               "false"),

    # Capital
    "INITIAL_CAPITAL":   _f("INITIAL_CAPITAL_USDT",     "1300"),

    # Risk
    "RISK_PCT":          _f("RISK_PER_TRADE_PCT",        "3.0"),   # base/fallback only
    # V13: Dynamic risk tiers — overridden per trade based on signal probability
    # 62-69% → 5% | 70-74% → 10% | 75-79% → 15% | 80-84% → 20% | 85%+ → 30%
    "MAX_DAILY_LOSS":    _f("MAX_DAILY_LOSS_PCT",        "5.0"),
    "CAUTION_LOSS":      _f("CAUTION_LOSS_PCT",          "3.0"),
    "MIN_WIN_PROB":      _f("MIN_WIN_PROBABILITY",       "62.0"),
    "MAX_LEVERAGE":      _i("MAX_LEVERAGE",              "75"),   # V13: up to 75x (hard cap)
    "MAX_SPREAD_PCT":    _f("MAX_SPREAD_PCT",            "0.10"),
    "MAX_CONSEC_LOSS":   _i("MAX_CONSECUTIVE_LOSSES",   "5"),     # V9: raised 3→5
    # V9: No daily trade cap — system trades unlimited as long as no drawdown/loss guards hit
    "PAPER_HOURS":       _f("PAPER_RECOVERY_HOURS",     "1.0"),
    "PAPER_MIN_TRADES":  _i("PAPER_MIN_TRADES",         "2"),      # V7: min paper wins before live
    "MAX_TRADE_AGE_H":   _f("MAX_TRADE_AGE_HOURS",      "24.0"),
    "WEEKLY_LOSS_LIMIT": _f("WEEKLY_LOSS_LIMIT_PCT",    "15.0"),
    # V8: Adaptive scan intervals
    "SCAN_FAST_S":       _i("SCAN_FAST_SECONDS",        "15"),     # no open trade
    "SCAN_SLOW_S":       _i("SCAN_SLOW_SECONDS",        "60"),     # managing trade
    # V9: Loss cooldown removed — signal gates (68% prob, 12 rules, trend align) are sufficient

    # V7: Multi-TP split
    "TP1_RATIO":         _f("TP1_ATR_RATIO",            "1.5"),
    "TP2_RATIO":         _f("TP2_ATR_RATIO",            "2.5"),
    "TP3_RATIO":         _f("TP3_ATR_RATIO",            "4.0"),
    # V13: TP1=15% (fee recovery + lock profit), Runner=85% adaptive ATR trail
    "TP1_PCT":           _f("TP1_CLOSE_PCT",            "15.0"),  # small lock-in
    "TP2_PCT":           _f("TP2_CLOSE_PCT",            "0.0"),   # V13: no fixed TP2 — runner rides trend
    # TP3 gets the remainder automatically

    # Tax (India)
    "TAX_RATE":          _f("TAX_RATE",                 "0.30"),
    "TDS_RATE":          _f("TDS_RATE",                 "0.01"),

    # Dashboard
    "DASH_PASS":         _s("DASHBOARD_PASSWORD",       "admin123"),
    "PORT":              _i("PORT",                     "8080"),

    # Telegram
    "TG_TOKEN":          _s("TELEGRAM_BOT_TOKEN",       ""),
    "TG_CHAT":           _s("TELEGRAM_CHAT_ID",         ""),

    # News
    "NEWS_KEY":          _s("NEWS_API_KEY",             ""),

    # Self-healing
    "SELF_HEAL":         _b("ENABLE_SELF_HEALING",      "true"),
    "MAX_HEAL_ATTEMPTS": _i("MAX_HEAL_ATTEMPTS",        "3"),
}

# Binance Futures fee constants
TAKER_FEE   = 0.0004   # 0.04% market orders
MAKER_FEE   = 0.0002   # 0.02% limit orders
FUNDING_8H  = 0.0001   # 0.01% per 8h (conservative estimate)
MIN_PROFIT  = 0.50     # minimum $0.50 net after all fees
MIN_RR      = 1.5      # minimum Risk:Reward ratio

VERSION = "13.0 FINAL"

# ═══════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════
def setup_logging():
    os.makedirs("logs", exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add("logs/system.log",  rotation="10 MB", retention="30 days", level="DEBUG")
    logger.add("logs/trades.log",  rotation="5 MB",  retention="90 days",
        filter=lambda r: "TRADE" in r["message"])
    logger.add("logs/errors.log",  rotation="5 MB",  retention="30 days", level="ERROR")
    logger.add("logs/healer.log",  rotation="5 MB",  retention="30 days",
        filter=lambda r: "HEAL" in r["message"] or "BUG" in r["message"])

# ═══════════════════════════════════════════════════════════════════
# DATABASE — SQLite on Railway persistent volume
# ═══════════════════════════════════════════════════════════════════
_DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_URL   = f"sqlite:///{_DATA_DIR}/trading.db"
_engine   = create_engine(_DB_URL, connect_args={"check_same_thread": False})
_Session  = sessionmaker(bind=_engine)
Base      = declarative_base()


class Trade(Base):
    __tablename__    = "trades"
    id               = Column(Integer, primary_key=True)
    trade_id         = Column(String,  unique=True, index=True)
    direction        = Column(String)
    is_paper         = Column(Boolean, default=False)
    entry_price      = Column(Float)
    fill_price       = Column(Float,   nullable=True)   # V8: actual Binance fill price
    exit_price       = Column(Float,   nullable=True)
    stop_loss        = Column(Float)
    take_profit1     = Column(Float)
    take_profit2     = Column(Float)
    take_profit3     = Column(Float)                   # V7: third TP
    quantity         = Column(Float)
    qty_remaining    = Column(Float,   nullable=True)  # V7: partial close tracking
    leverage         = Column(Integer)
    win_prob         = Column(Float)
    entry_time       = Column(DateTime, default=datetime.utcnow)
    exit_time        = Column(DateTime, nullable=True)
    status           = Column(String,   default="OPEN")
    gross_pnl        = Column(Float,    default=0.0)
    fees_total       = Column(Float,    default=0.0)
    tax_liability    = Column(Float,    default=0.0)
    net_for_trading  = Column(Float,    default=0.0)
    exit_reason      = Column(String,   nullable=True)
    regime           = Column(String,   nullable=True)
    spread           = Column(Float,    nullable=True)
    funding_rate     = Column(Float,    nullable=True)
    session          = Column(String,   nullable=True)
    tp1_hit          = Column(Boolean,  default=False)  # V7: partial exit tracking
    tp2_hit          = Column(Boolean,  default=False)
    partial_pnl      = Column(Float,    default=0.0)    # V7: PnL banked from partial exits
    candle_pattern   = Column(String,   nullable=True)  # V8: pattern that confirmed signal
    signal_rules     = Column(Text,     nullable=True)  # V8: top rules that fired


class DailyLog(Base):
    """One row per calendar day — drives all P&L and tax reports."""
    __tablename__    = "daily_log"
    id               = Column(Integer, primary_key=True)
    log_date         = Column(Date,    unique=True, index=True)
    opening_capital  = Column(Float,   default=0.0)
    closing_capital  = Column(Float,   default=0.0)
    gross_pnl        = Column(Float,   default=0.0)
    fees             = Column(Float,   default=0.0)
    net_for_trading  = Column(Float,   default=0.0)
    tax_liability    = Column(Float,   default=0.0)
    trades_count     = Column(Integer, default=0)
    wins             = Column(Integer, default=0)
    losses           = Column(Integer, default=0)
    paper_trades     = Column(Integer, default=0)


class TaxLedger(Base):
    """Running tax liability — separate from trading capital."""
    __tablename__  = "tax_ledger"
    id             = Column(Integer, primary_key=True)
    trade_id       = Column(String,  index=True)
    trade_date     = Column(DateTime, default=datetime.utcnow)
    gross_profit   = Column(Float,   default=0.0)
    tax_amount     = Column(Float,   default=0.0)
    tds_amount     = Column(Float,   default=0.0)
    is_paid        = Column(Boolean, default=False)
    paid_date      = Column(DateTime, nullable=True)


class KV(Base):
    """Key-value store — all runtime state survives restarts."""
    __tablename__ = "kv"
    key           = Column(String,  primary_key=True)
    value         = Column(Text)
    updated_at    = Column(DateTime, default=datetime.utcnow)


class AlertLog(Base):
    __tablename__ = "alert_log"
    id            = Column(Integer, primary_key=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    level         = Column(String)
    message       = Column(Text)


class HealLog(Base):
    """Self-healing events — full audit trail."""
    __tablename__ = "heal_log"
    id            = Column(Integer, primary_key=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    error_hash    = Column(String,   index=True)
    error_type    = Column(String)
    error_msg     = Column(Text)
    fix_applied   = Column(Text)
    fix_success   = Column(Boolean, default=False)
    attempts      = Column(Integer, default=1)


class SignalLog(Base):
    """V8: Every signal decision logged — TAKEN or why SKIPPED."""
    __tablename__ = "signal_log"
    id            = Column(Integer, primary_key=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    direction     = Column(String)
    prob          = Column(Float)
    outcome       = Column(String)   # TAKEN | SKIPPED_PROB | SKIPPED_FEES | SKIPPED_FILTER
    skip_reason   = Column(Text,    nullable=True)
    rules_fired   = Column(Text,    nullable=True)
    regime        = Column(String,  nullable=True)
    session       = Column(String,  nullable=True)


def db_init():
    """Create all tables and seed default state values."""
    Base.metadata.create_all(_engine)
    db = _Session()
    init_cap = str(CFG["INITIAL_CAPITAL"])
    today    = today_ist().isoformat()
    defaults = {
        "mode":               "LIVE",
        "manual_stop":        "false",
        "stop_reason":        "",
        "capital":            init_cap,
        "peak_capital":       init_cap,
        "day_start_cap":      init_cap,
        "week_start_cap":     init_cap,
        "week_start_date":    today,
        "total_trades":       "0",
        "wins":               "0",
        "losses":             "0",
        "consecutive_losses": "0",
        "win_rate":           "0.0",
        "day_trade_count":    "0",
        "paper_start":        "",
        "paper_pnl":          "0.0",
        "paper_wins":         "0",      # V7: paper win counter for recovery gate
        "last_heartbeat":     datetime.utcnow().isoformat(),
        "last_loss_time":     "",       # retained for schema compat (cooldown removed in V9)
        "tax_liability_total":"0.0",
        "heal_paused":        "false",
        "heal_attempts":      "0",
        "last_error_hash":    "",
        "system_version":     VERSION,
        # V13: Connection health + signal transparency
        "last_bull_score":    "0",
        "last_bear_score":    "0",
        "last_4h_trend":      "NEUTRAL",
        "last_score_time":    "",
        "last_signal_prob":   "0",
        "last_signal_dir":    "",
        "last_signal_time":   "",
        "last_reject_reason": "",
        "last_binance_ping":  "",
        "scan_count_hour":    "0",
        "scan_hour_start":    "",
        # V7 performance stats
        "total_gross":        "0.0",
        "total_fees":         "0.0",
        "total_wins_amt":     "0.0",
        "total_losses_amt":   "0.0",
    }
    for k, v in defaults.items():
        if not db.query(KV).filter(KV.key == k).first():
            db.add(KV(key=k, value=v))
    db.commit()
    db.close()
    logger.info(f"✓ Database ready at {_DATA_DIR}/trading.db")


def kget(key: str, default: str = "") -> str:
    db  = _Session()
    row = db.query(KV).filter(KV.key == key).first()
    db.close()
    return row.value if row else default


def kset(key: str, value):
    db  = _Session()
    row = db.query(KV).filter(KV.key == key).first()
    if row:
        row.value      = str(value)
        row.updated_at = datetime.utcnow()
    else:
        db.add(KV(key=key, value=str(value)))
    db.commit()
    db.close()


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════
def tg(msg: str, level: str = "INFO"):
    token = CFG["TG_TOKEN"]
    chat  = CFG["TG_CHAT"]
    if not token or "YOUR_" in token:
        return
    emoji = {
        "INFO":  "ℹ️",  "TRADE": "📊",
        "WARN":  "⚠️",  "CRIT":  "🚨",
        "WIN":   "✅",  "LOSS":  "❌",
        "HEAL":  "🔧",  "PARTIAL": "🎯",
    }.get(level, "📌")
    mode = "🧪TESTNET" if CFG["TESTNET"] else "💰LIVE"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat,
                  "text": f"{emoji} *BTC Bot v{VERSION} [{mode}]*\n\n{msg}",
                  "parse_mode": "Markdown"},
            timeout=8)
    except Exception as e:
        logger.warning(f"Telegram error: {e}")


def alert_log(level: str, msg: str):
    try:
        db = _Session()
        db.add(AlertLog(level=level, message=str(msg)[:500]))
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"alert_log error: {e}")
    if level in ("WARN", "CRIT", "HEAL"):
        tg(msg, level)


# ═══════════════════════════════════════════════════════════════════
# BINANCE CLIENT
# ═══════════════════════════════════════════════════════════════════
_binance: Optional[Client] = None


def get_client() -> Client:
    global _binance
    if _binance is None:
        if CFG["TESTNET"]:
            _binance = Client(CFG["API_KEY"], CFG["API_SECRET"], testnet=True)
            logger.warning("⚠️  TESTNET MODE — no real money at risk")
        else:
            _binance = Client(CFG["API_KEY"], CFG["API_SECRET"])
    return _binance


def reset_client():
    """Force reconnect — used by self-healer after API errors."""
    global _binance
    _binance = None
    logger.info("HEAL: Binance client reset — will reconnect on next call")


# ═══════════════════════════════════════════════════════════════════
# FEE & TAX CALCULATOR
# KEY DESIGN: Tax stays in capital for trading. Tracked as liability.
# Capital compounds on FULL net (gross - fees). Tax paid separately.
# ═══════════════════════════════════════════════════════════════════
@dataclass
class FeeCheck:
    viable:          bool
    fees:            float
    tax_liability:   float
    net_for_trading: float
    rr_ratio:        float
    expected_net:    float   # V7: expected value using win probability
    reason:          str = ""


def check_viable(direction: str, entry: float, sl: float,
                 tp1: float, tp2: float, tp3: float,
                 qty: float, win_prob: float) -> FeeCheck:
    """
    Pre-trade gate — only proceeds if:
    1. Expected value (EV) > $0.50 after fees (win_prob-weighted)
    2. R:R >= 1.5 (to TP2 minimum)
    Tax is tracked as liability, does NOT reduce trading capital.
    V7: Multi-TP aware — uses weighted avg of TP1/TP2/TP3 exits.
    """
    tp1_qty = qty * (CFG["TP1_PCT"] / 100)
    tp2_qty = qty * (CFG["TP2_PCT"] / 100)
    tp3_qty = qty - tp1_qty - tp2_qty

    pos_val = entry * qty
    e_fee   = pos_val * TAKER_FEE
    x_fee   = (tp1 * tp1_qty + tp2 * tp2_qty + tp3 * tp3_qty) * MAKER_FEE
    # Conservative funding estimate: avg hold 6h
    funding = pos_val * FUNDING_8H * 0.75
    fees    = e_fee + x_fee + funding

    if direction == "BUY":
        gross  = ((tp1 - entry)*tp1_qty +
                  (tp2 - entry)*tp2_qty +
                  (tp3 - entry)*tp3_qty)
        reward = abs(tp2 - entry)
        risk   = abs(entry - sl)
        sl_loss= (sl - entry) * qty  # negative
    else:
        gross  = ((entry - tp1)*tp1_qty +
                  (entry - tp2)*tp2_qty +
                  (entry - tp3)*tp3_qty)
        reward = abs(entry - tp2)
        risk   = abs(sl - entry)
        sl_loss= (entry - sl) * qty  # negative

    net_for_trading = gross - fees
    tax_liability   = max(0.0, net_for_trading) * CFG["TAX_RATE"]
    rr              = round(reward / risk, 2) if risk > 0 else 0.0

    # V7: Expected value check
    wp = win_prob / 100.0
    ev = (wp * net_for_trading) + ((1 - wp) * (sl_loss - fees))

    reasons = []
    if net_for_trading < MIN_PROFIT:
        reasons.append(f"Net ${net_for_trading:.4f} < min ${MIN_PROFIT}")
    if rr < MIN_RR:
        reasons.append(f"R:R {rr:.2f} < min {MIN_RR:.1f}")
    if ev < 0:
        reasons.append(f"Negative EV: ${ev:.4f}")

    viable = len(reasons) == 0
    if not viable:
        logger.info(f"Fee gate rejected: {' | '.join(reasons)}")

    return FeeCheck(
        viable          = viable,
        fees            = round(fees, 6),
        tax_liability   = round(tax_liability, 6),
        net_for_trading = round(net_for_trading, 6),
        rr_ratio        = rr,
        expected_net    = round(ev, 6),
        reason          = " | ".join(reasons),
    )


def actual_pnl(direction: str, entry: float, exit_p: float,
               qty: float, hours: float = 4.0) -> dict:
    """Actual P&L on close. Tax is tracked, NOT deducted from capital."""
    pos_val = entry * qty
    e_fee   = pos_val * TAKER_FEE
    x_fee   = exit_p  * qty * TAKER_FEE
    funding = pos_val * FUNDING_8H * (hours / 8.0)
    fees    = e_fee + x_fee + funding
    gross   = ((exit_p - entry) if direction == "BUY"
               else (entry - exit_p)) * qty
    net_for_trading = gross - fees
    tax_liability   = max(0.0, net_for_trading) * CFG["TAX_RATE"]
    tds = pos_val * CFG["TDS_RATE"] if pos_val > 50000 else 0.0
    return {
        "gross":           round(gross, 6),
        "fees":            round(fees, 6),
        "net_for_trading": round(net_for_trading, 6),
        "tax_liability":   round(tax_liability, 6),
        "tds":             round(tds, 6),
    }


def dynamic_risk_pct(prob: float, regime_score: int, adx: float) -> float:
    """V13: Risk percentage scales with signal quality.
    62-69% → 5% | 70-74% → 10% | 75-79% → 15% | 80-84% → 20% | 85%+ → 30%
    Extra confirmation required for 30%: strong regime + high ADX."""
    if prob >= 85:
        # Only deploy 30% if market quality confirms high confidence
        if regime_score >= 60 and adx >= 25:
            return 30.0
        return 20.0   # high prob but market quality not ideal
    elif prob >= 80: return 20.0
    elif prob >= 75: return 15.0
    elif prob >= 70: return 10.0
    else:            return 5.0


def position_size(capital: float, entry: float,
                  sl: float, leverage: int,
                  prob: float = 62.0,
                  regime_score: int = 50,
                  adx: float = 20.0) -> dict:
    """V13 FIX: Uses 90% of capital as max margin to avoid edge-case failures.
    Dynamic risk scales with signal quality. Always enforces 0.001 BTC minimum."""
    MIN_QTY    = 0.001
    MAX_MARGIN = capital * 0.90   # never exceed 90% of capital as margin

    dist = abs(entry - sl)
    if dist <= 0:
        return {"qty": 0, "error": "SL distance is zero"}

    # Dynamic risk based on probability tier
    risk_pct = dynamic_risk_pct(prob, regime_score, adx)
    risk_usd = capital * (risk_pct / 100.0)

    qty = risk_usd / dist
    qty = round(qty, 3)

    if qty < MIN_QTY:
        qty = MIN_QTY

    margin = round((entry * qty) / leverage, 2)

    # Cap qty so margin never exceeds 90% of capital
    if margin > MAX_MARGIN:
        qty    = round((MAX_MARGIN * leverage) / entry, 3)
        if qty < MIN_QTY:
            qty = MIN_QTY
        margin = round((entry * qty) / leverage, 2)

    # Hard fail only if truly cannot afford even minimum order
    if margin > capital:
        return {"qty": 0, "error": f"Capital too low: need ${margin:.2f}, have ${capital:.2f}"}

    return {"qty": qty, "margin": margin, "error": None, "risk_pct": risk_pct}


# ═══════════════════════════════════════════════════════════════════
# SPREAD + FUNDING FILTER
# ═══════════════════════════════════════════════════════════════════
def spread_ok() -> Tuple[bool, float]:
    try:
        ob  = get_client().futures_orderbook_ticker(symbol=CFG["SYMBOL"])
        bid = float(ob["bidPrice"])
        ask = float(ob["askPrice"])
        pct = ((ask - bid) / bid) * 100
        ok  = pct <= CFG["MAX_SPREAD_PCT"]
        if not ok:
            logger.info(f"Spread {pct:.4f}% > max {CFG['MAX_SPREAD_PCT']}% — skip")
        return ok, round(pct, 6)
    except Exception as e:
        logger.warning(f"Spread check error (allowing): {e}")
        return True, 0.0


# ═══════════════════════════════════════════════════════════════════
# MARKET DATA ENGINE
# ═══════════════════════════════════════════════════════════════════
@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30))
def fetch_klines(tf: str, limit: int = 220) -> pd.DataFrame:
    intervals = {
        "1m":  Client.KLINE_INTERVAL_1MINUTE,
        "5m":  Client.KLINE_INTERVAL_5MINUTE,
        "15m": Client.KLINE_INTERVAL_15MINUTE,
        "1h":  Client.KLINE_INTERVAL_1HOUR,
        "4h":  Client.KLINE_INTERVAL_4HOUR,
    }
    raw = get_client().futures_klines(
        symbol=CFG["SYMBOL"], interval=intervals[tf], limit=limit)
    df = pd.DataFrame(raw, columns=[
        "ts","open","high","low","close","vol",
        "cts","qvol","trades","tbbase","tbquote","ignore"])
    for c in ["open","high","low","close","vol"]:
        df[c] = pd.to_numeric(df[c])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df.set_index("ts")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["close"], df["high"], df["low"], df["vol"]

    for w in [9, 21, 50, 200]:
        df[f"ema{w}"] = EMAIndicator(c, w).ema_indicator()

    m = MACD(c)
    df["macd"]     = m.macd()
    df["macd_sig"] = m.macd_signal()
    df["macd_h"]   = m.macd_diff()

    df["rsi"]    = RSIIndicator(c, 14).rsi()
    sr           = StochRSIIndicator(c, 14)
    df["srsi_k"] = sr.stochrsi_k()
    df["srsi_d"] = sr.stochrsi_d()

    adx           = ADXIndicator(h, l, c, 14)
    df["adx"]     = adx.adx()
    df["adx_pos"] = adx.adx_pos()
    df["adx_neg"] = adx.adx_neg()

    bb           = BollingerBands(c, 20)
    df["bb_up"]  = bb.bollinger_hband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_lo"]  = bb.bollinger_lband()
    df["bb_pct"] = bb.bollinger_pband()
    df["bb_wid"] = (df["bb_up"] - df["bb_lo"]) / df["bb_mid"]

    df["atr"]    = AverageTrueRange(h, l, c, 14).average_true_range()
    df["obv"]    = OnBalanceVolumeIndicator(c, v).on_balance_volume()

    tp           = (h + l + c) / 3
    df["vwap"]   = (tp * v).rolling(20).sum() / v.rolling(20).sum()
    df["vol_ma"] = v.rolling(20).mean()
    df["vol_r"]  = v / df["vol_ma"]
    df["mom5"]   = c.pct_change(5)
    df["mom10"]  = c.pct_change(10)

    # V11 FIX: Removed EMA200 requirement — catches gradual uptrends too.
    # Old definition required EMA50>EMA200 which takes 33 days to confirm,
    # causing system to miss fresh uptrends and take wrong-direction trades.
    df["trend_up"]   = (c > df["ema21"]) & (df["ema21"] > df["ema50"])
    df["trend_down"] = (c < df["ema21"]) & (df["ema21"] < df["ema50"])
    # Strong trend: full EMA alignment including EMA200 (used for scoring bonus)
    df["trend_up_strong"]   = df["trend_up"]  & (df["ema50"] > df["ema200"])
    df["trend_down_strong"] = df["trend_down"] & (df["ema50"] < df["ema200"])
    df["bb_squeeze"] = df["bb_wid"] < df["bb_wid"].rolling(20).mean() * 0.8
    # V8: BB squeeze breakout — price exits squeeze for first time
    prev_sq = df["bb_squeeze"].shift(1).fillna(False)
    df["bb_break_up"]   = (~df["bb_squeeze"]) & prev_sq & (c > df["bb_up"].shift(1))
    df["bb_break_down"] = (~df["bb_squeeze"]) & prev_sq & (c < df["bb_lo"].shift(1))
    df = _supertrend(df)
    return df.dropna()


def _supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    h, l, c = df["high"], df["low"], df["close"]
    atr     = AverageTrueRange(h, l, c, period).average_true_range()
    hl2     = (h + l) / 2
    up      = (hl2 + mult * atr).copy()
    dn      = (hl2 - mult * atr).copy()
    bull    = [True] * len(df)
    for i in range(1, len(df)):
        up.iloc[i] = min(up.iloc[i], up.iloc[i-1]) if c.iloc[i-1] <= up.iloc[i-1] else up.iloc[i]
        dn.iloc[i] = max(dn.iloc[i], dn.iloc[i-1]) if c.iloc[i-1] >= dn.iloc[i-1] else dn.iloc[i]
        if   bull[i-1] and c.iloc[i] < dn.iloc[i]:     bull[i] = False
        elif not bull[i-1] and c.iloc[i] > up.iloc[i]: bull[i] = True
        else:                                            bull[i] = bull[i-1]
    df["st_bull"] = bull
    return df


def get_all_timeframes() -> dict:
    tfs = {}
    for tf in ["1m", "5m", "15m", "1h", "4h"]:
        try:
            tfs[tf] = add_indicators(fetch_klines(tf))
        except Exception as e:
            logger.error(f"Data {tf}: {e}")
    return tfs


def get_price() -> float:
    t = get_client().futures_symbol_ticker(symbol=CFG["SYMBOL"])
    return float(t["price"])


def get_funding_rate() -> float:
    try:
        data = get_client().futures_funding_rate(symbol=CFG["SYMBOL"], limit=1)
        if data:
            return float(data[-1]["fundingRate"])
    except Exception as e:
        logger.warning(f"Funding rate error: {e}")
    return 0.0


def get_session() -> str:
    h = datetime.utcnow().hour
    if  7 <= h <  9:  return "LONDON_OPEN"
    if  9 <= h < 12:  return "LONDON_NY_OVERLAP"
    if 12 <= h < 17:  return "NY_SESSION"
    if 17 <= h < 20:  return "NY_CLOSE"
    if  0 <= h <  5:  return "ASIA"
    return "OFF_HOURS"


# V7: Session quality score (higher = better for trading)
SESSION_SCORES = {
    "LONDON_NY_OVERLAP": 100,
    "NY_SESSION":        90,
    "LONDON_OPEN":       80,
    "NY_CLOSE":          60,
    "ASIA":              50,
    "OFF_HOURS":         40,
}


# ═══════════════════════════════════════════════════════════════════
# REGIME DETECTION
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Regime:
    volatility: str   # LOW | NORMAL | HIGH | EXTREME | CHAOS
    trend:      str   # STRONG_UP | WEAK_UP | NEUTRAL | WEAK_DOWN | STRONG_DOWN
    structure:  str   # TRENDING | RANGING | CHOPPY | BREAKOUT
    score:      int   # 0-100 overall trade quality
    atr_pct:    float # ATR as % of price (for SL/TP scaling)


def detect_regime(tfs: dict) -> Regime:
    if not all(k in tfs for k in ["15m", "1h", "4h"]):
        return Regime("NORMAL", "NEUTRAL", "RANGING", 30, 0.5)

    df15, df1h, df4h = tfs["15m"], tfs["1h"], tfs["4h"]

    atr_now = float(df15["atr"].iloc[-1])
    atr_avg = float(df15["atr"].rolling(50).mean().iloc[-1])
    atr_r   = atr_now / max(atr_avg, 0.001)
    price   = float(df15["close"].iloc[-1])
    atr_pct = atr_now / price * 100

    if   atr_pct > 2.0:   vol = "CHAOS"
    elif atr_r   > 3.0:   vol = "EXTREME"
    elif atr_r   > 1.5:   vol = "HIGH"
    elif atr_r   < 0.7:   vol = "LOW"
    else:                  vol = "NORMAL"

    adx  = float(df1h["adx"].iloc[-1])
    adxp = float(df1h["adx_pos"].iloc[-1])
    adxn = float(df1h["adx_neg"].iloc[-1])
    t4u  = bool(df4h["trend_up"].iloc[-1])
    t4d  = bool(df4h["trend_down"].iloc[-1])
    t1u  = bool(df1h["trend_up"].iloc[-1])
    t1d  = bool(df1h["trend_down"].iloc[-1])

    if   adx > 30 and adxp > adxn and t4u and t1u:    trend = "STRONG_UP"
    elif adx > 20 and adxp > adxn and (t4u or t1u):   trend = "WEAK_UP"
    elif adx > 30 and adxn > adxp and t4d and t1d:    trend = "STRONG_DOWN"
    elif adx > 20 and adxn > adxp and (t4d or t1d):   trend = "WEAK_DOWN"
    else:                                               trend = "NEUTRAL"

    bb_wid = float(df15["bb_wid"].iloc[-1])
    if   adx > 25 and bb_wid > 0.03:                  structure = "TRENDING"
    elif adx < 15 and bb_wid < 0.02:                  structure = "RANGING"
    elif vol in ("CHAOS", "EXTREME"):                  structure = "CHOPPY"
    elif bool(df15["bb_squeeze"].iloc[-1]):            structure = "BREAKOUT"
    else:                                               structure = "RANGING"

    s = 50
    if vol == "LOW":           s += 10
    elif vol == "NORMAL":      s +=  5
    elif vol == "HIGH":        s -= 15
    elif vol == "EXTREME":     s -= 40
    elif vol == "CHAOS":       s -= 50
    if trend in ("STRONG_UP","STRONG_DOWN"):   s += 20
    elif trend in ("WEAK_UP","WEAK_DOWN"):     s += 10
    elif trend == "NEUTRAL":                   s -= 10
    if structure == "TRENDING":   s += 15
    elif structure == "RANGING":  s -= 10
    elif structure == "CHOPPY":   s -= 25
    elif structure == "BREAKOUT": s +=  5

    # V7: weekend penalty
    if datetime.utcnow().weekday() >= 5:
        s -= 5   # V13: reduced weekend regime penalty

    return Regime(vol, trend, structure, max(0, min(100, s)), round(atr_pct, 4))


def detect_anomaly(tfs: dict) -> Tuple[bool, str]:
    """
    V8 FIX: Only block on flash price move >3% in 5 min.
    Volume spikes are NO LONGER a block — they are used as
    directional signal confirmation in the engine (Rule 8).
    """
    df1m = tfs.get("1m")
    if df1m is None or len(df1m) < 5:
        return False, ""
    recent = df1m["close"].tail(5)
    pct    = abs((recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0]) * 100
    if pct > 3.0:
        return True, f"Flash move {pct:.1f}% in 5 min"
    return False, ""


# V7: Multi-timeframe trend alignment check
def trend_aligned(tfs: dict, direction: str) -> Tuple[bool, str]:
    """V11 FIX: Blocks trade if 4H trend contradicts direction.
    Old version required BOTH 4H AND 1H to contradict — too weak.
    New version blocks on 4H alone — prevents trading against major trend."""
    if not all(k in tfs for k in ["1h", "4h"]):
        return True, "Insufficient data — skip conflict check"
    df4h = tfs["4h"]
    df1h = tfs["1h"]
    t4u  = bool(df4h["trend_up"].iloc[-1])
    t4d  = bool(df4h["trend_down"].iloc[-1])
    t1u  = bool(df1h["trend_up"].iloc[-1])
    t1d  = bool(df1h["trend_down"].iloc[-1])
    # Block if 4H trend directly contradicts signal direction
    if direction == "BUY"   and t4d:
        return False, "BUY signal but 4H is bearish"
    if direction == "SHORT" and t4u:
        return False, "SHORT signal but 4H is bullish"
    # Also block if 1H contradicts (belt-and-suspenders for strong trends)
    if direction == "BUY"   and t1d and not t4u:
        return False, "BUY signal but 1H bearish and 4H not bullish"
    if direction == "SHORT" and t1u and not t4d:
        return False, "SHORT signal but 1H bullish and 4H not bearish"
    return True, ""


# ═══════════════════════════════════════════════════════════════════
# NEWS SENTIMENT
# ═══════════════════════════════════════════════════════════════════
_BULL_W = ["surge","rally","bullish","breakout","adoption","gains",
           "all-time high","buy","positive","growth","approved","ETF",
           "institutional","upgrade","record"]
_BEAR_W = ["crash","drop","bearish","ban","hack","fear","selloff",
           "negative","decline","loss","regulatory","crackdown",
           "seized","bankrupt","lawsuit","investigation"]


def get_sentiment() -> float:
    key = CFG["NEWS_KEY"]
    if not key or "YOUR_" in key:
        return 0.0
    scores = []
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything?q=bitcoin"
            "&language=en&sortBy=publishedAt&pageSize=20"
            f"&apiKey={key}", timeout=8)
        if r.status_code == 200:
            for a in r.json().get("articles", [])[:20]:
                txt = (a.get("title","") + " " +
                       a.get("description","")).lower()
                b = sum(1 for w in _BULL_W if w in txt)
                s = sum(1 for w in _BEAR_W if w in txt)
                if b + s > 0:
                    scores.append((b - s) / (b + s))
    except Exception as e:
        logger.warning(f"News error: {e}")
    return round(sum(scores) / len(scores), 3) if scores else 0.0


# ═══════════════════════════════════════════════════════════════════
# V8: CANDLE PATTERN ENGINE
# Detects: Engulfing, Hammer, Shooting Star, Pin Bar, Morning/Evening Star
# ═══════════════════════════════════════════════════════════════════
@dataclass
class CandlePattern:
    name:      str
    direction: str   # BULL | BEAR
    strength:  int   # 1-10


def detect_candle_patterns(df: pd.DataFrame) -> List[CandlePattern]:
    patterns = []
    if len(df) < 3:
        return patterns
    o0,h0,l0,c0 = float(df["open"].iloc[-1]),  float(df["high"].iloc[-1]),  float(df["low"].iloc[-1]),  float(df["close"].iloc[-1])
    o1,h1,l1,c1 = float(df["open"].iloc[-2]),  float(df["high"].iloc[-2]),  float(df["low"].iloc[-2]),  float(df["close"].iloc[-2])
    o2,h2,l2,c2 = float(df["open"].iloc[-3]),  float(df["high"].iloc[-3]),  float(df["low"].iloc[-3]),  float(df["close"].iloc[-3])
    body0,body1  = abs(c0-o0), abs(c1-o1)
    range0,range1= h0-l0, h1-l1
    bull0,bear0  = c0>o0, c0<o0
    bull1,bear1  = c1>o1, c1<o1

    # Bullish Engulfing
    if bear1 and bull0 and o0<=c1 and c0>=o1 and body0>body1*1.1:
        patterns.append(CandlePattern("Bullish Engulfing","BULL",8))
    # Bearish Engulfing
    if bull1 and bear0 and o0>=c1 and c0<=o1 and body0>body1*1.1:
        patterns.append(CandlePattern("Bearish Engulfing","BEAR",8))
    # Hammer
    if range0>0:
        lw = min(o0,c0)-l0; uw = h0-max(o0,c0)
        if body0>0 and lw>=2.0*body0 and uw<=0.1*range0:
            patterns.append(CandlePattern("Hammer","BULL",7))
    # Shooting Star
    if range0>0:
        uw = h0-max(o0,c0); lw = min(o0,c0)-l0
        if body0>0 and uw>=2.0*body0 and lw<=0.1*range0:
            patterns.append(CandlePattern("Shooting Star","BEAR",7))
    # Bullish Pin Bar
    if range0>0 and body0/range0<0.35 and (min(o0,c0)-l0)/range0>=0.6:
        patterns.append(CandlePattern("Bullish Pin Bar","BULL",7))
    # Bearish Pin Bar
    if range0>0 and body0/range0<0.35 and (h0-max(o0,c0))/range0>=0.6:
        patterns.append(CandlePattern("Bearish Pin Bar","BEAR",7))
    # Morning Star
    if bear1 and abs(c1-o1)<abs(c2-o2)*0.5 and bull0 and c0>(o2+c2)/2:
        patterns.append(CandlePattern("Morning Star","BULL",9))
    # Evening Star
    if bull1 and abs(c1-o1)<abs(c2-o2)*0.5 and bear0 and c0<(o2+c2)/2:
        patterns.append(CandlePattern("Evening Star","BEAR",9))
    return patterns


# ═══════════════════════════════════════════════════════════════════
# SIGNAL ENGINE — 12 fixed weighted rules
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    direction: str
    prob:      float
    entry:     float
    sl:        float
    tp1:       float
    tp2:       float
    tp3:       float    # V7: third target
    atr:       float
    leverage:  int
    regime:    str
    factors:   list = field(default_factory=list)
    reason:    str  = ""
    candle_pattern: str = ""   # V8: pattern name if one fired


def _none(r: str = "") -> Signal:
    return Signal("NONE", 0, 0, 0, 0, 0, 0, 0, 1, "", reason=r)


def generate_signal(tfs: dict, sentiment: float, regime: Regime) -> Signal:
    if regime.volatility in ("EXTREME", "CHAOS"):
        return _none(f"Hard block: volatility={regime.volatility}")
    if regime.score < 20:
        return _none(f"Market quality score too low: {regime.score}/100")
    if not all(k in tfs for k in ["5m","15m", "1h", "4h"]):
        return _none("Insufficient timeframe data")

    df5  = tfs["5m"]
    df15 = tfs["15m"]
    df1h = tfs["1h"]
    df4h = tfs["4h"]
    price = float(df15["close"].iloc[-1])
    atr   = float(df15["atr"].iloc[-1])

    bull, bear = [], []

    # Rule 1: 4H trend — weight 15 (strong trend = 20)
    # V13: uses relaxed trend_up (EMA21>EMA50 only) for detection
    # but gives extra weight when full EMA alignment (incl EMA200) confirmed
    if bool(df4h["trend_up"].iloc[-1]):
        w = 20 if bool(df4h["trend_up_strong"].iloc[-1]) else 15
        bull.append((f"4H uptrend{'(strong)' if w==20 else ''}", w))
    if bool(df4h["trend_down"].iloc[-1]):
        w = 20 if bool(df4h["trend_down_strong"].iloc[-1]) else 15
        bear.append((f"4H downtrend{'(strong)' if w==20 else ''}", w))

    # Rule 2: 1H trend — weight 10
    if bool(df1h["trend_up"].iloc[-1]):   bull.append(("1H uptrend",   10))
    if bool(df1h["trend_down"].iloc[-1]): bear.append(("1H downtrend", 10))

    # Rule 3: Supertrend 15m+1h — weight 12
    st15 = bool(df15["st_bull"].iloc[-1])
    st1h = bool(df1h["st_bull"].iloc[-1])
    if     st15 and     st1h: bull.append(("Supertrend bullish 15m+1h", 12))
    if not st15 and not st1h: bear.append(("Supertrend bearish 15m+1h", 12))

    # Rule 4: MACD histogram 1h — weight 8
    mh  = float(df1h["macd_h"].iloc[-1])
    mhp = float(df1h["macd_h"].iloc[-2])
    if mh > 0 and mh > mhp: bull.append(("1H MACD histogram rising", 8))
    if mh < 0 and mh < mhp: bear.append(("1H MACD histogram falling",8))

    # Rule 5: RSI V8 FIX — neutral zone + trending momentum
    rsi15  = float(df15["rsi"].iloc[-1])
    rsi15p = float(df15["rsi"].iloc[-2])
    rsi1h  = float(df1h["rsi"].iloc[-1])
    # Neutral zone (unchanged)
    if 45 < rsi15 < 65 and rsi15 > rsi15p: bull.append(("RSI 15m rising neutral zone", 6))
    if 35 < rsi15 < 55 and rsi15 < rsi15p: bear.append(("RSI 15m falling neutral zone",6))
    # V8 NEW: Trending RSI — scores positively in trending regime
    if regime.structure == "TRENDING":
        if rsi15 >= 60 and rsi15 > rsi15p: bull.append(("RSI 15m bullish momentum", 6))
        if rsi15 <= 40 and rsi15 < rsi15p: bear.append(("RSI 15m bearish momentum", 6))
    if rsi1h > 55: bull.append(("1H RSI bullish", 5))
    if rsi1h < 45: bear.append(("1H RSI bearish", 5))

    # Rule 6: VWAP — weight 7
    vwap = float(df15["vwap"].iloc[-1])
    if price > vwap: bull.append(("Price above VWAP", 7))
    else:            bear.append(("Price below VWAP", 7))

    # Rule 7: EMA stack 15m — weight 8
    e9  = float(df15["ema9"].iloc[-1])
    e21 = float(df15["ema21"].iloc[-1])
    e50 = float(df15["ema50"].iloc[-1])
    ema_stack_bull = (price > e9 > e21 > e50)
    ema_stack_bear = (price < e9 < e21 < e50)
    if ema_stack_bull: bull.append(("EMA stack bullish 15m", 8))
    if ema_stack_bear: bear.append(("EMA stack bearish 15m", 8))

    # Rule 8: Volume V8 FIX — spike is now directional signal, not anomaly block
    vr = float(df15["vol_r"].iloc[-1])
    cp = float(df15["close"].iloc[-2])
    if vr > 1.5:
        if price > cp: bull.append(("Volume spike on bullish candle", 8))
        else:          bear.append(("Volume spike on bearish candle", 8))
    elif vr > 1.0:
        if price > cp: bull.append(("Above-avg volume bullish", 4))
        else:          bear.append(("Above-avg volume bearish", 4))

    # Rule 9: Bollinger Band position — weight 5
    bbp = float(df15["bb_pct"].iloc[-1])
    bm  = float(df15["bb_mid"].iloc[-1])
    if 0.4 < bbp < 0.7 and price > bm: bull.append(("Above BB midline, not OB", 5))
    if 0.3 < bbp < 0.6 and price < bm: bear.append(("Below BB midline, not OS", 5))

    # Rule 10: ADX strength 1h — weight 8
    adx  = float(df1h["adx"].iloc[-1])
    adxp = float(df1h["adx_pos"].iloc[-1])
    adxn = float(df1h["adx_neg"].iloc[-1])
    if adx > 25:
        if adxp > adxn: bull.append(("ADX>25 +DI leading", 8))
        else:           bear.append(("ADX>25 -DI leading", 8))

    # Rule 11: News sentiment — weight 5
    if sentiment >  0.3: bull.append((f"Bullish sentiment {sentiment:.2f}", 5))
    if sentiment < -0.3: bear.append((f"Bearish sentiment {sentiment:.2f}", 5))

    # Rule 12: EMA9 V8 FIX — only fires when Rule 7 (EMA stack) did NOT align
    # Prevents double-counting EMA9 — was inflating scores artificially
    if not ema_stack_bull and not ema_stack_bear:
        if price > e9: bull.append(("Price above EMA9 (standalone)", 5))
        if price < e9: bear.append(("Price below EMA9 (standalone)", 5))

    # Rule 13: V8 NEW — 5m Supertrend (early momentum detection)
    if "st_bull" in df5.columns and len(df5) > 1:
        st5_now  = bool(df5["st_bull"].iloc[-1])
        st5_prev = bool(df5["st_bull"].iloc[-2])
        if st5_now:  bull.append(("5m Supertrend bullish", 7))
        else:        bear.append(("5m Supertrend bearish", 7))
        if st5_now  and not st5_prev: bull.append(("5m Supertrend fresh flip up",   5))
        if not st5_now and st5_prev:  bear.append(("5m Supertrend fresh flip down", 5))

    # Rule 14: V8 NEW — BB Squeeze Breakout explicit signal — weight 10
    if "bb_break_up"   in df15.columns and bool(df15["bb_break_up"].iloc[-1]):
        bull.append(("BB Squeeze Breakout UP", 10))
    if "bb_break_down" in df15.columns and bool(df15["bb_break_down"].iloc[-1]):
        bear.append(("BB Squeeze Breakout DOWN", 10))

    # V8: Candle pattern bonus on 15m
    patterns = detect_candle_patterns(df15)
    pat_name = ""
    for p in patterns:
        if p.direction == "BULL": bull.append((f"Pattern: {p.name}", p.strength)); pat_name = p.name
        else:                     bear.append((f"Pattern: {p.name}", p.strength)); pat_name = p.name

    bull_score = sum(w for _, w in bull)
    bear_score = sum(w for _, w in bear)
    TOTAL      = 130    # V13: adjusted to realistic max score

    # V13: 4H DOMINANT TREND BIAS
    # If 4H trend favours a direction, add bonus to that side's score
    # This prevents short-term 15m noise from overriding the bigger trend
    t4u_now = bool(df4h["trend_up"].iloc[-1])
    t4d_now = bool(df4h["trend_down"].iloc[-1])
    t4u_str = bool(df4h["trend_up_strong"].iloc[-1]) if "trend_up_strong" in df4h.columns else False
    t4d_str = bool(df4h["trend_down_strong"].iloc[-1]) if "trend_down_strong" in df4h.columns else False

    if t4u_str:   bull_score += 15   # strong 4H uptrend → significant BUY bias
    elif t4u_now: bull_score += 8    # moderate 4H uptrend → mild BUY bias
    if t4d_str:   bear_score += 15   # strong 4H downtrend → significant SHORT bias
    elif t4d_now: bear_score += 8    # moderate 4H downtrend → mild SHORT bias

    # V13: If scores within 15% of each other, prefer 4H trend direction
    score_gap = abs(bull_score - bear_score)
    score_max = max(bull_score, bear_score)
    scores_close = score_max > 0 and (score_gap / score_max) < 0.15

    # Log bull vs bear scores every scan for dashboard transparency
    logger.info(f"SCORES | Bull: {bull_score} | Bear: {bear_score} | 4H: {'UP' if t4u_now else 'DOWN' if t4d_now else 'NEUTRAL'} | Gap: {score_gap}")
    kset("last_bull_score",  str(bull_score))
    kset("last_bear_score",  str(bear_score))
    kset("last_4h_trend",    "UP" if t4u_now else "DOWN" if t4d_now else "NEUTRAL")
    kset("last_score_time",  datetime.utcnow().isoformat())

    conflict = min(bull_score, bear_score) * 0.5

    if   bull_score > bear_score:
        direction = "BUY"
        raw = bull_score - conflict
        factors = bull
    elif bear_score > bull_score:
        direction = "SHORT"
        raw = bear_score - conflict
        factors = bear
    else:
        return _none("Signals balanced — no clear direction")

    # V13: If scores very close, override with 4H trend direction
    if scores_close:
        if t4u_now and direction == "SHORT":
            direction = "BUY"
            raw = bull_score - conflict
            factors = bull
            logger.info("SCORE TIE → 4H uptrend bias overrides to BUY")
        elif t4d_now and direction == "BUY":
            direction = "SHORT"
            raw = bear_score - conflict
            factors = bear
            logger.info("SCORE TIE → 4H downtrend bias overrides to SHORT")

    # Trend conflict block (unchanged from V7)
    aligned, conflict_reason = trend_aligned(tfs, direction)
    if not aligned:
        return _none(f"Timeframe conflict: {conflict_reason}")

    # Regime penalty
    penalty = 0
    if   regime.score < 40: penalty = 12
    elif regime.score < 55: penalty = 10
    elif regime.score < 65: penalty =  4
    raw = max(0.0, raw - penalty)

    sess       = get_session()
    sess_score = SESSION_SCORES.get(sess, 50)
    if   sess_score >= 90: raw += 4
    elif sess_score < 50:  raw -= 3   # V13: reduced Asia penalty

    prob = min(95.0, (raw / TOTAL) * 100 * 1.4)

    # V13: Dynamic confidence threshold — escalates after losses, resets on win
    # Replaces blunt paper-mode jump with graduated selectivity
    threshold  = CFG["MIN_WIN_PROB"]   # base: 62%
    day_start  = float(kget("day_start_cap", str(CFG["INITIAL_CAPITAL"])))
    capital    = float(kget("capital",       str(CFG["INITIAL_CAPITAL"])))
    consec_now = int(kget("consecutive_losses", "0"))

    # Escalate threshold based on recent loss streak
    if   consec_now >= 5: threshold = max(threshold, 80.0)
    elif consec_now >= 3: threshold = max(threshold, 75.0)
    elif consec_now >= 2: threshold = max(threshold, 70.0)

    # Daily loss caution still applies
    if day_start > 0 and (day_start - capital) / day_start * 100 >= CFG["CAUTION_LOSS"]:
        threshold = max(threshold, 75.0)

    if prob < threshold:
        s = _none(f"Win prob {prob:.1f}% < required {threshold:.0f}% (consec_loss={consec_now})")
        s.prob = round(prob, 2)
        return s

    # SL/TP
    sl_mult = 1.0
    if regime.volatility == "HIGH":  sl_mult = 1.5
    elif regime.volatility == "LOW": sl_mult = 0.8

    if direction == "BUY":
        sl  = price - sl_mult * atr
        tp1 = price + CFG["TP1_RATIO"] * atr
        tp2 = price + CFG["TP2_RATIO"] * atr
        tp3 = price + CFG["TP3_RATIO"] * atr
    else:
        sl  = price + sl_mult * atr
        tp1 = price - CFG["TP1_RATIO"] * atr
        tp2 = price - CFG["TP2_RATIO"] * atr
        tp3 = price - CFG["TP3_RATIO"] * atr

    max_sl_dist = price * 0.02
    if direction == "BUY"   and (price - sl) > max_sl_dist: sl = price - max_sl_dist
    if direction == "SHORT" and (sl - price) > max_sl_dist: sl = price + max_sl_dist

    # V13: Dynamic leverage — scales with probability AND regime
    # Hard cap: 75x (125x too close to liquidation for volatile BTC)
    strong_trend = regime.trend in ("STRONG_UP", "STRONG_DOWN")
    good_regime  = regime.score >= 60
    if prob >= 85 and strong_trend and good_regime:
        lev = 75   # highest confidence + strong trend
    elif prob >= 85:
        lev = 50   # high confidence but trend not perfectly aligned
    elif prob >= 80:
        lev = 50
    elif prob >= 75:
        lev = 30
    elif prob >= 70:
        lev = 30
    else:
        lev = 20   # base leverage for 62-69% signals
    # Safety cap in high/extreme volatility
    if regime.volatility == "HIGH":    lev = min(lev, 30)
    if regime.volatility == "EXTREME": lev = min(lev, 10)
    lev = min(lev, CFG["MAX_LEVERAGE"])

    logger.info(f"SIGNAL {direction} | Prob {prob:.1f}% | Rules: {len(factors)} | "
                f"Pattern: {pat_name or 'none'} | Regime: {regime.structure} | Session: {sess}")

    return Signal(
        direction=direction, prob=round(prob, 2),
        entry=round(price, 2), sl=round(sl, 2),
        tp1=round(tp1, 2), tp2=round(tp2, 2), tp3=round(tp3, 2),
        atr=round(atr, 2), leverage=lev,
        regime=regime.structure, factors=factors,
        candle_pattern=pat_name,
    )



# ═══════════════════════════════════════════════════════════════════
# SIGNAL LOGGER
# ═══════════════════════════════════════════════════════════════════
def _log_signal(sig: Signal, outcome: str, skip_reason: str = ""):
    try:
        db = _Session()
        db.add(SignalLog(
            direction   = sig.direction,
            prob        = sig.prob,
            outcome     = outcome,
            skip_reason = skip_reason[:200] if skip_reason else None,
            rules_fired = json.dumps([f[0] for f in sig.factors])[:400] if sig.factors else None,
            regime      = sig.regime,
            session     = get_session(),
        ))
        db.commit(); db.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# ORDER EXECUTION V8 — returns (success, fill_price)
# ═══════════════════════════════════════════════════════════════════
def place_order(direction: str, qty: float, leverage: int) -> Tuple[bool, float]:
    """V8: Returns (success, actual_fill_price) for post-fill SL recalculation."""
    try:
        get_client().futures_change_leverage(symbol=CFG["SYMBOL"], leverage=leverage)
        order = get_client().futures_create_order(
            symbol   = CFG["SYMBOL"],
            side     = "BUY" if direction == "BUY" else "SELL",
            type     = "MARKET",
            quantity = qty)
        # Fetch actual fill price
        fill_price = 0.0
        try:
            trades = get_client().futures_account_trades(symbol=CFG["SYMBOL"], limit=1)
            if trades:
                fill_price = float(trades[-1]["price"])
        except Exception:
            pass
        if fill_price <= 0:
            fill_price = float(order.get("avgPrice",0) or order.get("price",0))
        return True, fill_price
    except BinanceAPIException as e:
        logger.error(f"Place order failed: {e}")
        alert_log("CRIT", f"Order placement failed: {str(e)[:200]}")
        return False, 0.0


def close_order(direction: str, qty: float, reduce_only: bool = True) -> bool:
    try:
        get_client().futures_create_order(
            symbol     = CFG["SYMBOL"],
            side       = "SELL" if direction == "BUY" else "BUY",
            type       = "MARKET",
            quantity   = qty,
            reduceOnly = reduce_only)
        return True
    except BinanceAPIException as e:
        logger.error(f"Close order failed: {e}")
        alert_log("WARN", f"Close order failed: {str(e)[:200]}")
        return False


# V7: Partial close for multi-TP exits
def partial_close(direction: str, qty: float) -> bool:
    """Close a partial position at TP1 or TP2."""
    return close_order(direction, qty, reduce_only=True)


# ═══════════════════════════════════════════════════════════════════
# STARTUP RECONCILIATION — V7: fetch actual Binance PnL
# ═══════════════════════════════════════════════════════════════════
def reconcile_on_startup():
    try:
        positions = get_client().futures_position_information(
            symbol=CFG["SYMBOL"])
        for p in positions:
            amt = float(p["positionAmt"])
            if abs(amt) < 0.001:
                continue
            direction   = "BUY" if amt > 0 else "SHORT"
            entry_price = float(p["entryPrice"])
            unreal_pnl  = float(p.get("unrealizedProfit", 0))
            leverage    = int(p.get("leverage", 1))

            logger.warning(
                f"⚠️  Orphaned position found: "
                f"{direction} {abs(amt):.3f} BTC @ ${entry_price:,.2f} "
                f"| Unrealized PnL: ${unreal_pnl:+.2f}")

            db = _Session()
            existing = db.query(Trade).filter(
                Trade.status   == "OPEN",
                Trade.is_paper == False).first()

            if not existing:
                # Reconstruct SL/TP from entry
                atr_est = entry_price * 0.01
                if direction == "BUY":
                    sl  = entry_price - atr_est
                    tp1 = entry_price + CFG["TP1_RATIO"] * atr_est
                    tp2 = entry_price + CFG["TP2_RATIO"] * atr_est
                    tp3 = entry_price + CFG["TP3_RATIO"] * atr_est
                else:
                    sl  = entry_price + atr_est
                    tp1 = entry_price - CFG["TP1_RATIO"] * atr_est
                    tp2 = entry_price - CFG["TP2_RATIO"] * atr_est
                    tp3 = entry_price - CFG["TP3_RATIO"] * atr_est

                db.add(Trade(
                    trade_id     = "RCVR-" + str(uuid.uuid4())[:6].upper(),
                    direction    = direction,
                    is_paper     = False,
                    entry_price  = entry_price,
                    stop_loss    = sl,
                    take_profit1 = tp1,
                    take_profit2 = tp2,
                    take_profit3 = tp3,
                    quantity     = abs(amt),
                    qty_remaining= abs(amt),
                    leverage     = leverage,
                    win_prob     = 0.0,
                    status       = "OPEN",
                    regime       = "RECOVERED",
                ))
                db.commit()
                alert_log("WARN",
                    f"Position recovered on restart: {direction} "
                    f"{abs(amt):.3f} BTC @ ${entry_price:,.2f}\n"
                    f"Unrealized PnL: ${unreal_pnl:+.2f}\n"
                    f"Estimated SL/TP set. Monitoring active.")
            else:
                # Sync qty_remaining if needed
                if existing.qty_remaining is None:
                    existing.qty_remaining = existing.quantity
                    db.commit()
                logger.info("Open trade in DB matches Binance — OK")
            db.close()
    except Exception as e:
        logger.error(f"Reconcile error: {e}")


# ═══════════════════════════════════════════════════════════════════
# SELF-HEALING ENGINE — V7: 10 error types, async non-blocking
# ═══════════════════════════════════════════════════════════════════
_FIX_MAP = {
    "BinanceAPIException":    ("reset_binance_client",  "fix_binance_client"),
    "ConnectionError":        ("reset_binance_client",  "fix_binance_client"),
    "RemoteDisconnected":     ("reset_binance_client",  "fix_binance_client"),
    "ChunkedEncodingError":   ("reset_binance_client",  "fix_binance_client"),
    "Timeout":                ("reset_binance_client",  "fix_binance_client"),
    "InvalidSignature":       ("reset_binance_client",  "fix_binance_client"),
    "ReadTimeout":            ("reset_binance_client",  "fix_binance_client"),
    "OperationalError":       ("reset_db_session",       "fix_db_session"),
    "DatabaseError":          ("reset_db_session",       "fix_db_session"),
    "IntegrityError":         ("reset_db_session",       "fix_db_session"),
    "StatementError":         ("reset_db_session",       "fix_db_session"),
    "NoneType":               ("skip_bad_data",          "fix_skip_tick"),
    "KeyError":               ("skip_bad_data",          "fix_skip_tick"),
    "IndexError":             ("skip_bad_data",          "fix_skip_tick"),
    "ValueError":             ("skip_bad_data",          "fix_skip_tick"),
    "ZeroDivisionError":      ("skip_bad_data",          "fix_skip_tick"),
    "AttributeError":         ("skip_bad_data",          "fix_skip_tick"),   # V7 new
    "TypeError":              ("skip_bad_data",          "fix_skip_tick"),   # V7 new
    "DataFrame":              ("refresh_market_data",    "fix_market_data"),
    "position too small":     ("adjust_position_size",   "fix_position_size"),
    "SL distance is zero":    ("adjust_position_size",   "fix_position_size"),
    "futures_klines":         ("reset_binance_client",   "fix_binance_client"),
    "futures_create_order":   ("reset_binance_client",   "fix_binance_client"),  # V7 new
    "RecursionError":         ("skip_bad_data",          "fix_skip_tick"),       # V7 new
}


def _error_hash(err: Exception) -> str:
    sig = f"{type(err).__name__}:{str(err)[:120]}"
    return hashlib.md5(sig.encode()).hexdigest()[:12]


def fix_binance_client() -> bool:
    try:
        reset_client()
        time.sleep(3)
        get_client().ping()
        logger.info("HEAL: Binance client reconnected successfully")
        return True
    except Exception as e:
        logger.error(f"HEAL: Binance client fix failed: {e}")
        return False


def fix_db_session() -> bool:
    try:
        global _engine, _Session
        _engine.dispose()
        _engine   = create_engine(_DB_URL, connect_args={"check_same_thread": False})
        _Session  = sessionmaker(bind=_engine)
        Base.metadata.create_all(_engine)
        db = _Session()
        db.query(KV).first()
        db.close()
        logger.info("HEAL: Database session reset successfully")
        return True
    except Exception as e:
        logger.error(f"HEAL: DB session fix failed: {e}")
        return False


def fix_skip_tick() -> bool:
    logger.info("HEAL: Skipping bad tick (data/calc error)")
    return True


def fix_market_data() -> bool:
    logger.info("HEAL: Market data will be refreshed next tick")
    return True


def fix_position_size() -> bool:
    logger.info("HEAL: Position size error — will recalculate next tick")
    return True


def _run_fix(fix_fn_name: str) -> bool:
    fix_map = {
        "fix_binance_client":  fix_binance_client,
        "fix_db_session":      fix_db_session,
        "fix_skip_tick":       fix_skip_tick,
        "fix_market_data":     fix_market_data,
        "fix_position_size":   fix_position_size,
    }
    fn = fix_map.get(fix_fn_name)
    return fn() if fn else False


def _verify_system() -> Tuple[bool, str]:
    try:
        db = _Session()
        db.query(KV).first()
        db.close()
    except Exception as e:
        return False, f"DB check failed: {e}"
    try:
        get_client().ping()
    except Exception as e:
        return False, f"Binance ping failed: {e}"
    try:
        tf = fetch_klines("15m", limit=10)
        if len(tf) < 5:
            return False, "Market data returned too few rows"
    except Exception as e:
        return False, f"Market data check failed: {e}"
    return True, "All systems verified OK"


async def self_heal(err: Exception, context: str = "") -> bool:
    if not CFG["SELF_HEAL"]:
        return False

    err_type = type(err).__name__
    err_msg  = str(err)
    ehash    = _error_hash(err)
    attempts = int(kget("heal_attempts", "0"))

    logger.warning(f"BUG DETECTED [{err_type}]: {err_msg[:150]}")

    if attempts >= CFG["MAX_HEAL_ATTEMPTS"] and kget("last_error_hash") == ehash:
        msg = (f"Self-healer exhausted {attempts} attempts for "
               f"{err_type}. Manual review required.")
        logger.error(f"HEAL: {msg}")
        kset("manual_stop", "true")
        kset("stop_reason", msg)
        alert_log("CRIT", msg)
        return False

    was_live = kget("mode") == "LIVE" and kget("manual_stop") != "true"
    if was_live:
        kset("heal_paused", "true")
        kset("mode",        "PAPER")
        kset("stop_reason", f"Self-healing: {err_type}")
        logger.info("HEAL: Switched to PAPER while healing")

    fix_fn_name = "fix_skip_tick"
    fix_desc    = "skip_bad_data"
    for pattern, (desc, fn) in _FIX_MAP.items():
        if pattern.lower() in err_type.lower() or pattern.lower() in err_msg.lower():
            fix_fn_name = fn
            fix_desc    = desc
            break

    logger.info(f"HEAL: Applying fix '{fix_desc}' for {err_type}")

    attempts += 1
    kset("heal_attempts",   str(attempts))
    kset("last_error_hash", ehash)
    fix_ok = _run_fix(fix_fn_name)

    await asyncio.sleep(5)
    if fix_ok:
        verified, verify_msg = _verify_system()
    else:
        verified, verify_msg = False, "Fix function returned False"

    try:
        db = _Session()
        existing = db.query(HealLog).filter(HealLog.error_hash == ehash).first()
        if existing:
            existing.attempts    += 1
            existing.fix_success  = verified
        else:
            db.add(HealLog(
                error_hash  = ehash,
                error_type  = err_type,
                error_msg   = err_msg[:400],
                fix_applied = fix_desc,
                fix_success = verified,
                attempts    = attempts,
            ))
        db.commit()
        db.close()
    except Exception:
        pass

    if verified:
        kset("heal_attempts", "0")
        kset("last_error_hash", "")
        if was_live and kget("heal_paused") == "true":
            kset("mode",        "LIVE")
            kset("stop_reason", "")
            kset("heal_paused", "false")
            logger.info(f"HEAL: ✓ Fix verified — {verify_msg}. Resuming LIVE trading.")
            alert_log("HEAL",
                f"✓ Self-heal success: fixed {err_type} with '{fix_desc}'. "
                f"Verified: {verify_msg}. Live trading resumed.")
        else:
            kset("heal_paused", "false")
        return True
    else:
        logger.error(f"HEAL: Fix not verified — {verify_msg}. Attempt {attempts}/{CFG['MAX_HEAL_ATTEMPTS']}")
        alert_log("WARN",
            f"Self-heal attempt {attempts}/{CFG['MAX_HEAL_ATTEMPTS']} for {err_type}: "
            f"fix '{fix_desc}' applied but verify failed: {verify_msg}")
        if attempts >= CFG["MAX_HEAL_ATTEMPTS"]:
            msg = f"Self-healer failed after {attempts} attempts for {err_type}. Manual review."
            kset("manual_stop", "true")
            kset("stop_reason", msg)
            kset("heal_paused", "false")
            alert_log("CRIT", msg)
        return False


# ═══════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════
def graceful_shutdown(signum=None, frame=None):
    logger.info("Shutdown signal received — safe closing…")
    try:
        db = _Session()
        t  = db.query(Trade).filter(
            Trade.status   == "OPEN",
            Trade.is_paper == False).first()
        db.close()
        if t:
            logger.info(f"Closing {t.trade_id} before shutdown")
            close_order(t.direction, t.qty_remaining or t.quantity)
            tg("🔴 System shutting down — open position closed safely.", "WARN")
        else:
            tg("🔴 System shutting down — no open positions.", "INFO")
    except Exception as e:
        logger.error(f"Shutdown error: {e}")
    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════
# CORE TRADING ENGINE — V7: Multi-TP, partial exits, enhanced guards
# ═══════════════════════════════════════════════════════════════════
class TradingEngine:

    def __init__(self):
        self._sentiment    = 0.0
        self._sentiment_ts = datetime.utcnow() - timedelta(hours=2)
        self._last_ist_date = today_ist()     # V8: IST-based daily reset

    async def run(self):
        logger.info(f"▶ Trading engine | Fast:{CFG['SCAN_FAST_S']}s no-trade | Slow:{CFG['SCAN_SLOW_S']}s managing")
        while True:
            try:
                kset("last_heartbeat", datetime.utcnow().isoformat())
                # V13: Track scan count per hour for dashboard health
                now_h = datetime.utcnow().strftime("%Y-%m-%dT%H")
                if kget("scan_hour_start") != now_h:
                    kset("scan_hour_start", now_h)
                    kset("scan_count_hour", "0")
                kset("scan_count_hour", str(int(kget("scan_count_hour","0")) + 1))
                # V13: Binance ping health check every 5 minutes
                last_ping = kget("last_binance_ping", "")
                ping_stale = True
                if last_ping:
                    try:
                        ping_stale = (datetime.utcnow() - datetime.fromisoformat(last_ping)).total_seconds() > 300
                    except Exception:
                        pass
                if ping_stale:
                    try:
                        get_client().ping()
                        kset("last_binance_ping", datetime.utcnow().isoformat())
                    except Exception as e:
                        kset("last_binance_ping", f"ERROR:{str(e)[:50]}")
                # V8: IST daily reset
                curr_ist = today_ist()
                if curr_ist != self._last_ist_date:
                    self._daily_reset()
                    self._last_ist_date = curr_ist
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}")
                alert_log("WARN", f"Engine tick error: {str(e)[:200]}")
                await self_heal(e, "trading_engine_tick")
                await asyncio.sleep(CFG["SCAN_SLOW_S"])
                continue
            # V8: Adaptive scan — fast when no open trade
            db = _Session()
            has_open = db.query(Trade).filter(Trade.status == "OPEN").first() is not None
            db.close()
            await asyncio.sleep(CFG["SCAN_FAST_S"] if not has_open else CFG["SCAN_SLOW_S"])

    # ── MAIN TICK ────────────────────────────────────────────────
    async def _tick(self):
        if kget("manual_stop") == "true":
            return
        if kget("heal_paused") == "true":
            logger.info("Tick skipped — self-healer active")
            return

        mode      = kget("mode", "LIVE")
        is_paper  = (mode == "PAPER")
        capital   = float(kget("capital",      str(CFG["INITIAL_CAPITAL"])))
        day_start = float(kget("day_start_cap", str(capital)))
        peak      = float(kget("peak_capital",  str(capital)))

        # ── DAILY LOSS CIRCUIT BREAKER ──────────────────────────
        daily_loss = (day_start - capital) / day_start * 100 if day_start > 0 else 0.0
        if daily_loss >= CFG["MAX_DAILY_LOSS"] and mode == "LIVE":
            kset("mode",        "PAPER")
            kset("stop_reason", f"Daily loss {daily_loss:.1f}% >= {CFG['MAX_DAILY_LOSS']}%")
            kset("paper_start", datetime.utcnow().isoformat())
            kset("paper_pnl",   "0.0")
            kset("paper_wins",  "0")
            logger.warning(f"Daily loss {daily_loss:.1f}% → PAPER TRADING")
            tg(f"⚠️ Daily loss {daily_loss:.1f}% hit.\n"
               f"Switching to PAPER TRADING.\n"
               f"Auto-resumes after {CFG['PAPER_HOURS']}h profitable "
               f"+ {CFG['PAPER_MIN_TRADES']} paper wins.", "WARN")
            return

        # ── WEEKLY LOSS LIMIT ────────────────────────────────────
        week_cap  = float(kget("week_start_cap", str(capital)))
        week_loss = (week_cap - capital) / week_cap * 100 if week_cap > 0 else 0.0
        if week_loss >= CFG["WEEKLY_LOSS_LIMIT"] and mode == "LIVE":
            # V7: close any open position before stopping
            self._emergency_close_all("Weekly loss limit hit")
            kset("manual_stop", "true")
            kset("stop_reason",
                 f"Weekly loss {week_loss:.1f}% >= {CFG['WEEKLY_LOSS_LIMIT']}%. "
                 f"Manual review required.")
            alert_log("CRIT",
                f"Weekly loss limit {week_loss:.1f}% triggered. "
                f"Capital ${capital:.2f} vs week-start ${week_cap:.2f}")
            return

        # ── CAPITAL FLOOR (30% below peak) ──────────────────────
        if peak > 0 and capital < peak * 0.70:
            if kget("manual_stop") != "true":
                self._emergency_close_all("Capital floor breached")
                kset("manual_stop", "true")
                kset("stop_reason",
                     f"Capital ${capital:.2f} is 30% below peak ${peak:.2f}. "
                     f"Manual review required.")
                alert_log("CRIT",
                    f"Capital floor breached. Capital=${capital:.2f} Peak=${peak:.2f}")
            return

        # ── PAPER RECOVERY CHECK ─────────────────────────────────
        if mode == "PAPER":
            await self._check_recovery()
            is_paper = True

        # ── CONSECUTIVE LOSS GUARD ───────────────────────────────
        consec = int(kget("consecutive_losses", "0"))
        if consec >= CFG["MAX_CONSEC_LOSS"] and not is_paper:
            kset("mode",        "PAPER")
            kset("stop_reason", f"{consec} consecutive losses — paper trading")
            kset("paper_start", datetime.utcnow().isoformat())
            kset("paper_pnl",   "0.0")
            kset("paper_wins",  "0")
            kset("consecutive_losses", "0")
            logger.warning(f"{consec} consecutive losses → PAPER TRADING")
            tg(f"⚠️ {consec} consecutive losses.\n"
               f"Switched to paper trading.\n"
               f"Auto-resumes after {CFG['PAPER_HOURS']}h profitable "
               f"+ {CFG['PAPER_MIN_TRADES']} paper wins.", "WARN")
            return

        # V9: No daily trade cap — trades freely while drawdown/loss guards are clear

        # V9: Loss cooldown removed — 68% gate + 12-rule scoring + trend alignment
        #     already filter poor post-loss setups without blocking genuine signals

        # ── SENTIMENT REFRESH (every 30 min) ────────────────────
        if (datetime.utcnow() - self._sentiment_ts).total_seconds() > 1800:
            self._sentiment    = get_sentiment()
            self._sentiment_ts = datetime.utcnow()

        # ── MARKET DATA ──────────────────────────────────────────
        tfs = get_all_timeframes()
        if not tfs or "15m" not in tfs:
            return

        # ── REGIME + ANOMALY ─────────────────────────────────────
        regime  = detect_regime(tfs)
        anomaly, reason = detect_anomaly(tfs)
        if anomaly:
            logger.warning(f"Anomaly: {reason} — skip tick")
            return

        price = float(tfs["15m"]["close"].iloc[-1])

        # ── MANAGE EXISTING OPEN TRADE ───────────────────────────
        db     = _Session()
        open_t = db.query(Trade).filter(
            Trade.status   == "OPEN",
            Trade.is_paper == is_paper).first()
        if open_t:
            await self._manage_trade(db, open_t, price, tfs, capital, is_paper)
            db.close()
            return
        db.close()

        # ── SIGNAL ───────────────────────────────────────────────
        signal = generate_signal(tfs, self._sentiment, regime)
        if signal.direction == "NONE":
            logger.debug(f"No signal: {signal.reason}")
            kset("last_reject_reason", signal.reason[:100] if signal.reason else "No signal")
            return
        # V13: Track last signal found
        kset("last_signal_prob", str(signal.prob))
        kset("last_signal_dir",  signal.direction)
        kset("last_signal_time", datetime.utcnow().isoformat())

        # ── SPREAD + FUNDING FILTERS ─────────────────────────────
        sp_ok, sp_pct = spread_ok()
        if not sp_ok:
            _log_signal(signal, "SKIPPED_FILTER", "Spread too wide"); kset("last_reject_reason", "Spread too wide")
            return

        # V8 FIX: Funding rate thresholds raised — 0.001 not 0.0003
        # At 0.0003, positive funding is actually a SHORT signal, not a reason to skip
        fr = get_funding_rate()
        if signal.direction == "BUY"   and fr < -0.001:
            logger.info(f"SKIP funding {fr:.5f} bad for BUY")
            _log_signal(signal, "SKIPPED_FILTER", f"Funding {fr:.5f} bad for BUY")
            return
        if signal.direction == "SHORT" and fr >  0.001:
            logger.info(f"SKIP funding {fr:.5f} bad for SHORT")
            _log_signal(signal, "SKIPPED_FILTER", f"Funding {fr:.5f} bad for SHORT")
            return

        # ── POSITION SIZE — V12 Dynamic Risk Engine ──────────────
        adx_now = float(tfs["1h"]["adx"].iloc[-1]) if "1h" in tfs else 20.0
        sz = position_size(capital, signal.entry, signal.sl, signal.leverage,
                           prob=signal.prob,
                           regime_score=regime.score,
                           adx=adx_now)
        if sz["error"] or sz["qty"] <= 0:
            logger.warning(f"Sizing: {sz.get('error')}")
            _log_signal(signal, "SKIPPED_FILTER", f"Sizing: {sz.get('error')}")
            return

        qty = sz["qty"]
        logger.info(f"Position: {qty} BTC | Risk: {sz.get('risk_pct',3)}% | Margin: ${sz['margin']}")

        # ── FEE + TAX + EV GATE ──────────────────────────────────
        viable = check_viable(
            signal.direction, signal.entry, signal.sl,
            signal.tp1, signal.tp2, signal.tp3,
            qty, signal.prob)
        if not viable.viable:
            _log_signal(signal, "SKIPPED_FEES", viable.reason); kset("last_reject_reason", f"Fee gate: {viable.reason[:60]}")
            return

        _log_signal(signal, "TAKEN")
        await self._open_trade(signal, qty, capital, is_paper,
                                regime.structure, sp_pct, fr)

    # ── OPEN TRADE ───────────────────────────────────────────────
    async def _open_trade(self, signal, qty, capital,
                           is_paper, regime_str, spread, fr):
        tid  = "T-" + str(uuid.uuid4())[:8].upper()
        sess = get_session()
        fill_price = signal.entry  # default for paper

        if not is_paper:
            ok, fp = place_order(signal.direction, qty, signal.leverage)
            if not ok:
                return
            # V8: Post-fill SL/TP recalculation using actual fill price
            if fp > 0 and abs(fp - signal.entry) / signal.entry > 0.0005:
                logger.info(f"Post-fill adj: signal ${signal.entry:.2f} → fill ${fp:.2f}")
                diff       = fp - signal.entry
                signal.sl  = round(signal.sl  + diff, 2)
                signal.tp1 = round(signal.tp1 + diff, 2)
                signal.tp2 = round(signal.tp2 + diff, 2)
                signal.tp3 = round(signal.tp3 + diff, 2)
                fill_price = fp

        sess = get_session()
        db   = _Session()
        db.add(Trade(
            trade_id      = tid,
            direction     = signal.direction,
            is_paper      = is_paper,
            entry_price   = signal.entry,
            fill_price    = fill_price,
            stop_loss     = signal.sl,
            take_profit1  = signal.tp1,
            take_profit2  = signal.tp2,
            take_profit3  = signal.tp3,
            quantity      = qty,
            qty_remaining = qty,
            leverage      = signal.leverage,
            win_prob      = signal.prob,
            status        = "OPEN",
            regime        = regime_str,
            spread        = spread,
            funding_rate  = fr,
            session       = sess,
            tp1_hit       = False,
            tp2_hit       = False,
            partial_pnl   = 0.0,
            candle_pattern= signal.candle_pattern or None,
            signal_rules  = json.dumps([f[0] for f in signal.factors])[:400] if signal.factors else None,
        ))
        db.commit()
        db.close()

        kset("day_trade_count", int(kget("day_trade_count","0")) + 1)

        mode_str = "PAPER" if is_paper else "LIVE"
        logger.info(
            f"TRADE OPENED [{tid}] {mode_str} | {signal.direction} "
            f"@ ${signal.entry:,.2f} | SL ${signal.sl:,.2f} "
            f"| TP1 ${signal.tp1:,.2f} TP2 ${signal.tp2:,.2f} TP3 ${signal.tp3:,.2f} "
            f"| Prob {signal.prob}% | Lev {signal.leverage}x")

        tg(f"{'📄' if is_paper else '💰'} TRADE OPENED\n"
           f"Direction: *{signal.direction}*\n"
           f"Entry: ${signal.entry:,.2f}\n"
           f"Stop Loss: ${signal.sl:,.2f}\n"
           f"TP1: ${signal.tp1:,.2f} (15% lock) | Runner: ${signal.tp3:,.2f} (85% adaptive trail)\n"
           f"Qty: {qty} BTC | Leverage: {signal.leverage}×\n"
           f"Win Probability: {signal.prob}%\n"
           f"Regime: {regime_str} | Session: {sess}", "TRADE")

    # ── MANAGE OPEN TRADE — V7: Multi-TP partial exits + trailing SL ─
    async def _manage_trade(self, db, trade, price, tfs,
                             capital, is_paper):
        atr = float(tfs["15m"]["atr"].iloc[-1]) if "15m" in tfs else 0

        # Force close stale trade
        if trade.entry_time:
            age_h = (datetime.utcnow() -
                     trade.entry_time).total_seconds() / 3600
            if age_h >= CFG["MAX_TRADE_AGE_H"]:
                alert_log("WARN",
                    f"Force-closing stale trade {trade.trade_id} after {age_h:.1f}h")
                await self._close_trade(db, trade, price,
                                        "MAX_AGE_FORCE_CLOSE", capital, is_paper)
                return

        qty_remaining = trade.qty_remaining or trade.quantity

        # ── V7: PARTIAL EXIT AT TP1 ──────────────────────────────
        if not trade.tp1_hit:
            tp1_triggered = (
                (trade.direction == "BUY"   and price >= trade.take_profit1) or
                (trade.direction == "SHORT" and price <= trade.take_profit1)
            )
            if tp1_triggered:
                # V13: TP1 = 15% of position — only if qty meets Binance minimum
                tp1_qty = round(trade.quantity * (CFG["TP1_PCT"] / 100), 3)
                tp1_qty = min(tp1_qty, qty_remaining)
                if tp1_qty >= 0.001:
                    # Partial close at TP1
                    if not is_paper:
                        partial_close(trade.direction, tp1_qty)
                    hours = max(0.1, (datetime.utcnow() - trade.entry_time
                                      ).total_seconds() / 3600 if trade.entry_time else 1.0)
                    pnl = actual_pnl(trade.direction, trade.entry_price,
                                     trade.take_profit1, tp1_qty, hours)
                    trade.tp1_hit      = True
                    trade.qty_remaining= round(qty_remaining - tp1_qty, 3)
                    trade.partial_pnl  = round((trade.partial_pnl or 0) + pnl["net_for_trading"], 6)
                    if not is_paper:
                        cap_new = capital + pnl["net_for_trading"]
                        kset("capital", round(cap_new, 4))
                        if cap_new > float(kget("peak_capital", str(cap_new))):
                            kset("peak_capital", round(cap_new, 4))
                        self._record_tax(pnl, trade.trade_id)
                    logger.info(
                        f"TRADE PARTIAL TP1 [{trade.trade_id}] "
                        f"Closed {tp1_qty} BTC @ ${trade.take_profit1:,.2f} "
                        f"| Net: ${pnl['net_for_trading']:+.4f} "
                        f"| Remaining: {trade.qty_remaining} BTC")
                    tg(f"{'📄' if is_paper else '🎯'} PARTIAL TP1 HIT\n"
                       f"{trade.direction} — Closed 15% (fee lock)\n"
                       f"@ ${trade.take_profit1:,.2f}\n"
                       f"Net banked: ${pnl['net_for_trading']:+.4f}\n"
                       f"Runner: {trade.qty_remaining} BTC riding trend", "PARTIAL")
                else:
                    # Position too small to partial close — just mark TP1 hit
                    trade.tp1_hit = True
                    logger.info(f"TP1 price hit [{trade.trade_id}] — qty too small to partial, moving SL only")

                # Always move SL to breakeven after TP1 price hit
                if trade.direction == "BUY":
                    be = trade.entry_price * 1.0005
                    if be > trade.stop_loss:
                        trade.stop_loss = round(be, 2)
                else:
                    be = trade.entry_price * 0.9995
                    if be < trade.stop_loss:
                        trade.stop_loss = round(be, 2)
                db.commit()
                return

        # ── V13: NO FIXED TP2 — 85% RUNNER WITH ADAPTIVE ATR TRAIL ──
        # After TP1, remaining position rides trend with adaptive trailing stop.
        # Trail width adjusts based on trend strength: strong=wider, weak=tighter.
        qty_remaining = trade.qty_remaining or trade.quantity

        # ── ADAPTIVE ATR TRAILING SL ─────────────────────────────
        # Determine trend strength for trail multiplier
        adx_val = atr  # fallback
        try:
            if "1h" in tfs:
                adx_val = float(tfs["1h"]["adx"].iloc[-1])
        except Exception:
            pass

        # Trail multiplier: strong trend = wider trail (let winners run)
        if adx_val >= 30:    trail_mult = 1.5   # strong trend — wide trail
        elif adx_val >= 20:  trail_mult = 1.0   # normal trend
        else:                trail_mult = 0.6   # weak trend — tight trail

        if trade.direction == "BUY":
            # Adaptive ATR trail
            if atr > 0:
                new_sl = price - trail_mult * atr
                if new_sl > trade.stop_loss:
                    trade.stop_loss = round(new_sl, 2)
            hit_sl  = price <= trade.stop_loss
            hit_tp3 = price >= trade.take_profit3
        else:
            if atr > 0:
                new_sl = price + trail_mult * atr
                if new_sl < trade.stop_loss:
                    trade.stop_loss = round(new_sl, 2)
            if atr > 0:
                new_sl = price + 0.8 * atr
                if new_sl < trade.stop_loss:
                    trade.stop_loss = round(new_sl, 2)
            hit_sl  = price >= trade.stop_loss
            hit_tp3 = price <= trade.take_profit3

        db.commit()

        # Decide exit
        exit_p = exit_r = None
        if   hit_sl:  exit_p, exit_r = trade.stop_loss,    "STOP_LOSS"
        elif hit_tp3: exit_p, exit_r = trade.take_profit3, "TP3"

        if exit_r:
            await self._close_trade(db, trade, exit_p,
                                    exit_r, capital, is_paper)

    # ── CLOSE TRADE (remaining position) ────────────────────────
    async def _close_trade(self, db, trade, exit_price,
                            reason, capital, is_paper):
        qty_close = trade.qty_remaining or trade.quantity
        hours = max(0.1,
            (datetime.utcnow() - trade.entry_time).total_seconds() / 3600
            if trade.entry_time else 4.0)

        pnl = actual_pnl(trade.direction, trade.entry_price,
                          exit_price, qty_close, hours)

        # Add partial PnL already banked
        total_net = pnl["net_for_trading"] + (trade.partial_pnl or 0.0)

        trade.exit_price      = exit_price
        trade.exit_time       = datetime.utcnow()
        trade.exit_reason     = reason
        trade.gross_pnl       = pnl["gross"]
        trade.fees_total      = pnl["fees"]
        trade.tax_liability   = pnl["tax_liability"]
        trade.net_for_trading = round(total_net, 6)
        trade.qty_remaining   = 0
        trade.status          = ("WIN"  if total_net > 0 else
                                  "LOSS" if total_net < 0 else
                                  "BREAKEVEN")
        db.commit()

        if not is_paper:
            close_order(trade.direction, qty_close)

            new_cap = float(kget("capital", str(capital))) + pnl["net_for_trading"]
            kset("capital", round(new_cap, 4))
            if new_cap > float(kget("peak_capital", str(new_cap))):
                kset("peak_capital", round(new_cap, 4))

            total_tax = float(kget("tax_liability_total", "0.0"))
            total_tax += pnl["tax_liability"]
            kset("tax_liability_total", round(total_tax, 4))

            self._record_tax(pnl, trade.trade_id)

            # Update stats
            total  = int(kget("total_trades","0")) + 1
            wins   = int(kget("wins","0"))
            losses = int(kget("losses","0"))
            consec = int(kget("consecutive_losses","0"))

            if trade.status == "WIN":
                wins  += 1
                kset("consecutive_losses", "0")
                kset("total_wins_amt",
                     str(round(float(kget("total_wins_amt","0")) + total_net, 4)))
            elif trade.status == "LOSS":
                losses += 1
                consec += 1
                kset("consecutive_losses", str(consec))
                kset("total_losses_amt",
                     str(round(float(kget("total_losses_amt","0")) + abs(total_net), 4)))

            kset("total_trades", str(total))
            kset("wins",         str(wins))
            kset("losses",       str(losses))
            kset("win_rate",
                 str(round(wins / total * 100, 1)) if total > 0 else "0.0")
            kset("total_gross",
                 str(round(float(kget("total_gross","0")) + pnl["gross"], 4)))
            kset("total_fees",
                 str(round(float(kget("total_fees","0")) + pnl["fees"], 4)))

            logger.info(
                f"TRADE CLOSED [{trade.trade_id}] {trade.status} "
                f"| Total Net: ${total_net:+.4f} "
                f"| Fees: ${pnl['fees']:.4f} "
                f"| Tax liability: ${pnl['tax_liability']:.4f} "
                f"| Capital: ${new_cap:,.2f}")
        else:
            pp = float(kget("paper_pnl", "0.0")) + total_net
            kset("paper_pnl", str(round(pp, 4)))
            if trade.status == "WIN":
                pw = int(kget("paper_wins", "0")) + 1
                kset("paper_wins", str(pw))

        level = "WIN" if trade.status == "WIN" else "LOSS"
        partial_info = (f"\nPartial banked: ${trade.partial_pnl:+.4f}" 
                        if (trade.partial_pnl or 0) != 0 else "")
        tg(f"{'📄' if is_paper else '💰'} TRADE CLOSED — *{trade.status}*\n"
           f"{trade.direction} "
           f"${trade.entry_price:,.2f} → ${exit_price:,.2f}\n"
           f"Gross: ${pnl['gross']:+.4f} | Fees: ${pnl['fees']:.4f}\n"
           f"*Total Net: ${total_net:+.4f}*{partial_info}\n"
           f"Tax liability: ${pnl['tax_liability']:.4f}\n"
           f"Reason: {reason}", level)

    def _record_tax(self, pnl: dict, trade_id: str):
        """Record tax liability entry."""
        if pnl["tax_liability"] > 0:
            try:
                tdb = _Session()
                tdb.add(TaxLedger(
                    trade_id     = trade_id,
                    trade_date   = datetime.utcnow(),
                    gross_profit = pnl["gross"],
                    tax_amount   = pnl["tax_liability"],
                    tds_amount   = pnl["tds"],
                ))
                tdb.commit()
                tdb.close()
            except Exception as e:
                logger.error(f"Tax record error: {e}")

    def _emergency_close_all(self, reason: str):
        """Close open live position immediately — used on limit breaches."""
        try:
            db = _Session()
            t  = db.query(Trade).filter(
                Trade.status   == "OPEN",
                Trade.is_paper == False).first()
            db.close()
            if t:
                qty = t.qty_remaining or t.quantity
                close_order(t.direction, qty)
                logger.warning(f"Emergency close [{t.trade_id}]: {reason}")
                alert_log("CRIT", f"Emergency position close: {reason}")
        except Exception as e:
            logger.error(f"Emergency close error: {e}")

    # ── PAPER RECOVERY CHECK — V7: requires time AND trade count ─
    async def _check_recovery(self):
        start_str = kget("paper_start","")
        paper_pnl = float(kget("paper_pnl","0.0"))
        paper_wins= int(kget("paper_wins","0"))
        if not start_str:
            return
        try:
            elapsed = (datetime.utcnow() -
                       datetime.fromisoformat(start_str)
                       ).total_seconds() / 3600
        except Exception:
            return

        time_ok   = elapsed >= CFG["PAPER_HOURS"]
        profit_ok = paper_pnl > 0
        trades_ok = paper_wins >= CFG["PAPER_MIN_TRADES"]

        if time_ok and profit_ok and trades_ok:
            capital = float(kget("capital", str(CFG["INITIAL_CAPITAL"])))
            kset("mode",               "LIVE")
            kset("stop_reason",        "")
            kset("paper_pnl",          "0.0")
            kset("paper_wins",         "0")
            kset("consecutive_losses", "0")
            kset("day_start_cap",      str(capital))
            kset("heal_attempts",      "0")
            logger.info(
                f"✓ Paper trading profitable (${paper_pnl:.2f}) "
                f"over {elapsed:.1f}h with {paper_wins} wins — resuming LIVE")
            tg(f"✅ Paper trading profitable over {CFG['PAPER_HOURS']}h.\n"
               f"Paper P&L: ${paper_pnl:.4f} | Wins: {paper_wins}\n"
               f"Resuming LIVE trading automatically.", "INFO")

    # ── DAILY RESET ──────────────────────────────────────────────
    def _daily_reset(self):
        """V8: Resets at IST midnight — daily limit resets at your midnight, not UTC."""
        capital   = float(kget("capital",       str(CFG["INITIAL_CAPITAL"])))
        start     = float(kget("day_start_cap", str(capital)))
        wins      = int(kget("wins","0"))
        losses    = int(kget("losses","0"))
        total     = int(kget("total_trades","0"))
        daily_pnl = capital - start
        today     = today_ist()   # V8: IST date
        db    = _Session()
        today_trades = db.query(Trade).filter(
            Trade.entry_time >= datetime.combine(today, datetime.min.time()),
            Trade.status.in_(["WIN","LOSS","BREAKEVEN"]),
            Trade.is_paper == False).all()
        gross = sum(t.gross_pnl      for t in today_trades)
        fees  = sum(t.fees_total     for t in today_trades)
        tax   = sum(t.tax_liability  for t in today_trades)
        paper = db.query(Trade).filter(
            Trade.entry_time >= datetime.combine(today, datetime.min.time()),
            Trade.is_paper == True).count()

        existing = db.query(DailyLog).filter(DailyLog.log_date == today).first()
        if existing:
            existing.closing_capital = capital
            existing.gross_pnl       = gross
            existing.fees            = fees
            existing.net_for_trading = capital - start
            existing.tax_liability   = tax
            existing.trades_count    = len(today_trades)
            existing.wins            = sum(1 for t in today_trades if t.status=="WIN")
            existing.losses          = sum(1 for t in today_trades if t.status=="LOSS")
            existing.paper_trades    = paper
        else:
            db.add(DailyLog(
                log_date        = today,
                opening_capital = start,
                closing_capital = capital,
                gross_pnl       = gross,
                fees            = fees,
                net_for_trading = capital - start,
                tax_liability   = tax,
                trades_count    = len(today_trades),
                wins  = sum(1 for t in today_trades if t.status=="WIN"),
                losses= sum(1 for t in today_trades if t.status=="LOSS"),
                paper_trades    = paper,
            ))
        db.commit()
        db.close()

        kset("day_start_cap",  str(capital))
        kset("day_trade_count","0")

        if datetime.utcnow().weekday() == 0:
            kset("week_start_cap",  str(capital))
            kset("week_start_date", today.isoformat())

        tg(f"📅 *Daily Summary (IST)*\n"
           f"Capital: ${capital:,.2f}\n"
           f"Day P&L: ${daily_pnl:+.2f}\n"
           f"Trades: {total} | W:{wins} L:{losses}\n"
           f"Win Rate: {kget('win_rate','0')}%\n"
           f"Tax liability today: ${tax:.4f}", "INFO")


# ═══════════════════════════════════════════════════════════════════
# WATCHDOG — V7: checks heartbeat, capital floor, stale trades
# ═══════════════════════════════════════════════════════════════════
class Watchdog:

    async def run(self):
        logger.info("🛡 Watchdog started — checking every 30 seconds")
        while True:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
            await asyncio.sleep(30)

    async def _check(self):
        # Heartbeat check
        hb = kget("last_heartbeat","")
        if hb:
            try:
                stale = (datetime.utcnow() -
                         datetime.fromisoformat(hb)
                         ).total_seconds() > 180
                if stale:
                    alert_log("WARN", "Engine heartbeat stale >3min — may be stuck")
            except Exception:
                pass

        # Capital floor double-check
        try:
            cap  = float(kget("capital", "0"))
            peak = float(kget("peak_capital", "0"))
            if (peak > 0 and cap < peak * 0.70
                    and kget("manual_stop") != "true"):
                kset("manual_stop", "true")
                kset("stop_reason",
                     f"Watchdog: capital ${cap:.2f} "
                     f"30% below peak ${peak:.2f}")
                alert_log("CRIT",
                    f"Watchdog triggered capital floor. "
                    f"Cap=${cap:.2f} Peak=${peak:.2f}")
        except Exception:
            pass

        # V7: Check for stale open trade
        try:
            db = _Session()
            t  = db.query(Trade).filter(
                Trade.status   == "OPEN",
                Trade.is_paper == False).first()
            db.close()
            if t and t.entry_time:
                age_h = (datetime.utcnow() - t.entry_time).total_seconds() / 3600
                if age_h > CFG["MAX_TRADE_AGE_H"] + 1:
                    alert_log("WARN",
                        f"Watchdog: Trade {t.trade_id} is {age_h:.1f}h old "
                        f"(max {CFG['MAX_TRADE_AGE_H']}h) — engine may be stuck")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# FASTAPI — DASHBOARD V7
# ═══════════════════════════════════════════════════════════════════
app  = FastAPI(title=f"BTC Trading System v{VERSION}", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_sec = HTTPBasic()


def _auth(creds: HTTPBasicCredentials = Depends(_sec)):
    ok = (_secrets.compare_digest(
              creds.username.encode(), b"admin") and
          _secrets.compare_digest(
              creds.password.encode(),
              CFG["DASH_PASS"].encode()))
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@app.get("/health")
def health():
    return {
        "status":    "ok",
        "mode":      kget("mode", "LIVE"),
        "heartbeat": kget("last_heartbeat", ""),
        "version":   VERSION,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(_=Depends(_auth)):
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/api/status")
def api_status(_=Depends(_auth)):
    mode    = kget("mode", "LIVE")
    manual  = kget("manual_stop", "false") == "true"
    reason  = kget("stop_reason", "")
    healing = kget("heal_paused", "false") == "true"
    capital = float(kget("capital",      str(CFG["INITIAL_CAPITAL"])))
    peak    = float(kget("peak_capital", str(capital)))
    init    = CFG["INITIAL_CAPITAL"]
    total   = int(kget("total_trades", "0"))
    wins    = int(kget("wins",   "0"))
    losses  = int(kget("losses", "0"))
    wr      = float(kget("win_rate", "0.0"))
    consec  = int(kget("consecutive_losses", "0"))
    day_t   = int(kget("day_trade_count",    "0"))
    paper_pnl  = float(kget("paper_pnl", "0.0"))
    paper_wins = int(kget("paper_wins",  "0"))
    day_start  = float(kget("day_start_cap", str(capital)))
    day_pnl    = round(capital - day_start, 4)
    tax_total  = float(kget("tax_liability_total", "0.0"))
    heal_att   = int(kget("heal_attempts", "0"))

    # V7: Performance stats
    tot_gross    = float(kget("total_gross",    "0.0"))
    tot_fees     = float(kget("total_fees",     "0.0"))
    tot_wins_amt = float(kget("total_wins_amt", "0.0"))
    tot_loss_amt = float(kget("total_losses_amt","0.0"))
    expectancy   = round((tot_wins_amt - tot_loss_amt) / max(total, 1), 4)
    avg_win      = round(tot_wins_amt / max(wins, 1), 4)
    avg_loss     = round(tot_loss_amt / max(losses, 1), 4)
    profit_factor= round(tot_wins_amt / max(tot_loss_amt, 0.0001), 2)

    if   healing:         label, color = "🔧 SELF-HEALING",    "yellow"
    elif manual:          label, color = "MANUALLY STOPPED",   "red"
    elif mode == "PAPER": label, color = "PAPER TRADING",      "yellow"
    else:                 label, color = "LIVE TRADING",       "green"

    if CFG["TESTNET"]:
        label = "🧪 TESTNET — " + label

    return {
        "label":        label,      "color":       color,
        "mode":         mode,       "reason":      reason,
        "healing":      healing,    "heal_attempts": heal_att,
        "capital":      round(capital, 2),
        "peak":         round(peak,    2),
        "initial":      round(init,    2),
        "return_pct":   round((capital - init) / init * 100, 2) if init else 0,
        "day_pnl":      day_pnl,
        "paper_pnl":    round(paper_pnl, 4),
        "paper_wins":   paper_wins,
        "paper_required": CFG["PAPER_MIN_TRADES"],
        "total":        total,      "wins":        wins,
        "losses":       losses,     "win_rate":    wr,
        "consec":       consec,     "day_trades":  day_t,
        "max_day_t":    "unlimited",
        "tax_total":    round(tax_total, 4),
        "heartbeat":    kget("last_heartbeat", ""),
        "testnet":      CFG["TESTNET"],
        "version":      VERSION,
        # V7 performance stats
        "expectancy":   expectancy,
        "avg_win":      avg_win,
        "avg_loss":     avg_loss,
        "profit_factor":profit_factor,
        "session":      get_session(),
        # V13: Connection health + signal transparency
        "last_bull_score":    kget("last_bull_score",  "0"),
        "last_bear_score":    kget("last_bear_score",  "0"),
        "last_4h_trend":      kget("last_4h_trend",    "NEUTRAL"),
        "last_score_time":    kget("last_score_time",  ""),
        "last_signal_prob":   kget("last_signal_prob", "0"),
        "last_signal_dir":    kget("last_signal_dir",  ""),
        "last_signal_time":   kget("last_signal_time", ""),
        "last_reject_reason": kget("last_reject_reason",""),
        "last_binance_ping":  kget("last_binance_ping",""),
        "scan_count_hour":    int(kget("scan_count_hour","0")),
    }


@app.get("/api/open_trade")
def api_open_trade(_=Depends(_auth)):
    db = _Session()
    t  = db.query(Trade).filter(Trade.status == "OPEN").first()
    db.close()
    if not t:
        return {"trade": None}
    age = ""
    if t.entry_time:
        h   = (datetime.utcnow() - t.entry_time).total_seconds() / 3600
        age = f"{h:.1f}h"
    return {"trade": {
        "id":          t.trade_id,
        "direction":   t.direction,
        "paper":       t.is_paper,
        "entry":       t.entry_price,
        "sl":          t.stop_loss,
        "tp1":         t.take_profit1,
        "tp2":         t.take_profit2,
        "tp3":         t.take_profit3,
        "qty":         t.quantity,
        "qty_remaining": t.qty_remaining,
        "leverage":    t.leverage,
        "prob":        t.win_prob,
        "regime":      t.regime,
        "session":     t.session,
        "spread":      t.spread,
        "funding":     t.funding_rate,
        "age":         age,
        "tp1_hit":     t.tp1_hit,
        "tp2_hit":     t.tp2_hit,
        "partial_pnl": t.partial_pnl,
    }}


@app.get("/api/trades")
def api_trades(n: int = 50, _=Depends(_auth)):
    db   = _Session()
    rows = (db.query(Trade)
              .filter(Trade.status != "OPEN")
              .order_by(Trade.entry_time.desc())
              .limit(n).all())
    db.close()
    return {"trades": [{
        "id":      t.trade_id,
        "dir":     t.direction,
        "paper":   t.is_paper,
        "entry":   t.entry_price,
        "exit":    t.exit_price,
        "status":  t.status,
        "net":     t.net_for_trading,
        "fees":    t.fees_total,
        "tax":     t.tax_liability,
        "prob":    t.win_prob,
        "reason":  t.exit_reason,
        "regime":  t.regime,
        "session": t.session,
        "partial": t.partial_pnl,
        "tp1_hit": t.tp1_hit,
        "tp2_hit": t.tp2_hit,
        "time":    t.entry_time.isoformat() if t.entry_time else "",
    } for t in rows]}


@app.get("/api/daily_pnl")
def api_daily_pnl(_=Depends(_auth)):
    db   = _Session()
    rows = (db.query(DailyLog)
              .order_by(DailyLog.log_date.desc())
              .limit(30).all())
    db.close()
    return {"days": [{
        "date":    r.log_date.isoformat() if hasattr(r.log_date,"isoformat") else str(r.log_date),
        "capital": r.closing_capital,
        "pnl":     r.net_for_trading,
        "gross":   r.gross_pnl,
        "fees":    r.fees,
        "tax":     r.tax_liability,
        "trades":  r.trades_count,
        "wins":    r.wins,
        "losses":  r.losses,
    } for r in reversed(rows)]}


# V7: Equity curve endpoint
@app.get("/api/equity")
def api_equity(_=Depends(_auth)):
    db   = _Session()
    rows = (db.query(DailyLog)
              .order_by(DailyLog.log_date.asc())
              .all())
    db.close()
    init = CFG["INITIAL_CAPITAL"]
    points = [{"date": "Start", "capital": round(init, 2)}]
    for r in rows:
        points.append({
            "date":    r.log_date.isoformat() if hasattr(r.log_date,"isoformat") else str(r.log_date),
            "capital": round(r.closing_capital, 2),
        })
    return {"equity": points, "initial": round(init, 2)}


@app.get("/api/monthly")
def api_monthly(_=Depends(_auth)):
    db    = _Session()
    now   = datetime.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows  = (db.query(DailyLog)
               .filter(DailyLog.log_date >= start.date())
               .all())
    db.close()
    gross = sum(r.gross_pnl       for r in rows)
    fees  = sum(r.fees            for r in rows)
    tax   = sum(r.tax_liability   for r in rows)
    net   = sum(r.net_for_trading for r in rows)
    wins  = sum(r.wins            for r in rows)
    total = sum(r.trades_count    for r in rows)
    return {
        "month":    now.strftime("%B %Y"),
        "trades":   total,
        "wins":     wins,
        "losses":   total - wins,
        "wr":       round(wins / total * 100, 1) if total > 0 else 0,
        "gross":    round(gross, 4),
        "fees":     round(fees,  4),
        "tax":      round(tax,   4),
        "net":      round(net,   4),
        "withdraw": round(net * 0.40, 4),
        "reinvest": round(net * 0.60, 4),
    }


@app.get("/api/ytd")
def api_ytd(_=Depends(_auth)):
    db    = _Session()
    now   = datetime.utcnow()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    rows  = (db.query(DailyLog)
               .filter(DailyLog.log_date >= start.date())
               .all())
    db.close()
    gross = sum(r.gross_pnl       for r in rows)
    fees  = sum(r.fees            for r in rows)
    tax   = sum(r.tax_liability   for r in rows)
    net   = sum(r.net_for_trading for r in rows)
    wins  = sum(r.wins            for r in rows)
    total = sum(r.trades_count    for r in rows)
    init  = CFG["INITIAL_CAPITAL"]
    return {
        "year":          now.year,
        "trades":        total,
        "wins":          wins,
        "losses":        total - wins,
        "wr":            round(wins / total * 100, 1) if total > 0 else 0,
        "gross":         round(gross, 4),
        "fees":          round(fees,  4),
        "tax_liability": round(tax,   4),
        "net":           round(net,   4),
        "return_pct":    round(net / init * 100, 2) if init > 0 else 0,
    }


@app.get("/api/tax_annual")
def api_tax_annual(_=Depends(_auth)):
    db    = _Session()
    now   = datetime.utcnow()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0)
    rows  = (db.query(TaxLedger)
               .filter(TaxLedger.trade_date >= start,
                       TaxLedger.is_paid == False)
               .all())
    db.close()
    total_profit = sum(r.gross_profit for r in rows)
    total_tax    = sum(r.tax_amount   for r in rows)
    total_tds    = sum(r.tds_amount   for r in rows)
    return {
        "year":            now.year,
        "total_profit":    round(total_profit, 4),
        "tax_at_30pct":    round(total_tax,    4),
        "tds_deducted":    round(total_tds,    4),
        "net_tax_payable": round(total_tax - total_tds, 4),
        "trades_count":    len(rows),
        "note": "File under Schedule VDA in your ITR. Consult a CA.",
    }


@app.get("/api/tax_summary")
def api_tax_summary(_=Depends(_auth)):
    now   = datetime.utcnow()
    db    = _Session()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0)
    year_start  = now.replace(month=1, day=1, hour=0, minute=0, second=0)
    day_rows    = db.query(TaxLedger).filter(TaxLedger.trade_date >= today_start).all()
    month_rows  = db.query(TaxLedger).filter(TaxLedger.trade_date >= month_start).all()
    year_rows   = db.query(TaxLedger).filter(TaxLedger.trade_date >= year_start).all()
    db.close()
    def _sum(rows):
        return {
            "tax":   round(sum(r.tax_amount for r in rows), 4),
            "tds":   round(sum(r.tds_amount for r in rows), 4),
            "net":   round(sum(r.tax_amount - r.tds_amount for r in rows), 4),
            "count": len(rows),
        }
    return {
        "daily":   _sum(day_rows),
        "monthly": _sum(month_rows),
        "ytd":     _sum(year_rows),
        "total_liability": round(float(kget("tax_liability_total","0")), 4),
    }


@app.get("/api/alerts")
def api_alerts(_=Depends(_auth)):
    db   = _Session()
    rows = (db.query(AlertLog)
              .order_by(AlertLog.created_at.desc())
              .limit(20).all())
    db.close()
    return {"alerts": [{
        "level":   a.level,
        "message": a.message,
        "time":    a.created_at.isoformat(),
    } for a in rows]}


@app.get("/api/heal_log")
def api_heal_log(_=Depends(_auth)):
    db   = _Session()
    rows = (db.query(HealLog)
              .order_by(HealLog.created_at.desc())
              .limit(20).all())
    db.close()
    return {"heals": [{
        "time":     h.created_at.isoformat(),
        "type":     h.error_type,
        "msg":      h.error_msg[:100],
        "fix":      h.fix_applied,
        "success":  h.fix_success,
        "attempts": h.attempts,
    } for h in rows]}


@app.post("/api/stop")
def api_stop(_=Depends(_auth)):
    kset("manual_stop", "true")
    kset("stop_reason", "Manually stopped from dashboard")
    tg("⏹ System manually stopped from dashboard.", "INFO")
    return {"ok": True, "message": "System stopped"}


@app.post("/api/start")
def api_start(_=Depends(_auth)):
    kset("manual_stop",    "false")
    kset("stop_reason",    "")
    kset("mode",           "LIVE")
    kset("heal_paused",    "false")
    kset("heal_attempts",  "0")
    tg("▶ System manually started from dashboard.", "INFO")
    return {"ok": True, "message": "System started"}


# ═══════════════════════════════════════════════════════════════════
# DASHBOARD HTML — V7 with equity chart + performance stats
# ═══════════════════════════════════════════════════════════════════
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC Trading System v13 FINAL</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#050c18;color:#dde6f0;min-height:100vh}
.hdr{background:linear-gradient(135deg,#0b1a42,#082860);padding:12px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #00d97a;flex-wrap:wrap;gap:8px}
.hdr h1{font-size:16px;font-weight:700;color:#fff}
.hdr .sub{font-size:10px;color:#6aabe0;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:11px;padding:11px}
.card{background:#091422;border-radius:10px;padding:13px;border:1px solid #122136}
.card h3{font-size:10px;color:#4d9fd5;text-transform:uppercase;letter-spacing:1px;margin-bottom:9px;padding-bottom:5px;border-bottom:1px solid #122136}
.badge{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:14px;font-weight:700;font-size:13px}
.green{background:#00d97a18;color:#00d97a;border:1px solid #00d97a}
.yellow{background:#f5a62318;color:#f5a623;border:1px solid #f5a623}
.red{background:#e8394218;color:#e83942;border:1px solid #e83942}
.blue{background:#4d9fd518;color:#4d9fd5;border:1px solid #4d9fd5}
.dot{width:8px;height:8px;border-radius:50%;animation:blink 2s infinite}
.dg{background:#00d97a}.dy{background:#f5a623}.dr{background:#e83942}.db{background:#4d9fd5}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #0e1f30}
.row:last-child{border-bottom:none}
.rl{color:#4a6070;font-size:11px}
.rv{font-size:12px;font-weight:600}
.profit{color:#00d97a}.loss{color:#e83942}.warn{color:#f5a623}.muted{color:#3a5060}.heal{color:#4d9fd5}
.big{font-size:22px;font-weight:700}
.btn{padding:7px 16px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;border:none;transition:.2s}
.btn-go{background:#00d97a;color:#000}.btn-go:hover{background:#00b562}
.btn-st{background:#e83942;color:#fff}.btn-st:hover{background:#b02030}
.btns{display:flex;gap:8px;margin-top:9px}
.tb{width:100%;border-collapse:collapse;font-size:11px}
.tb th{text-align:left;padding:5px 7px;color:#4d9fd5;border-bottom:1px solid #122136;font-size:10px;white-space:nowrap}
.tb td{padding:5px 7px;border-bottom:1px solid #0c1c2a;white-space:nowrap}
.tb tr:hover td{background:#0c1c2a}
.chip{padding:2px 7px;border-radius:9px;font-size:10px;font-weight:600}
.cw{background:#00d97a18;color:#00d97a}
.cl{background:#e8394218;color:#e83942}
.co{background:#f5a62318;color:#f5a623}
.ch{background:#4d9fd518;color:#4d9fd5}
.full{grid-column:1/-1}
.w2{grid-column:span 2}
.al{padding:5px 8px;margin-bottom:3px;border-radius:5px;font-size:11px;border-left:3px solid}
.al-w{background:#f5a62308;border-color:#f5a623}
.al-c{background:#e8394208;border-color:#e83942}
.al-i{background:#4d9fd508;border-color:#4d9fd5}
.al-h{background:#00d97a08;border-color:#00d97a}
.rb{background:#1a0808;border:1px solid #e83942;border-radius:5px;padding:6px 8px;font-size:11px;color:#ff7070;margin-top:6px}
.hb{background:#080e18;border:1px solid #4d9fd5;border-radius:5px;padding:6px 8px;font-size:11px;color:#6aabe0;margin-top:6px}
.tnet{background:#1a1200;border:1px solid #f5a623;border-radius:5px;padding:5px 8px;font-size:11px;color:#f5a623;margin-bottom:7px}
.tabs{display:flex;gap:4px;margin-bottom:10px}
.tab{padding:5px 10px;border-radius:5px;font-size:11px;cursor:pointer;background:#0c1c2a;color:#4d9fd5;border:1px solid #122136}
.tab.active{background:#4d9fd5;color:#000}
.tab-pane{display:none}.tab-pane.active{display:block}
.divider{border:none;border-top:1px solid #122136;margin:8px 0}
/* Equity chart */
.chart-wrap{width:100%;height:120px;position:relative;margin-top:6px}
svg.echart{width:100%;height:100%}
.partial-bar{background:#f5a62318;border:1px solid #f5a623;border-radius:4px;padding:3px 6px;font-size:10px;color:#f5a623;margin-top:4px}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:6px}
.stat-box{background:#0c1c2a;border-radius:6px;padding:6px 8px;text-align:center}
.stat-v{font-size:14px;font-weight:700}
.stat-l{font-size:9px;color:#4a6070;margin-top:1px}
@media(max-width:540px){.grid{grid-template-columns:1fr}.w2{grid-column:span 1}}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <div class="hdr h1">₿ Autonomous BTC Trading System v13.0 FINAL</div>
    <div class="sub">Multi-TP · Self-Healing · Capital Protected · Exponential Growth · Railway Ready</div>
  </div>
  <div id="ts" style="font-size:10px;color:#4d9fd5;text-align:right"></div>
</div>

<div class="grid">

<!-- STATUS -->
<div class="card">
  <h3>System Status</h3>
  <div id="tnet" style="display:none" class="tnet">🧪 TESTNET — no real money</div>
  <div id="badge" class="badge green">
    <div class="dot dg" id="dot"></div>
    <span id="stxt">Loading…</span>
  </div>
  <div id="reason" style="display:none" class="rb"></div>
  <div id="healbox" style="display:none" class="hb"></div>
  <div class="btns">
    <button class="btn btn-go" onclick="ctrl('start')">▶ START</button>
    <button class="btn btn-st" onclick="ctrl('stop')">⏹ STOP</button>
  </div>
  <hr class="divider">
  <div class="row"><span class="rl">Session</span><span class="rv" id="sess">—</span></div>
  <div class="row"><span class="rl">Day Trades</span><span class="rv" id="dt">—</span></div>
  <div class="row"><span class="rl">Consecutive Losses</span><span class="rv" id="closs">—</span></div>
  <div class="row"><span class="rl">Win Rate (all-time)</span><span class="rv profit" id="wr">—</span></div>
  <div class="row"><span class="rl">Total Trades W/L</span><span class="rv" id="wl">—</span></div>
  <div id="paper-row" style="display:none">
    <hr class="divider">
    <div class="row"><span class="rl warn">📄 Paper P&L</span><span class="rv warn" id="ppnl">—</span></div>
    <div class="row"><span class="rl warn">📄 Paper Wins</span><span class="rv warn" id="pwins">—</span></div>
  </div>
</div>

<!-- CAPITAL -->
<div class="card">
  <h3>Capital (USDT) — Auto-Compounding</h3>
  <div id="cap-big" class="big profit">$0.00</div>
  <div style="color:#3a5060;font-size:10px;margin-top:2px">Current Trading Capital (incl. compounded profits)</div>
  <hr class="divider">
  <div class="row"><span class="rl">Today P&L</span><span class="rv" id="day-pnl">—</span></div>
  <div class="row"><span class="rl">Initial Capital</span><span class="rv" id="cap-i">—</span></div>
  <div class="row"><span class="rl">Peak Capital</span><span class="rv" id="cap-p">—</span></div>
  <div class="row"><span class="rl">Total Return</span><span class="rv" id="cap-r">—</span></div>
  <hr class="divider">
  <!-- V7 mini equity chart -->
  <div style="font-size:10px;color:#4a6070;margin-bottom:3px">Equity Curve (30d)</div>
  <div class="chart-wrap">
    <svg class="echart" id="echart" viewBox="0 0 300 100" preserveAspectRatio="none"></svg>
  </div>
</div>

<!-- V13: CONNECTION HEALTH PANEL -->
<div class="card">
  <h3>🔌 Connection Health & Signal Monitor</h3>
  <div class="row"><span class="rl">Binance API</span><span class="rv" id="bpstatus">—</span></div>
  <div class="row"><span class="rl">Last Binance Ping</span><span class="rv muted" id="bping">—</span></div>
  <div class="row"><span class="rl">Last Scan</span><span class="rv" id="lastscan">—</span></div>
  <div class="row"><span class="rl">Scans This Hour</span><span class="rv" id="scanhour">—</span></div>
  <hr class="divider">
  <div class="row"><span class="rl">4H Trend</span><span class="rv" id="trend4h">—</span></div>
  <div class="row"><span class="rl">🟢 BUY Score</span><span class="rv profit" id="bullscore">—</span></div>
  <div class="row"><span class="rl">🔴 SHORT Score</span><span class="rv loss" id="bearscore">—</span></div>
  <div class="row"><span class="rl">Last Signal</span><span class="rv" id="lastsig">—</span></div>
  <div class="row"><span class="rl">Last Signal Time</span><span class="rv muted" id="lastsigtime">—</span></div>
  <div class="row"><span class="rl">Last Rejection</span><span class="rv warn" id="lastrej">—</span></div>
</div>

<!-- OPEN TRADE -->
<div class="card">
  <h3>Open Trade — Multi-TP Active</h3>
  <div id="ot">
    <div style="color:#243040;font-size:12px;text-align:center;padding:18px">
      Waiting for high-probability signal…
    </div>
  </div>
</div>

<!-- PERFORMANCE STATS V7 -->
<div class="card">
  <h3>Performance Statistics</h3>
  <div class="stat-grid">
    <div class="stat-box">
      <div class="stat-v profit" id="pf">—</div>
      <div class="stat-l">Profit Factor</div>
    </div>
    <div class="stat-box">
      <div class="stat-v" id="exp">—</div>
      <div class="stat-l">Expectancy ($/trade)</div>
    </div>
    <div class="stat-box">
      <div class="stat-v profit" id="awv">—</div>
      <div class="stat-l">Avg Win</div>
    </div>
    <div class="stat-box">
      <div class="stat-v loss" id="alv">—</div>
      <div class="stat-l">Avg Loss</div>
    </div>
  </div>
  <hr class="divider">
  <div class="row"><span class="rl">Total Tax Liability</span><span class="rv warn" id="tax-total">—</span></div>
  <div class="row"><span class="rl">Strategy Version</span><span class="rv muted" id="ver">—</span></div>
</div>

<!-- P&L TABS -->
<div class="card">
  <h3>P&L Reports</h3>
  <div class="tabs">
    <div class="tab active" onclick="showTab('pnl','monthly')">Monthly</div>
    <div class="tab" onclick="showTab('pnl','ytd')">YTD</div>
  </div>
  <div id="pnl-monthly" class="tab-pane active">
    <div class="row"><span class="rl">Month</span><span class="rv" id="mmon">—</span></div>
    <div class="row"><span class="rl">Trades</span><span class="rv" id="mt">—</span></div>
    <div class="row"><span class="rl">Win Rate</span><span class="rv" id="mwr">—</span></div>
    <div class="row"><span class="rl">Gross P&L</span><span class="rv" id="mg">—</span></div>
    <div class="row"><span class="rl">Exchange Fees</span><span class="rv muted" id="mf">—</span></div>
    <div class="row"><span class="rl">Net in Capital</span><span class="rv" id="mn">—</span></div>
    <hr class="divider">
    <div class="row"><span class="rl">Withdrawal (40%)</span><span class="rv profit" id="mw">—</span></div>
    <div class="row"><span class="rl">Reinvested (60%)</span><span class="rv" id="mr">—</span></div>
  </div>
  <div id="pnl-ytd" class="tab-pane">
    <div class="row"><span class="rl">Year</span><span class="rv" id="ytd-year">—</span></div>
    <div class="row"><span class="rl">Trades</span><span class="rv" id="yt">—</span></div>
    <div class="row"><span class="rl">Win Rate</span><span class="rv" id="ywr">—</span></div>
    <div class="row"><span class="rl">Gross P&L</span><span class="rv" id="yg">—</span></div>
    <div class="row"><span class="rl">Total Fees</span><span class="rv muted" id="yf">—</span></div>
    <div class="row"><span class="rl">Net in Capital</span><span class="rv" id="yn">—</span></div>
    <div class="row"><span class="rl">YTD Return %</span><span class="rv" id="yr">—</span></div>
  </div>
</div>

<!-- TAX SUMMARY -->
<div class="card">
  <h3>Tax Liability (India 30% + 1% TDS)</h3>
  <div style="font-size:10px;color:#3a5060;margin-bottom:8px">
    Tax tracked as liability — stays in trading capital until filing
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('tax','daily')">Today</div>
    <div class="tab" onclick="showTab('tax','monthly')">MoTD</div>
    <div class="tab" onclick="showTab('tax','ytd')">YTD</div>
  </div>
  <div id="tax-daily" class="tab-pane active">
    <div class="row"><span class="rl">Tax Liability</span><span class="rv warn" id="tax-d">—</span></div>
    <div class="row"><span class="rl">TDS Deducted</span><span class="rv muted" id="tds-d">—</span></div>
    <div class="row"><span class="rl">Net Payable</span><span class="rv warn" id="taxnet-d">—</span></div>
  </div>
  <div id="tax-monthly" class="tab-pane">
    <div class="row"><span class="rl">Tax Liability</span><span class="rv warn" id="tax-m">—</span></div>
    <div class="row"><span class="rl">TDS Deducted</span><span class="rv muted" id="tds-m">—</span></div>
    <div class="row"><span class="rl">Net Payable</span><span class="rv warn" id="taxnet-m">—</span></div>
  </div>
  <div id="tax-ytd" class="tab-pane">
    <div class="row"><span class="rl">Tax Liability</span><span class="rv warn" id="tax-y">—</span></div>
    <div class="row"><span class="rl">TDS Deducted</span><span class="rv muted" id="tds-y">—</span></div>
    <div class="row"><span class="rl">Net Payable</span><span class="rv warn" id="taxnet-y">—</span></div>
  </div>
  <hr class="divider">
  <div class="row"><span class="rl">Total Liability Balance</span><span class="rv warn" id="tax-total2">—</span></div>
</div>

<!-- ALERTS + HEAL LOG -->
<div class="card">
  <h3>System Alerts & Self-Heal Log</h3>
  <div class="tabs">
    <div class="tab active" onclick="showTab('sys','alerts')">Alerts</div>
    <div class="tab" onclick="showTab('sys','heals')">Heal Log</div>
  </div>
  <div id="sys-alerts" class="tab-pane active">
    <div id="alerts"><div style="color:#243040;font-size:11px">✓ System healthy</div></div>
  </div>
  <div id="sys-heals" class="tab-pane">
    <div id="heallog"><div style="color:#243040;font-size:11px">No self-heal events yet</div></div>
  </div>
</div>

<!-- TRADE LOG -->
<div class="card full">
  <h3>Recent Trades — Multi-TP Breakdown</h3>
  <div style="overflow-x:auto">
  <table class="tb">
    <thead><tr>
      <th>Time</th><th>Dir</th><th>Entry</th><th>Exit</th>
      <th>Partial Banked</th><th>Total Net</th><th>Fees</th><th>Tax Liab.</th>
      <th>TP Exits</th><th>Session</th><th>Exit Reason</th><th>Status</th>
    </tr></thead>
    <tbody id="tbody">
      <tr><td colspan="12" style="color:#243040;text-align:center;padding:14px">Loading…</td></tr>
    </tbody>
  </table>
  </div>
</div>

</div>

<script>
const f2  = n => `$${Number(n||0).toFixed(2)}`;
const f4  = n => `$${Number(n||0).toFixed(4)}`;
const fp  = n => `${Number(n||0).toFixed(1)}%`;
const pos = n => Number(n||0) >= 0 ? 'profit' : 'loss';
const sgn = n => (Number(n||0) >= 0 ? '+' : '') + f4(n);

function showTab(group, name) {
  document.querySelectorAll(`[id^="${group}-"]`).forEach(el => el.classList.remove('active'));
  document.getElementById(`${group}-${name}`).classList.add('active');
  const pane = document.getElementById(`${group}-${name}`).closest('.card');
  pane.querySelectorAll('.tab').forEach(t => {
    t.classList.remove('active');
    if (t.getAttribute('onclick').includes(name)) t.classList.add('active');
  });
}

function drawEquity(points) {
  const svg = document.getElementById('echart');
  if (!points || points.length < 2) return;
  const vals = points.map(p => p.capital);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const range = mx - mn || 1;
  const W = 300, H = 100, pad = 6;
  const xs = points.map((_,i) => pad + (i/(points.length-1)) * (W-pad*2));
  const ys = vals.map(v => H - pad - ((v - mn)/range) * (H-pad*2));
  const line = xs.map((x,i) => `${i===0?'M':'L'}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ');
  const area = line + ` L${xs[xs.length-1].toFixed(1)},${H-pad} L${pad},${H-pad} Z`;
  const last = vals[vals.length-1], first = vals[0];
  const col = last >= first ? '#00d97a' : '#e83942';
  svg.innerHTML = `
    <defs>
      <linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${col}" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="${col}" stop-opacity="0.02"/>
      </linearGradient>
    </defs>
    <path d="${area}" fill="url(#eg)"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="1.8"/>
    <circle cx="${xs[xs.length-1].toFixed(1)}" cy="${ys[ys.length-1].toFixed(1)}" r="3" fill="${col}"/>`;
}

function setSt(d) {
  const c = d.color;
  const dotClass = c==='green'?'dg':c==='yellow'?'dy':c==='red'?'dr':'db';
  document.getElementById('badge').className = `badge ${c}`;
  document.getElementById('dot').className   = `dot ${dotClass}`;
  document.getElementById('stxt').textContent = d.label;

  const r = document.getElementById('reason');
  if (d.reason) { r.style.display='block'; r.textContent='Reason: '+d.reason; }
  else r.style.display='none';

  const hb = document.getElementById('healbox');
  if (d.healing) {
    hb.style.display='block';
    hb.textContent = `🔧 Self-healing in progress (attempt ${d.heal_attempts}/${3}) — live trading paused`;
  } else hb.style.display='none';

  if (d.testnet) document.getElementById('tnet').style.display='block';

  const capEl = document.getElementById('cap-big');
  if (d.capital > 0) {
    capEl.textContent = f2(d.capital);
    capEl.className = 'big ' + (d.capital >= d.initial ? 'profit' : 'loss');
  } else {
    capEl.textContent = 'Loading...';
    capEl.className = 'big muted';
  }
  document.getElementById('cap-i').textContent   = f2(d.initial);
  document.getElementById('cap-p').textContent   = f2(d.peak);
  const re = document.getElementById('cap-r');
  re.textContent = (d.return_pct>=0?'+':'')+fp(d.return_pct);
  re.className   = 'rv '+(d.return_pct>=0?'profit':'loss');

  const dp = document.getElementById('day-pnl');
  dp.textContent = sgn(d.day_pnl);
  dp.className   = 'rv '+pos(d.day_pnl);

  document.getElementById('wr').textContent  = fp(d.win_rate);
  document.getElementById('wl').textContent  = `${d.wins} / ${d.losses}`;
  document.getElementById('dt').textContent  = `${d.day_trades} / ${d.max_day_t}`;
  document.getElementById('sess').textContent= d.session||'—';
  const ce = document.getElementById('closs');
  ce.textContent = d.consec;
  ce.className   = 'rv '+(d.consec>=2?'loss':d.consec>0?'warn':'profit');
  document.getElementById('tax-total').textContent  = f4(d.tax_total);
  document.getElementById('tax-total2').textContent = f4(d.tax_total);

  // V7 perf stats
  const pfv = document.getElementById('pf');
  pfv.textContent = d.profit_factor||'—';
  pfv.className   = 'stat-v '+(Number(d.profit_factor||0)>=1?'profit':'loss');
  const expv = document.getElementById('exp');
  expv.textContent = d.expectancy ? (Number(d.expectancy)>=0?'+':'')+f4(d.expectancy).replace('$','') : '—';
  expv.className   = 'stat-v '+pos(d.expectancy);
  document.getElementById('awv').textContent = d.avg_win  ? f4(d.avg_win)  : '—';
  document.getElementById('alv').textContent = d.avg_loss ? f4(d.avg_loss) : '—';
  document.getElementById('ver').textContent = 'v' + (d.version||'7.0');

  // V13: Connection health panel
  // Binance ping status
  const bp = d.last_binance_ping || '';
  const bpEl = document.getElementById('bpstatus');
  const bpTimeEl = document.getElementById('bping');
  if (!bp) {
    bpEl.textContent = '⏳ Not checked yet'; bpEl.className='rv warn';
  } else if (bp.startsWith('ERROR')) {
    bpEl.textContent = '🔴 DISCONNECTED'; bpEl.className='rv loss';
    bpTimeEl.textContent = bp.replace('ERROR:','');
  } else {
    bpEl.textContent = '🟢 CONNECTED'; bpEl.className='rv profit';
    try { bpTimeEl.textContent = new Date(bp).toLocaleTimeString(); } catch(e){}
  }
  // Last scan (from heartbeat)
  const hb = d.heartbeat || '';
  const scanEl = document.getElementById('lastscan');
  if (hb) {
    try {
      const secs = Math.floor((Date.now() - new Date(hb).getTime()) / 1000);
      if (secs < 30) { scanEl.textContent = secs+'s ago'; scanEl.className='rv profit'; }
      else if (secs < 120) { scanEl.textContent = secs+'s ago ⚠️'; scanEl.className='rv warn'; }
      else { scanEl.textContent = secs+'s ago 🔴 STALE'; scanEl.className='rv loss'; }
    } catch(e) { scanEl.textContent = '—'; }
  }
  document.getElementById('scanhour').textContent = (d.scan_count_hour||0) + ' scans/hr';
  // 4H trend and scores
  const t4 = d.last_4h_trend || 'NEUTRAL';
  const t4El = document.getElementById('trend4h');
  t4El.textContent = t4;
  t4El.className = 'rv ' + (t4==='UP'?'profit':t4==='DOWN'?'loss':'muted');
  document.getElementById('bullscore').textContent = d.last_bull_score || '0';
  document.getElementById('bearscore').textContent = d.last_bear_score || '0';
  // Last signal
  const lsd = d.last_signal_dir || '';
  const lsp = d.last_signal_prob || '0';
  const lsEl = document.getElementById('lastsig');
  if (lsd) {
    lsEl.textContent = lsd + ' @ ' + lsp + '%';
    lsEl.className = 'rv ' + (lsd==='BUY'?'profit':'loss');
  } else { lsEl.textContent = 'None yet'; lsEl.className='rv muted'; }
  try {
    const lst = d.last_signal_time;
    if (lst) document.getElementById('lastsigtime').textContent = new Date(lst).toLocaleTimeString();
  } catch(e){}
  document.getElementById('lastrej').textContent = d.last_reject_reason || '—';

  const pr = document.getElementById('paper-row');
  if (d.mode === 'PAPER') {
    pr.style.display = 'block';
    document.getElementById('ppnl').textContent  = sgn(d.paper_pnl);
    document.getElementById('pwins').textContent = `${d.paper_wins} / ${d.paper_required} required`;
  } else pr.style.display = 'none';

  document.getElementById('ts').textContent = 'Updated '+new Date().toLocaleTimeString();
}

function setOT(d) {
  const el = document.getElementById('ot');
  if (!d.trade) {
    el.innerHTML = '<div style="color:#243040;font-size:12px;text-align:center;padding:18px">No open trade — waiting for 68%+ signal</div>';
    return;
  }
  const t  = d.trade;
  const dc = t.direction==='BUY'?'profit':'loss';
  const ds = t.direction==='BUY'?'▲ BUY LONG':'▼ SHORT SELL';
  const p1c = t.tp1_hit ? '<span class="chip cw">✓ HIT</span>' : '<span class="chip co">OPEN</span>';
  const p2c = t.tp2_hit ? '<span class="chip cw">✓ HIT</span>' : '<span class="chip co">OPEN</span>';
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;margin-bottom:7px">
      <span class="${dc}" style="font-weight:700">${ds}</span>
      <span class="chip ${t.paper?'co':'cw'}">${t.paper?'PAPER':'LIVE'}</span>
    </div>
    <div class="row"><span class="rl">Entry</span><span class="rv">${f2(t.entry)}</span></div>
    <div class="row"><span class="rl">Stop Loss (trailing)</span><span class="rv loss">${f2(t.sl)}</span></div>
    <div class="row"><span class="rl">TP1 15% ${p1c}</span><span class="rv profit">${f2(t.tp1)}</span></div>
    <div class="row"><span class="rl">TP2 35% ${p2c}</span><span class="rv profit">${f2(t.tp2)}</span></div>
    <div class="row"><span class="rl">TP3 25% (runner)</span><span class="rv profit">${f2(t.tp3)}</span></div>
    <div class="row"><span class="rl">Remaining Qty</span><span class="rv">${t.qty_remaining||t.qty} / ${t.qty} BTC</span></div>
    <div class="row"><span class="rl">Win Prob / Leverage</span><span class="rv">${fp(t.prob)} / ${t.leverage}×</span></div>
    <div class="row"><span class="rl">Age / Session</span><span class="rv">${t.age||'—'} / ${t.session||'—'}</span></div>
    ${(t.partial_pnl||0)!==0?`<div class="partial-bar">🎯 Partial profits banked: ${sgn(t.partial_pnl)}</div>`:''}`;
}

function setMon(d) {
  document.getElementById('mmon').textContent = d.month||'';
  document.getElementById('mt').textContent  = d.trades;
  document.getElementById('mwr').textContent = fp(d.wr);
  const g = document.getElementById('mg'); g.textContent=sgn(d.gross); g.className='rv '+pos(d.gross);
  document.getElementById('mf').textContent  = f4(d.fees);
  const n = document.getElementById('mn'); n.textContent=sgn(d.net); n.className='rv '+pos(d.net);
  document.getElementById('mw').textContent  = f4(d.withdraw);
  document.getElementById('mr').textContent  = f4(d.reinvest);
}

function setYTD(d) {
  document.getElementById('ytd-year').textContent = d.year||'';
  document.getElementById('yt').textContent  = d.trades;
  document.getElementById('ywr').textContent = fp(d.wr);
  const g = document.getElementById('yg'); g.textContent=sgn(d.gross); g.className='rv '+pos(d.gross);
  document.getElementById('yf').textContent  = f4(d.fees);
  const n = document.getElementById('yn'); n.textContent=sgn(d.net); n.className='rv '+pos(d.net);
  const r = document.getElementById('yr'); r.textContent=(d.return_pct>=0?'+':'')+fp(d.return_pct); r.className='rv '+pos(d.return_pct);
}

function setTax(d) {
  const s = (id, v) => { document.getElementById(id).textContent = f4(v); };
  s('tax-d', d.daily.tax);   s('tds-d', d.daily.tds);   s('taxnet-d', d.daily.net);
  s('tax-m', d.monthly.tax); s('tds-m', d.monthly.tds); s('taxnet-m', d.monthly.net);
  s('tax-y', d.ytd.tax);     s('tds-y', d.ytd.tds);     s('taxnet-y', d.ytd.net);
}

function setAl(d) {
  const el = document.getElementById('alerts');
  if (!d.alerts||!d.alerts.length) { el.innerHTML='<div style="color:#243040;font-size:11px">✓ System healthy — no alerts</div>'; return; }
  el.innerHTML = d.alerts.slice(0,8).map(a=>{
    const cls = a.level==='CRIT'?'al-c':a.level==='WARN'?'al-w':a.level==='HEAL'?'al-h':'al-i';
    return `<div class="al ${cls}"><strong>${a.level}</strong> — ${a.message.substring(0,110)}<div style="font-size:9px;color:#3a5060">${new Date(a.time).toLocaleString()}</div></div>`;
  }).join('');
}

function setHealLog(d) {
  const el = document.getElementById('heallog');
  if (!d.heals||!d.heals.length) { el.innerHTML='<div style="color:#243040;font-size:11px">No self-heal events yet</div>'; return; }
  el.innerHTML = d.heals.slice(0,6).map(h=>{
    const sc = h.success?'cw':'cl';
    return `<div style="padding:5px 0;border-bottom:1px solid #0e1f30"><div style="display:flex;justify-content:space-between"><span style="color:#4d9fd5;font-size:11px">${h.type}</span><span class="chip ${sc}">${h.success?'FIXED':'FAILED'}</span></div><div style="font-size:10px;color:#3a5060">Fix: ${h.fix} | Attempts: ${h.attempts}</div><div style="font-size:9px;color:#2a3f50">${new Date(h.time).toLocaleString()}</div></div>`;
  }).join('');
}

function setTr(d) {
  const tb = document.getElementById('tbody');
  if (!d.trades||!d.trades.length) {
    tb.innerHTML='<tr><td colspan="12" style="color:#243040;text-align:center;padding:14px">No completed trades yet</td></tr>';
    return;
  }
  tb.innerHTML = d.trades.map(t=>{
    const sc = t.status==='WIN'?'cw':t.status==='LOSS'?'cl':'co';
    const dc = t.dir==='BUY'?'profit':'loss';
    const tpbadge = [t.tp1_hit?'<span class="chip cw">TP1</span>':'', t.tp2_hit?'<span class="chip cw">TP2</span>':''].filter(Boolean).join(' ') || '—';
    return `<tr>
      <td style="color:#3a5060">${t.time?new Date(t.time).toLocaleString():'—'}</td>
      <td class="${dc}" style="font-weight:600">${t.dir==='BUY'?'▲ BUY':'▼ SHORT'}</td>
      <td>${t.entry?f2(t.entry):'—'}</td>
      <td>${t.exit?f2(t.exit):'—'}</td>
      <td class="warn">${t.partial&&t.partial!==0?sgn(t.partial):'—'}</td>
      <td class="${Number(t.net||0)>=0?'profit':'loss'}">${sgn(t.net)}</td>
      <td class="muted">${f4(t.fees)}</td>
      <td class="warn">${f4(t.tax)}</td>
      <td>${tpbadge}</td>
      <td class="muted">${t.session||'—'}</td>
      <td class="muted">${t.reason||'—'}</td>
      <td><span class="chip ${sc}">${t.status}</span></td>
    </tr>`;
  }).join('');
}

async function refresh() {
  try {
    const [s,o,m,y,tx,al,tr,hl,eq] = await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/open_trade').then(r=>r.json()),
      fetch('/api/monthly').then(r=>r.json()),
      fetch('/api/ytd').then(r=>r.json()),
      fetch('/api/tax_summary').then(r=>r.json()),
      fetch('/api/alerts').then(r=>r.json()),
      fetch('/api/trades?n=40').then(r=>r.json()),
      fetch('/api/heal_log').then(r=>r.json()),
      fetch('/api/equity').then(r=>r.json()),
    ]);
    setSt(s); setOT(o); setMon(m); setYTD(y);
    setTax(tx); setAl(al); setTr(tr); setHealLog(hl);
    if (eq && eq.equity) drawEquity(eq.equity);
  } catch(e) { console.error('Refresh error:', e); }
}

async function ctrl(action) {
  const msg = action==='stop'
    ? 'Stop system? Open positions will be managed to their stop loss.'
    : 'Start live trading?';
  if (!confirm(msg)) return;
  await fetch(`/api/${action}`, {method:'POST'});
  setTimeout(refresh, 1200);
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
# SERVER
# ═══════════════════════════════════════════════════════════════════
async def start_server():
    config = uvicorn.Config(
        app,
        host      = "0.0.0.0",
        port      = CFG["PORT"],
        log_level = "warning",
    )
    await uvicorn.Server(config).serve()


# ═══════════════════════════════════════════════════════════════════
# MAIN STARTUP
# ═══════════════════════════════════════════════════════════════════
async def main():
    setup_logging()
    logger.info("=" * 70)
    logger.info(f"  AUTONOMOUS BITCOIN TRADING SYSTEM v{VERSION} FINAL")
    logger.info("  14-Rule Engine | Fast Scan 15s | Candle Patterns | IST Reset")
    logger.info("  Self-Healing | Capital Protected | Exponential Growth")
    logger.info("=" * 70)

    if CFG["TESTNET"]:
        logger.warning("⚠️  TESTNET MODE — no real money at risk")
        logger.warning("   Change USE_TESTNET=false when ready for live")

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT,  graceful_shutdown)

    db_init()
    reconcile_on_startup()

    logger.info(f"✓ Dashboard  → http://0.0.0.0:{CFG['PORT']}")
    logger.info("✓ Login      → username: admin | password: [DASHBOARD_PASSWORD]")
    logger.info("✓ Health     → /health (no auth — Railway uptime check)")
    logger.info(f"✓ DB path    → {_DATA_DIR}/trading.db")
    logger.info(f"✓ Self-Heal  → {'ENABLED' if CFG['SELF_HEAL'] else 'DISABLED'}")
    logger.info(f"✓ Multi-TP   → TP1 {CFG['TP1_PCT']}% fee-lock + 85% adaptive ATR runner")
    logger.info(f"✓ Paper gate → {CFG['PAPER_HOURS']}h + {CFG['PAPER_MIN_TRADES']} wins required")
    logger.info("✓ Starting trading engine, watchdog, dashboard…")

    tg(f"🚀 *BTC Trading System v{VERSION} Started*\n"
       f"Capital: ${CFG['INITIAL_CAPITAL']:,.2f} USDT\n"
       f"Mode: {'🧪 TESTNET' if CFG['TESTNET'] else '💰 LIVE TRADING'}\n"
       f"Fast scan: {CFG['SCAN_FAST_S']}s | 14 rules | Candle patterns: ON\n"
       f"Loss cooldown: REMOVED (V9) | IST daily reset: ON\n"
       f"Paper gate: {CFG['PAPER_HOURS']}h + {CFG['PAPER_MIN_TRADES']} wins\n"
       f"Multi-TP: TP1 15% lock + 85% adaptive ATR runner", "INFO")

    await asyncio.gather(
        start_server(),
        TradingEngine().run(),
        Watchdog().run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
