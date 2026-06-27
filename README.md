# Autonomous Bitcoin Trading System V9 FINAL

**Single file · Multi-TP · Self-Healing · Plug & Play · Auto-deploys from GitHub to Railway**

---

## What's in V9 FINAL

| Feature | Status |
|---|---|
| Take-Profit levels | ✅ 3 levels (TP1 40%, TP2 35%, TP3 25%) |
| Partial exit tracking | ✅ Partial PnL banked + shown on dashboard |
| Breakeven SL lock | ✅ Auto-locks at BE after TP1 hit |
| ATR-based SL scaling | ✅ Dynamic per regime (LOW/NORMAL/HIGH) |
| Paper→Live gate | ✅ 1h + positive P&L + 2 paper wins required |
| Trend conflict check | ✅ Rejects if 4H+1H directly contradict signal |
| Session quality scoring | ✅ Full scoring: ASIA/LONDON/NY/OVERLAP |
| Expected Value (EV) gate | ✅ Rejects negative EV trades even if fees pass |
| Emergency close on limits | ✅ Closes open position on weekly/capital breach |
| Self-heal error types | ✅ 10 error types fully handled |
| Performance stats | ✅ Profit Factor, Expectancy, Avg Win/Loss |
| Equity chart on dashboard | ✅ 30-day SVG equity curve live |
| Watchdog stale trade check | ✅ Alerts if engine stuck with open trade |
| Reconcile PnL on recovery | ✅ Fetches actual unrealized PnL from Binance |
| Win probability gate | ✅ 68% minimum — rejects weak signals |
| 14-rule weighted signal engine | ✅ Multi-timeframe confirmation required |
| Candle pattern detection | ✅ ON |
| Loss cooldown | ✅ REMOVED — 68% gate + 14 rules already protect |
| Tax tracking (India) | ✅ 30% + TDS, tracked as liability, paid at year end |
| IST daily reset | ✅ ON |
| Circuit breakers | ✅ Daily -5%, Weekly, Peak drawdown, Consecutive loss guards |
| Single file architecture | ✅ Everything in bot.py (~3,100 lines) |
| Version | **9.0 FINAL** |

---

## What Changed from V8 → V9 FINAL

| Item | V8 | V9 FINAL |
|---|---|---|
| Loss cooldown | 15 min wait after every loss | ✅ Removed — redundant with signal gates |
| Version | 9.0 | ✅ 9.0 FINAL |

**Why cooldown was removed:** The system already has 5 layers of protection — 68% win probability gate, 14-rule weighted scoring, 4H+1H trend alignment, R:R ≥ 1.5 minimum, and EV > $0.50 gate. These already filter poor post-loss setups. The 15-min cooldown was a 6th redundant guard that only blocked genuine high-probability signals.

---

## Deploy in 4 Steps

### Step 1 — Edit settings.env
Replace these 4 values (minimum):
```
BINANCE_API_KEY=your_real_key
BINANCE_API_SECRET=your_real_secret
INITIAL_CAPITAL_USDT=1300
DASHBOARD_PASSWORD=choose_any_password
```
Keep `USE_TESTNET=true` until you've verified it working.

### Step 2 — Add Railway Volume (keeps all data safe forever)
Railway → your project → service → **Volumes** → **Add Volume** → mount path: `/data`

### Step 3 — Push to GitHub
Create a **private** repo on github.com, upload all 5 files from this folder.

### Step 4 — Connect to Railway
Railway → **New Project** → **Deploy from GitHub** → select repo → add the env vars from settings.env → **Deploy**

---

## Your Dashboard
- **URL:** `https://your-project.up.railway.app`
- **Username:** `admin`
- **Password:** your `DASHBOARD_PASSWORD`

### Dashboard shows:
- System status (Live / Paper / Healing / Stopped)
- Live equity curve (30-day chart)
- Open trade with TP1/TP2/TP3 levels + partial exit tracking
- Profit Factor, Expectancy, Avg Win/Loss
- Day P&L / Month P&L / Year P&L
- Tax Daily / MoTD / YTD (India 30% + TDS)
- Self-Heal log with fix audit trail
- Recent trades with partial exit breakdown
- Manual Stop / Start button

---

## How Multi-TP Works
Each trade automatically splits into 3 exits:
1. **TP1 (40% of position)** — at 1.5× ATR from entry → partial profit banked, SL moves to breakeven
2. **TP2 (35% of position)** — at 2.5× ATR from entry → more profit banked
3. **TP3 (25% runner)** — at 4.0× ATR from entry → full close OR stopped out by trailing SL

---

## How Paper→Live Recovery Works
System switches to paper trading after:
- 5 consecutive losses
- 5% daily drawdown

To return to live, ALL 3 conditions must be met:
1. ✅ At least 1 hour has passed
2. ✅ Paper P&L is positive
3. ✅ At least 2 winning paper trades completed

---

## Your Only Manual Tasks — Forever
1. Add money to Binance Futures wallet when scaling up
2. Withdraw profit from Binance when you want income
3. Renew Binance API key every 90 days (Binance emails a reminder)
4. Pay Railway bill (~$5/month, auto-charged)

---

## Files
| File | Purpose |
|---|---|
| `bot.py` | Complete system — everything in one file (~3,100 lines) |
| `settings.env` | Your config — the ONLY file you ever edit |
| `requirements.txt` | Auto-installed by Railway |
| `railway.toml` | Railway config — auto-restart forever |
| `.python-version` | Python 3.11.7 pinned |
