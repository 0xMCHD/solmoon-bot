"""Solana memecoin auto-trader — capital maximization strategy."""

import asyncio
import httpx
import time
from datetime import datetime, timezone

import config
import jupiter
import wallet
from meme_scanner import MemeCoin, MemeScanner
from wallet_tracker import WalletTracker
from wallet_websocket import WebSocketMonitor

# ---------------------------------------------------------------------------
# Position parameters
# ---------------------------------------------------------------------------
MEME_MAX_POSITION_SOL   = 0.015   # standard MOON (multi-wallet, not ultra-early)
ULTRA_EARLY_POSITION_SOL = 0.025  # ULTRA_EARLY tier — bigger when jackpot conditions met
MEME_TAKE_PROFIT_PCT    = 0.40    # final TP +40%
MEME_STOP_LOSS_PCT      = 0.15    # SL -15% (standard MOON)
ULTRA_EARLY_STOP_LOSS   = 0.25    # SL -25% for ULTRA_EARLY (accept variance for asymmetric upside)
MEME_TIMEOUT_SECONDS    = 2700    # 45 min max (standard MOON)
# ULTRA_EARLY has NO timeout — rides until trailing stop / SL / manual
MAX_CONCURRENT_TRADES   = 1       # focus mode — 1 trade at a time
MAX_WHALE_PREMIUM       = 0.15    # only copy if current price ≤ whale entry +15%
                                  # above that, we'd be buying the whale's exit liquidity

# Partial exit — triggers earlier because we enter higher into the pump
PARTIAL_SELL_TRIGGER    = 0.15    # sell 50% of the position at +15% (was +20%)
PARTIAL_SELL_RATIO      = 0.50    # 50% of tokens sold at trigger

# Trailing stop (on the remaining half)
TRAILING_ACTIVATE_PCT   = 0.12    # activates at +12% (was +15%)
TRAILING_DISTANCE_PCT   = 0.05    # pulls back 5% from the peak

# MOON mode — copy trade only (no fixed TP, wide trailing)
MOON_TRAILING_DISTANCE  = 0.08    # 8% — gives mega pumps room to breathe
MOON_MIN_GAIN_LOG       = 0.50    # log a warning if we exit below +50% in MOON mode
MOON_MAX_AGE_HOURS      = 168     # 7 days max even for whale copies (blocks stale tokens)

# ---------------------------------------------------------------------------
# Entry criteria
# ---------------------------------------------------------------------------
ENTRY_PUMP_1H_MIN       = 5.0     # +5% — confirmed momentum
ENTRY_PUMP_1H_MAX       = 50.0    # max +50% 1h — DexScreener trending = already pumped
ENTRY_PUMP_6H_MAX       = 500.0   # max +500% 6h — memecoins can 5x and keep going
ENTRY_BUY_RATIO_MIN     = 0.50    # 50% — majority of buyers
ENTRY_AGE_MIN_HOURS     = 0.17    # 10min — early
ENTRY_AGE_MAX_HOURS     = 4.0     # 4h max — ultra fresh
ENTRY_MIN_SCORE         = 30      # loose
ENTRY_MIN_LIQUIDITY     = 30_000  # $30K — small-caps OK
ENTRY_MAX_VOL_LIQ_RATIO = 12.0    # Vol 1h / Liq < 12× — pump exhaustion filter
ENTRY_MAX_RUGCHECK_RISK = 300     # KEEP strict — no rugs
TRADE_COOLDOWN_SECONDS  = 10800   # 3h cooldown after token exit

# ---------------------------------------------------------------------------
# Dynamic position — scales with signal strength
# ---------------------------------------------------------------------------
POSITION_BASE_SOL       = 0.08    # scanner alone or 1 wallet
POSITION_BOOST_2W_SOL   = 0.10    # 2 simultaneous alpha wallets
POSITION_BOOST_3W_SOL   = 0.12    # 3+ alpha wallets — very strong signal

# ---------------------------------------------------------------------------
# Auto blacklist — 24h after a SL
# ---------------------------------------------------------------------------
SL_BLACKLIST_HOURS      = 24      # token blacklisted 24h after triggering SL

# ---------------------------------------------------------------------------
# Blacklist — stablecoins, base tokens, wrapped assets never tradeable
# (prevents accidental USDC/wSOL buys via DexScreener copy-trade pairs)
# ---------------------------------------------------------------------------
BLACKLISTED_TOKEN_ADDRESSES: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",       # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL (Marinade)
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH (Wormhole)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",   # BTC (Wormhole)
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",   # stSOL (Lido)
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",    # bSOL (BlazeStake)
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",   # jitoSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK (avoid stale re-buy)
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",   # RAY (Raydium — base token)
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",   # PYTH
}

# ---------------------------------------------------------------------------
class RugChecker:
    """Rug risk check via rugcheck.xyz (free)."""

    API = "https://api.rugcheck.xyz/v1/tokens"

    async def check(self, token_address: str) -> dict:
        result = {
            "safe": False, "score": 999, "risks": [],
            "lp_locked": False, "top10_pct": 100, "error": None,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.API}/{token_address}/report",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result["score"] = data.get("score", 999)
                    risks = data.get("risks", [])
                    result["risks"] = [r.get("name", "") for r in risks]

                    markets = data.get("markets", [])
                    if markets:
                        lp = markets[0].get("lp", {})
                        result["lp_locked"] = lp.get("lpLockedPct", 0) > 80

                    top_holders = data.get("topHolders", [])
                    if top_holders:
                        result["top10_pct"] = sum(h.get("pct", 0) for h in top_holders[:10])

                    critical = ["Freeze Authority still enabled",
                                "Mint Authority still enabled",
                                "Honeypot"]
                    has_critical = any(r in result["risks"] for r in critical)
                    result["safe"] = result["score"] < ENTRY_MAX_RUGCHECK_RISK and not has_critical

                elif resp.status_code == 404:
                    result["error"] = "Token not found on rugcheck"
                else:
                    result["error"] = f"API error {resp.status_code}"
        except Exception as e:
            result["error"] = str(e)
        return result


# ---------------------------------------------------------------------------
STATS_FILE = "stats.json"
BALANCE_HISTORY_FILE = "balance_history.json"   # snapshots for dashboard chart
TRADES_LOG_FILE = "trades_history.json"         # per-trade history for dashboard
BALANCE_SNAPSHOT_INTERVAL = 1800                # 30 min between snapshots
DAILY_CIRCUIT_BREAKER_PCT = -50.0               # effectively disabled
                                                # WHY: daily_pnl is computed from balance delta,
                                                # which incorrectly treats SELL UNCERTAIN held
                                                # tokens as realized losses. Until we track
                                                # realized PnL per trade properly, the breaker
                                                # is unreliable. The 0.50 SOL kill switch in
                                                # CHALLENGE_PRIVATE.md remains the real safety net.
CIRCUIT_BREAKER_PAUSE_SEC = 86400               # 24h pause after circuit breaker hits


class MemeTrader:
    """Memecoin bot with partial exit + trailing stop + 2 simultaneous trades."""

    def __init__(self, keypair=None, pubkey: str = None):
        self.keypair  = keypair
        self.pubkey   = pubkey
        self.wallet_tracker = WalletTracker(rpc_url=config.RPC_URL)
        self.scanner  = MemeScanner(wallet_tracker=self.wallet_tracker)
        self.rug_checker = RugChecker()
        # Helius WebSocket monitor — push-based detection (real-time, ~3s latency)
        # Replaces (or complements) the 15s wallet poll loop.
        self.ws_monitor = WebSocketMonitor(
            rpc_url=config.RPC_URL,
            alpha_wallets=self.wallet_tracker.ALPHA_WALLETS,
            on_buy_callback=self._on_ws_buy,
        )
        self.active_trades: dict[str, dict] = {}
        self.trade_cooldowns: dict[str, float] = {}  # token → exit timestamp
        self.skip_cache: dict[str, tuple] = {}        # token → (expiry_ts, reason)
        self.sl_blacklist: dict[str, float] = {}      # token → expiry_ts (24h after SL)
        self.trade_count = 0
        self.wins  = 0
        self.losses = 0
        self.total_pnl_sol = 0.0
        self.paper_mode = False   # LIVE MODE
        self.running = False

        # Circuit breaker — daily loss tracking
        self.daily_pnl_sol = 0.0           # cumulative PnL today
        self.daily_start_balance = 0.0     # balance at start of day
        self.daily_reset_ts = 0.0          # when to reset the daily counter
        self.circuit_breaker_until = 0.0   # bot paused until this timestamp

        # Balance snapshot timing
        self._last_balance_snapshot_ts = 0.0

        # Load persisted stats if available
        self._load_stats()

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [MEME-TRADER] {msg}")

    # ------------------------------------------------------------------
    # ULTRA_EARLY classification — the jackpot tier
    # ------------------------------------------------------------------
    @staticmethod
    def is_ultra_early(token) -> bool:
        """
        ULTRA_EARLY = our highest-conviction tier.

        Conditions:
            1. Liquidity sourced from Jupiter probe (DexScreener still stale)
               → token is < 5 min old, freshly migrated from pump.fun
            2. 2+ alpha wallets bought simultaneously
               → multi-whale validation, very strong signal

        Tokens that qualify get:
            - Bigger position size (0.025 SOL)
            - NO timeout (ride until trailing or SL)
            - Loose trailing (-12%) to capture moonshots
            - TP tiers at +100%, +300%, +1000% (lock profits progressively)
            - Loose SL (-25%) to allow variance for asymmetric upside
        """
        if not getattr(token, 'copy_trade', False):
            return False
        if not getattr(token, 'liquidity_from_jupiter', False):
            return False
        if getattr(token, 'wallet_hit_count', 0) < 2:
            return False
        return True

    # ------------------------------------------------------------------
    # Stats persistence — survives bot restarts
    # ------------------------------------------------------------------
    def _load_stats(self):
        """Load persisted stats from STATS_FILE if it exists."""
        import json, os
        if not os.path.exists(STATS_FILE):
            return
        try:
            with open(STATS_FILE, "r") as f:
                d = json.load(f)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.trade_count = d.get("total_trades", 0)
            self.total_pnl_sol = d.get("total_pnl_sol", 0.0)
            self.daily_pnl_sol = d.get("daily_pnl_sol", 0.0)
            self.daily_start_balance = d.get("daily_start_balance", 0.0)
            self.daily_reset_ts = d.get("daily_reset_ts", 0.0)
            self.circuit_breaker_until = d.get("circuit_breaker_until", 0.0)
            self.log(f"📂 Stats loaded — {self.wins}W/{self.losses}L over {self.trade_count} trades")
        except Exception as e:
            self.log(f"⚠️ Stats load error (starting fresh): {e}")

    def _save_stats(self):
        """Persist stats to STATS_FILE so we survive restarts."""
        import json
        try:
            data = {
                "wins": self.wins,
                "losses": self.losses,
                "total_trades": self.trade_count,
                "total_pnl_sol": self.total_pnl_sol,
                "daily_pnl_sol": self.daily_pnl_sol,
                "daily_start_balance": self.daily_start_balance,
                "daily_reset_ts": self.daily_reset_ts,
                "circuit_breaker_until": self.circuit_breaker_until,
                "last_updated": time.time(),
                "last_updated_human": datetime.now().isoformat(),
            }
            with open(STATS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log(f"⚠️ Stats save error: {e}")

    def _append_trade_log(self, entry: dict):
        """Append a trade record to TRADES_LOG_FILE (last 200 trades kept)."""
        import json, os
        history = []
        if os.path.exists(TRADES_LOG_FILE):
            try:
                with open(TRADES_LOG_FILE) as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append(entry)
        history = history[-200:]   # bounded
        try:
            with open(TRADES_LOG_FILE, "w") as f:
                json.dump(history, f, indent=2)
        except Exception:
            pass

    async def _snapshot_balance(self):
        """Append a balance snapshot to history (for dashboard chart). Throttled."""
        import json, os
        now = time.time()
        if now - self._last_balance_snapshot_ts < BALANCE_SNAPSHOT_INTERVAL:
            return
        try:
            bal = await wallet.get_sol_balance(self.pubkey)
        except Exception:
            return
        history = []
        if os.path.exists(BALANCE_HISTORY_FILE):
            try:
                with open(BALANCE_HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append({
            "ts": now,
            "iso": datetime.now().isoformat(),
            "balance_sol": bal,
            "wins": self.wins,
            "losses": self.losses,
            "trades": self.trade_count,
            "active_trades": len(self.active_trades),
        })
        # Keep last 1000 entries (~20 days at 30min interval)
        history = history[-1000:]
        try:
            with open(BALANCE_HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
            self._last_balance_snapshot_ts = now
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Circuit breaker — pause bot 24h if intraday loss too big
    # ------------------------------------------------------------------
    async def _check_circuit_breaker(self) -> bool:
        """Returns True if bot is currently paused by circuit breaker."""
        now = time.time()

        # Still in paused window?
        if now < self.circuit_breaker_until:
            remaining_h = (self.circuit_breaker_until - now) / 3600
            self.log(f"🛑 CIRCUIT BREAKER active — {remaining_h:.1f}h remaining")
            return True

        # Reset daily counter every 24h
        if now > self.daily_reset_ts:
            try:
                bal = await wallet.get_sol_balance(self.pubkey)
                self.daily_start_balance = bal
            except Exception:
                pass
            self.daily_pnl_sol = 0.0
            self.daily_reset_ts = now + 86400
            self._save_stats()

        # Check if intraday loss triggers breaker
        if self.daily_start_balance > 0:
            daily_pct = (self.daily_pnl_sol / self.daily_start_balance) * 100
            if daily_pct <= DAILY_CIRCUIT_BREAKER_PCT:
                self.circuit_breaker_until = now + CIRCUIT_BREAKER_PAUSE_SEC
                self._save_stats()
                self.log(
                    f"🚨 CIRCUIT BREAKER TRIGGERED — {daily_pct:.1f}% intraday "
                    f"(daily PnL: {self.daily_pnl_sol:+.4f} SOL). "
                    f"Bot paused 24h."
                )
                return True
        return False

    # ------------------------------------------------------------------
    async def init(self):
        if not self.keypair:
            kp = wallet.load_keypair()
            self.keypair = kp
            self.pubkey  = str(kp.pubkey())
        self.log(f"Wallet: {self.pubkey}")
        bal = await wallet.get_sol_balance(self.pubkey)
        self.log(f"Balance: {bal:.6f} SOL")
        self.log(f"Mode: {'PAPER TRADE' if self.paper_mode else '⚠️  LIVE TRADE'}")
        self.log(f"Position: {MEME_MAX_POSITION_SOL} SOL | TP: +{MEME_TAKE_PROFIT_PCT*100:.0f}% "
                 f"| SL: -{MEME_STOP_LOSS_PCT*100:.0f}% | Max trades: {MAX_CONCURRENT_TRADES}")
        self.log(f"Partial exit: 50% sold at +{PARTIAL_SELL_TRIGGER*100:.0f}%"
                 f" | Trailing: activates at +{TRAILING_ACTIVATE_PCT*100:.0f}%")

    # ------------------------------------------------------------------
    async def validate_entry(self, token: MemeCoin, score: dict) -> dict:
        result = {"ok": False, "reason": "", "rug": None}
        moon_mode        = getattr(token, 'copy_trade',   False)
        new_listing_mode = getattr(token, 'new_listing',  False)

        # ── Auto SL blacklist — token that recently hit a SL ──────────────
        # Avoid re-entering on a token in distribution / downtrend.
        sl_expiry = self.sl_blacklist.get(token.address, 0)
        if time.time() < sl_expiry:
            remaining_h = int((sl_expiry - time.time()) / 3600) + 1
            result["reason"] = f"Auto blacklist (recent SL) — {remaining_h}h remaining"
            return result
        elif sl_expiry > 0:
            del self.sl_blacklist[token.address]  # expired, clean up

        # ── Universal blacklist — stablecoins / base tokens ───────────────
        # DexScreener returns BOTH tokens of a pair — without this filter,
        # copying a wallet that buys TOKEN/USDC would buy USDC itself.
        if token.address in BLACKLISTED_TOKEN_ADDRESSES:
            result["reason"] = f"Blacklisted token (stablecoin/base): {token.symbol}"
            return result
        # Price heuristic: a stablecoin always sits at ~$1 ± 5%
        if 0.95 <= token.price_usd <= 1.05 and token.market_cap > 500_000_000:
            result["reason"] = f"Likely stablecoin (price ${token.price_usd:.4f}, mcap ${token.market_cap/1e9:.1f}B)"
            return result

        # ── NEW LISTING MODE (pump.fun → Raydium migration < 20min) ───────
        # Token just listed on Raydium: 2x-10x potential in the next 30min.
        # No 1h data available → criteria adapted.
        # STRICT rug check (new listings are common rug terrain).
        if new_listing_mode and not moon_mode:
            if token.price_usd <= 0 or token.price_usd < 1e-10:
                result["reason"] = "[NEW] Price unavailable"
                return result
            if token.liquidity_usd < 15_000:
                result["reason"] = f"[NEW] Liquidity too low: ${token.liquidity_usd:.0f}"
                return result
            total_txns = token.txns_1h_buys + token.txns_1h_sells
            if total_txns > 10:
                buy_ratio = token.txns_1h_buys / total_txns
                if buy_ratio < 0.55:
                    result["reason"] = f"[NEW] Sell pressure: {buy_ratio*100:.0f}% buys (min 55%)"
                    return result
            if token.address in self.active_trades:
                result["reason"] = "Already in position on this token"
                return result
            cooldown_until = self.trade_cooldowns.get(token.address, 0)
            if time.time() < cooldown_until:
                remaining_min = int((cooldown_until - time.time()) / 60)
                result["reason"] = f"Cooldown active — {remaining_min}min remaining"
                return result
            if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
                result["reason"] = f"Max {MAX_CONCURRENT_TRADES} simultaneous trades reached"
                return result
            # Strict rug check — timeout = skip (not enough confidence without data)
            self.log(f"[NEW] 🆕 Rug check {token.symbol} (pump.fun→Raydium migration)...")
            try:
                rug = await asyncio.wait_for(self.rug_checker.check(token.address), timeout=8.0)
            except asyncio.TimeoutError:
                result["reason"] = "[NEW] Rugcheck timeout — skip (new listing caution)"
                return result
            result["rug"] = rug
            risks_str = ', '.join(rug.get('risks', [])[:3]) or 'none'
            self.log(f"  [NEW] Score: {rug['score']} | LP locked: {rug['lp_locked']} | Risks: {risks_str}")
            critical = ["Freeze Authority still enabled", "Mint Authority still enabled", "Honeypot"]
            if any(r in rug.get("risks", []) for r in critical):
                result["reason"] = "[NEW] RUG CRITICAL — skip"
                return result
            if rug["score"] > 300:  # strict threshold = same as normal mode
                result["reason"] = f"[NEW] RUG RISK: {rug['score']}"
                return result
            result["ok"] = True
            return result

        # ── MOON MODE (copy trade): MULTI-WALLET REQUIRED ──────────────────
        # Strategic redesign 2026-05-25: single-wallet MOON was generating
        # mass losses (mostly MEV/scalping copies that go to zero).
        # New rule: require 2+ wallet hits OR ultra-early Jupiter validation.
        if moon_mode:
            ultra_early = self.is_ultra_early(token)
            wallet_hits = getattr(token, 'wallet_hit_count', 0)
            if not ultra_early and wallet_hits < 2:
                result["reason"] = (
                    f"[MOON] Single-wallet signal (hits={wallet_hits}) "
                    f"— rejected. Require 2+ wallets OR ultra-early."
                )
                return result
            if token.price_usd <= 0 or token.price_usd < 1e-10:
                # 1e-10 = price below DexScreener precision (displays $0.00000000)
                # monitor_and_sell can't compute a correct PnL → entry refused
                result["reason"] = f"[MOON] Price too small or zero: ${token.price_usd:.2e}"
                return result
            if token.liquidity_usd < 5_000:   # minimum threshold, anti-honeypot
                result["reason"] = f"[MOON] Liquidity too low: ${token.liquidity_usd:.0f}"
                return result
            # Volume filter — strict only for DexScreener-sourced tokens (indexed >5min).
            # For Jupiter-only tokens (freshly migrated, DexScreener stale):
            #   - 1h volume CANNOT exist yet (token is < 5 min old)
            #   - Skip this check entirely; rely on:
            #       • wallet signal (alpha bought = real interest)
            #       • rug check (catches honeypots / bad mint authorities)
            #       • active dump filter (-15% 5min already triggers skip)
            #       • late entry filter (+60% 5min already triggers skip)
            if not getattr(token, 'liquidity_from_jupiter', False):
                if token.volume_1h < 25_000:
                    result["reason"] = f"[MOON] Volume 1h too low: ${token.volume_1h:.0f} — dead token or too early"
                    return result
            if token.age_hours > MOON_MAX_AGE_HOURS:
                result["reason"] = f"[MOON] Token too old: {token.age_hours:.0f}h (max {MOON_MAX_AGE_HOURS}h)"
                return result
            # Late entry filter: if already +60% in 5min, we'd enter at the local peak.
            # The whale bought, copycats followed, the wave is gone.
            # 60% threshold (not 40%) because big MOON pumps can keep going past an initial spike.
            if token.price_change_5m > 60:
                result["reason"] = f"[MOON] Late entry: +{token.price_change_5m:.0f}% in 5min — likely local peak"
                return result
            # Pump-already-done filter: +500%+ in 1h = whale bought way before detection.
            # SCRIBBLE: +1605% in 1h, only +14.9% in 5min → top already reached → SL in 68s.
            # We'd copy the wallet AFTER the wave, not during. Skip.
            if token.price_change_1h > 500:
                result["reason"] = f"[MOON] Pump already done: +{token.price_change_1h:.0f}% in 1h — copy too late"
                return result
            # Active 5min dump filter
            if token.price_change_5m < -15:
                result["reason"] = f"[MOON] Active dump: {token.price_change_5m:.0f}% in 5min — whale already exited"
                return result
            # Negative 1h trend filter: if token's been falling for 1h, the whale bought
            # well before the signal. Tendies: -18.4% 1h + +13% 5min bounce = dead cat → SL -15.8%.
            if token.price_change_1h < -15:
                result["reason"] = f"[MOON] Downtrend 1h: {token.price_change_1h:.0f}% — copy too late"
                return result
            if token.address in self.active_trades:
                result["reason"] = "Already in position on this token"
                return result
            cooldown_until = self.trade_cooldowns.get(token.address, 0)
            if time.time() < cooldown_until:
                remaining_min = int((cooldown_until - time.time()) / 60)
                result["reason"] = f"Cooldown active — {remaining_min}min remaining"
                return result
            if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
                result["reason"] = f"Max {MAX_CONCURRENT_TRADES} simultaneous trades reached"
                return result

            # ══ ON-CHAIN SAFETY GATES (fast, reliable, run BEFORE rugcheck) ══
            # These two catch the honeypots/rugs that rugcheck.xyz misses on
            # fresh tokens — the exact -100% losses we saw in the trade log.

            # Gate 1: Mint/Freeze authority (1 RPC call, ~0.5s)
            try:
                auth = await asyncio.wait_for(
                    wallet.check_mint_authority(token.address), timeout=5.0
                )
            except asyncio.TimeoutError:
                auth = {"safe": False, "checked": False, "reason": "auth check timeout"}
            if auth.get("checked") and not auth.get("safe"):
                self.log(f"  [MOON] ⛔ {token.symbol}: {auth['reason']}")
                result["reason"] = f"[MOON] {auth['reason']}"
                return result

            # Gate 2: Honeypot round-trip simulation (2 Jupiter calls, ~1-2s)
            try:
                hp = await asyncio.wait_for(
                    jupiter.simulate_round_trip(token.address, probe_amount_sol=0.01),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                hp = {"safe": False, "reason": "honeypot sim timeout"}
            if not hp.get("safe"):
                self.log(f"  [MOON] 🍯 {token.symbol}: {hp['reason']}")
                result["reason"] = f"[MOON] Honeypot/exit risk: {hp['reason']}"
                return result
            self.log(
                f"  [MOON] ✅ Round-trip clean ({token.symbol}): "
                f"loss {hp['round_trip_loss']*100:.1f}%, "
                f"sell impact {hp['sell_impact']:.1f}%"
            )

            self.log(f"[MOON] 🌙 Rug check {token.symbol} (copied from alpha wallet)...")
            try:
                # Hard deadline 8s — a slow rug check must not block the scan loop
                rug = await asyncio.wait_for(self.rug_checker.check(token.address), timeout=8.0)
            except asyncio.TimeoutError:
                self.log(f"  [MOON] Rugcheck timeout ({token.symbol}) — entering anyway (whale trust)")
                rug = {"safe": True, "score": 0, "risks": [], "lp_locked": False, "top10_pct": 0, "error": "Timeout"}
            result["rug"] = rug
            rug_err = rug.get("error")
            if rug_err and rug_err != "Timeout":
                # 404 = token not yet indexed on rugcheck (often very recent)
                # Other errors = API down / network
                # In both cases: default score = 999 (no real data)
                # → we DON'T enter on default score 999, too risky
                not_indexed = "not found" in str(rug_err).lower() or "404" in str(rug_err)
                if not_indexed:
                    self.log(f"  [MOON] Token not indexed on rugcheck — skip (unknown score)")
                    result["reason"] = "[MOON] Rugcheck: token not indexed — skip"
                    return result
                else:
                    # Other API error (network timeout, etc.) → whale trust
                    self.log(f"  [MOON] Rugcheck error: {rug_err} — entering anyway (whale trust)")
            else:
                risks_str = ', '.join(rug.get('risks', [])[:3]) or 'none'
                self.log(f"  [MOON] Score: {rug['score']} | LP locked: {rug['lp_locked']} | Risks: {risks_str}")
                critical = ["Freeze Authority still enabled", "Mint Authority still enabled", "Honeypot"]
                if any(r in rug.get("risks", []) for r in critical):
                    result["reason"] = "[MOON] RUG CRITICAL — skip"
                    return result
                # Multi-wallet boost: 2+ wallets = higher confidence → raised rug threshold
                # Reasoning: if multiple independent whales buy at the same time,
                # the probability of a coordinated rug is much lower.
                wallet_hits = getattr(token, 'wallet_hit_count', 0)
                max_rug_score = 600 if wallet_hits >= 2 else 450
                if wallet_hits >= 2:
                    self.log(f"  [MOON] ⚡ Strong signal: {wallet_hits} wallets bought — rug threshold raised to {max_rug_score}")
                if rug["score"] > max_rug_score:
                    result["reason"] = f"[MOON] RUG RISK too high: {rug['score']}"
                    return result
            result["ok"] = True
            return result

        # ── NORMAL MODE (scanner): DISABLED 2026-05-25 ────────────────────
        # NORMAL was 1W/4L over the test period. Scanner finds tokens that
        # have already pumped — by the time we arrive, the local top is in.
        # We focus 100% on copy-trade MOON with multi-wallet validation.
        # Re-enable only after we can prove an edge in paper mode.
        result["reason"] = "[NORMAL] Scanner mode disabled — focus on multi-wallet MOON only"
        return result

        # (legacy code below — kept for reference, never reached)
        if token.price_usd < 1e-10:
            result["reason"] = f"Price too small: ${token.price_usd:.2e} (DexScreener precision insufficient)"
            return result
        if token.price_change_1h < ENTRY_PUMP_1H_MIN:
            result["reason"] = f"Insufficient 1h pump: {token.price_change_1h:.1f}%"
            return result
        if token.price_change_1h > ENTRY_PUMP_1H_MAX:
            result["reason"] = f"Already too pumped: {token.price_change_1h:.1f}%"
            return result
        if abs(token.price_change_6h) > ENTRY_PUMP_6H_MAX:
            result["reason"] = f"6h pump too high: {token.price_change_6h:.1f}%"
            return result

        total_txns = token.txns_1h_buys + token.txns_1h_sells
        if total_txns > 0:
            buy_ratio = token.txns_1h_buys / total_txns
            if buy_ratio < ENTRY_BUY_RATIO_MIN:
                result["reason"] = f"Too many sells: {buy_ratio*100:.0f}% buys"
                return result

        if token.age_hours < ENTRY_AGE_MIN_HOURS:
            result["reason"] = f"Token too recent: {token.age_hours:.1f}h"
            return result
        if token.age_hours > ENTRY_AGE_MAX_HOURS:
            result["reason"] = f"Token too old: {token.age_hours:.1f}h"
            return result
        # Scanner active dump filter: -20% in 5min = distribution in progress.
        # BABYTROLL: -22.1% 5min at entry → gap-rug -35.8% (SL skipped).
        # If the token is already falling fast, the 1h pump is done — we'd enter the dump.
        if hasattr(token, 'price_change_5m') and token.price_change_5m < -20:
            result["reason"] = f"Active dump: {token.price_change_5m:.0f}% in 5min — distribution in progress"
            return result
        if token.liquidity_usd < ENTRY_MIN_LIQUIDITY:
            result["reason"] = f"Low liquidity: ${token.liquidity_usd/1000:.0f}K"
            return result
        # Pump-exhausted filter: if volume >> liquidity, everyone's already in
        if token.liquidity_usd > 0:
            vol_liq = token.volume_1h / token.liquidity_usd
            if vol_liq > ENTRY_MAX_VOL_LIQ_RATIO:
                result["reason"] = f"Pump exhausted: Vol/Liq {vol_liq:.1f}× (max {ENTRY_MAX_VOL_LIQ_RATIO}×)"
                return result
        if score["points"] < ENTRY_MIN_SCORE:
            result["reason"] = f"Score too low: {score['points']}"
            return result
        if token.address in self.active_trades:
            result["reason"] = "Already in position on this token"
            return result

        # 3h cooldown after exit
        cooldown_until = self.trade_cooldowns.get(token.address, 0)
        if time.time() < cooldown_until:
            remaining_min = int((cooldown_until - time.time()) / 60)
            result["reason"] = f"Cooldown active — {remaining_min}min remaining"
            return result

        if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
            result["reason"] = f"Max {MAX_CONCURRENT_TRADES} simultaneous trades reached"
            return result

        # Rug check — hard deadline 12s to not block the scan loop
        self.log(f"Rug check {token.symbol}...")
        try:
            rug = await asyncio.wait_for(self.rug_checker.check(token.address), timeout=12.0)
        except asyncio.TimeoutError:
            self.log(f"  Rugcheck timeout ({token.symbol}) — skip (caution)")
            result["reason"] = f"Rugcheck timeout — skip"
            return result
        result["rug"] = rug

        if rug["error"]:
            self.log(f"  Rugcheck error: {rug['error']} — caution")
        else:
            self.log(f"  Score: {rug['score']} | LP locked: {rug['lp_locked']} | Top10: {rug['top10_pct']:.1f}%")
            if rug["risks"]:
                self.log(f"  Risks: {', '.join(rug['risks'][:3])}")
            if not rug["safe"]:
                result["reason"] = f"RUG RISK — score: {rug['score']}"
                return result
            if rug["top10_pct"] > 70:
                result["reason"] = f"Top 10 too concentrated: {rug['top10_pct']:.1f}%"
                return result

            # Blacklist specific risks that often precede a rug
            rug_red_flags = [
                "Low amount of LP Providers",
                "Single LP Provider",
                "Mutable metadata",
                "Large Amount of LP Unlocked",
            ]
            for flag in rug_red_flags:
                if flag in rug["risks"]:
                    result["reason"] = f"RED FLAG: {flag}"
                    return result

        result["ok"] = True
        return result

    # ------------------------------------------------------------------
    def format_signal(self, token: MemeCoin, score: dict, rug: dict) -> str:
        self.trade_count += 1
        if rug:
            rug_err = rug.get("error")
            if rug_err and rug_err != "Timeout":
                rug_score = "ERR"   # API error (not a real score) — don't display 999
            else:
                rug_score = rug.get("score", "?")
        else:
            rug_score = "?"
        lp_str           = "✅" if rug and rug.get("lp_locked") else "❓"
        moon_mode        = getattr(token, 'copy_trade',  False)
        new_listing_mode = getattr(token, 'new_listing', False)
        wallet_hits      = getattr(token, 'wallet_hit_count', 0)
        if moon_mode:
            source_tag = "🌙 COPY TRADE — MOON MODE"
            if wallet_hits >= 2:
                source_tag = f"🔥 COPY TRADE x{wallet_hits} WALLETS — MOON MODE"
            strat_str  = f"Trailing -{MOON_TRAILING_DISTANCE*100:.0f}% from peak | No fixed TP | Ride until drop"
        elif new_listing_mode:
            source_tag = "🆕 NEW RAYDIUM LISTING — MOON MODE"
            strat_str  = f"Trailing -{MOON_TRAILING_DISTANCE*100:.0f}% from peak | No fixed TP | Ride until drop"
        else:
            source_tag = "📊 SCANNER — NORMAL MODE"
            strat_str  = f"50% exit at +{PARTIAL_SELL_TRIGGER*100:.0f}% → trailing -{TRAILING_DISTANCE_PCT*100:.0f}% | TP +{MEME_TAKE_PROFIT_PCT*100:.0f}%"
        pump5_str = f" | 5min: {token.price_change_5m:+.1f}%" if token.price_change_5m else ""
        return f"""
{'='*58}
🚀 MEME TRADE #{self.trade_count} — {'PAPER' if self.paper_mode else 'LIVE'} | {source_tag}
Token     : {token.symbol} ({token.name})
Address   : {token.address}
Price     : ${token.price_usd:.8f}
Pump 1h   : {token.price_change_1h:+.1f}%{pump5_str}
Liquidity : ${token.liquidity_usd/1000:.0f}K  |  Vol 1h: ${token.volume_1h/1000:.0f}K
Age       : {token.age_hours:.1f}h
Rug score : {rug_score}/1000 | LP locked: {lp_str}
Signals   : {' | '.join(score['flags']) if score['flags'] else '-'}
Position  : {MEME_MAX_POSITION_SOL} SOL
Strategy  : {strat_str}
SL        : -{MEME_STOP_LOSS_PCT*100:.0f}%
Chart     : https://dexscreener.com/solana/{token.address}
{'='*58}"""

    # ------------------------------------------------------------------
    async def execute_buy(self, token: MemeCoin) -> bool:
        if self.paper_mode:
            self.log(f"[PAPER] BUY {token.symbol} @ ${token.price_usd:.8f}")
            return True

        # MOON (copy trade / new listing): fast pump → 500 bps slippage to avoid
        # "Slippage exceeded" errors that force a retry and make us buy into the dump.
        # QuantumCat: 150 bps → 2 slippage failures → bought at -49% → SL -48%.
        is_moon = getattr(token, 'copy_trade', False) or getattr(token, 'new_listing', False)
        buy_slippage = 500 if is_moon else 150

        # Dynamic position size — tier base × wallet-quality multiplier (#7)
        # ULTRA_EARLY = multi-wallet + Jupiter-fresh = highest conviction
        # Standard MOON = multi-wallet but not ultra-early = smaller bet
        if self.is_ultra_early(token):
            base_sol = ULTRA_EARLY_POSITION_SOL
            tier = "🚀 ULTRA_EARLY"
        else:
            base_sol = MEME_MAX_POSITION_SOL
            tier = "🌙 STANDARD_MOON"

        # #7 — scale by the best contributing wallet's profit-quality score [0.5, 1.5]
        source_wallets = getattr(token, 'source_wallets', []) or []
        quality = 1.0
        if source_wallets:
            quality = max(self.scanner.wallet_quality(w) for w in source_wallets)
        position_sol = round(base_sol * quality, 4)
        # Never exceed 25% of current capital on a single trade (safety)
        # (uses last known balance; falls back to base if unknown)
        wallet_hits = getattr(token, 'wallet_hit_count', 0)
        q_tag = f" ×{quality:.2f} quality" if abs(quality - 1.0) > 0.01 else ""
        self.log(f"  {tier} {position_sol} SOL ({wallet_hits} wallets{q_tag})")

        position_lamports = int(position_sol * config.LAMPORTS_PER_SOL)
        try:
            order = await jupiter.get_quote(
                config.SOL_MINT, token.address, position_lamports,
                slippage_bps=buy_slippage, taker=self.pubkey,
            )
            if not order:
                self.log(f"❌ BUY {token.symbol}: get_quote returned empty")
                return False
            # Check if Jupiter returned an error in the body
            if "error" in order or "code" in order:
                self.log(f"❌ BUY {token.symbol}: Jupiter error — {order.get('error') or order.get('message', order)}")
                return False
            swap_tx = order.get("transaction") or order.get("swapTransaction")
            if not swap_tx:
                self.log(f"❌ BUY {token.symbol}: no transaction in quote — {list(order.keys())}")
                return False
            signed_tx = wallet.sign_transaction(swap_tx, self.keypair)
            result = await jupiter.execute_swap(signed_tx, request_id=order.get("requestId"))
            if result and result.get("status", "").lower() == "success":
                self.log(f"✅ BUY {token.symbol} confirmed: {result.get('signature','')[:20]}...")
                return True
            else:
                self.log(f"❌ BUY {token.symbol} failed: status={result.get('status') if result else 'None'} | {result}")
                return False
        except Exception as e:
            self.log(f"BUY error {token.symbol}: [{type(e).__name__}] {e or '(empty message)'}")
            return False

    # ------------------------------------------------------------------
    async def execute_sell(self, token: MemeCoin, ratio: float = 1.0) -> bool:
        """Sell `ratio` of the position (1.0 = all, 0.5 = half)."""
        if self.paper_mode:
            pct = int(ratio * 100)
            self.log(f"[PAPER] SELL {pct}% {token.symbol}")
            return True
        try:
            token_balance = await wallet.get_token_balance(self.pubkey, token.address)
            if token_balance <= 0:
                self.log(f"No {token.symbol} balance to sell")
                return False

            amount_to_sell = int(token_balance * ratio)
            if amount_to_sell <= 0:
                return False

            order = await jupiter.get_quote(
                token.address, config.SOL_MINT, amount_to_sell,
                slippage_bps=300, taker=self.pubkey,
            )
            if not order:
                return False
            swap_tx = order.get("transaction") or order.get("swapTransaction")
            if not swap_tx:
                return False
            signed_tx = wallet.sign_transaction(swap_tx, self.keypair)
            result = await jupiter.execute_swap(signed_tx, request_id=order.get("requestId"))
            if result and result.get("status", "").lower() == "success":
                pct = int(ratio * 100)
                self.log(f"✅ SELL {pct}% {token.symbol} confirmed")
                return True
        except Exception as e:
            self.log(f"SELL error: {e}")
        return False

    # ------------------------------------------------------------------
    async def _get_meme_price(self, token_address: str) -> float | None:
        try:
            # connect=3s: includes DNS lookup — prevents DNS dropouts from
            # stretching the monitoring loop to 10-20s per iteration instead of 5s
            timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"https://api.dexscreener.com/tokens/v1/solana/{token_address}",
                )
                if resp.status_code == 200:
                    data  = resp.json()
                    pairs = data if isinstance(data, list) else data.get("pairs", [])
                    if pairs:
                        price = float(pairs[0].get("priceUsd", 0) or 0)
                        return price if price > 0 else None
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    async def _sell_with_retry(self, token: MemeCoin, ratio: float = 1.0,
                                max_attempts: int = 5) -> bool:
        """
        Sell with 5 attempts and exponential backoff.
        Backoff [5, 10, 20, 30s] between attempts — covers Alchemy 429s
        (~30s rate limit) and brief DNS/network outages.

        Hard 90s deadline per attempt via asyncio.wait_for:
        - get_token_balance: ~3s (connect timeout)
        - jupiter get_quote: ~12s (1-2 tries)
        - execute_swap:      ~60s (1 try → Solana tx landing)
        → 90s = enough for one full attempt without freezing for hours if DNS down.

        Cap +421%: sell failed with Alchemy 429 after 3 attempts at 3s each.
        5 attempts + 30s final delay = max 2 min before giving up.
        """
        # Delays between attempts: 5s, 10s, 20s, 30s (covers Alchemy 429 cooldown)
        retry_delays = [5, 10, 20, 30]
        for attempt in range(1, max_attempts + 1):
            try:
                ok = await asyncio.wait_for(
                    self.execute_sell(token, ratio=ratio),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                self.log(
                    f"  ⚠️ SELL {token.symbol} timeout 90s "
                    f"(attempt {attempt}/{max_attempts}) — wallet check advised"
                )
                ok = False
            if ok:
                return True
            if attempt < max_attempts:
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                self.log(
                    f"  ⚠️ SELL {token.symbol} failed "
                    f"(attempt {attempt}/{max_attempts}) — retry in {delay}s"
                )
                await asyncio.sleep(delay)
        self.log(
            f"  🚨 SELL {token.symbol} IMPOSSIBLE after {max_attempts} attempts "
            f"— CHECK YOUR WALLET MANUALLY (tokens may still be present)"
        )
        return False

    # ------------------------------------------------------------------
    async def monitor_and_sell(self, token: MemeCoin, entry_price: float,
                               moon_mode: bool = False):
        """
        3 tier strategies :

        ULTRA_EARLY (jackpot tier):
            - NO timeout, loose trailing -12%, loose SL -25%
            - Partial TPs at +100%, +300%, +1000% (locks moonshot profits)
            - Designed to ride freshly-migrated multi-wallet tokens to 10x

        Standard MOON (copy trade, multi-wallet):
            - Existing strategy: trailing -8%, partial TPs +50%/+100%
            - 45 min timeout

        Normal (scanner): DISABLED — see validate_entry.
        """
        ultra_early = self.is_ultra_early(token)

        # Mode-specific parameters
        if ultra_early:
            trailing_dist = 0.12              # Loose -12% to let it run
            sl_price      = entry_price * (1 - ULTRA_EARLY_STOP_LOSS)
            tp_price      = float('inf')      # No fixed TP, partial TPs handle it
            partial_px    = float('inf')      # No NORMAL-style partial
            timeout_secs  = float('inf')      # NO TIMEOUT — ride forever
        elif moon_mode:
            trailing_dist = MOON_TRAILING_DISTANCE
            sl_price      = entry_price * (1 - MEME_STOP_LOSS_PCT)
            tp_price      = float('inf')
            partial_px    = float('inf')
            timeout_secs  = MEME_TIMEOUT_SECONDS
        else:
            trailing_dist = TRAILING_DISTANCE_PCT
            tp_price      = entry_price * (1 + MEME_TAKE_PROFIT_PCT)
            sl_price      = entry_price * (1 - MEME_STOP_LOSS_PCT)
            partial_px    = entry_price * (1 + PARTIAL_SELL_TRIGGER)
            timeout_secs  = MEME_TIMEOUT_SECONDS

        start_time    = time.time()

        partial_sold    = False
        trailing_active = False
        peak_price      = entry_price
        trailing_sl     = 0.0

        mode_tag = "🌙 MOON" if moon_mode else "📊 NORMAL"
        if moon_mode:
            self.log(
                f"Monitor {token.symbol} [{mode_tag}] | Entry ${entry_price:.8f} "
                f"| Trailing -{MOON_TRAILING_DISTANCE*100:.0f}% | SL ${sl_price:.8f} "
                f"| No fixed TP — ride until trailing"
            )
        else:
            self.log(
                f"Monitor {token.symbol} [{mode_tag}] | Entry ${entry_price:.8f} "
                f"| Partial +{PARTIAL_SELL_TRIGGER*100:.0f}% @ ${partial_px:.8f} "
                f"| TP ${tp_price:.8f} | SL ${sl_price:.8f}"
            )

        last_check = time.time()
        consecutive_fails = 0
        last_known_price: float | None = None  # last confirmed price (persists across iterations)
        last_price_ts: float = time.time()     # timestamp of last successful price
        timeout_log_ts: float = 0              # anti-spam timeout log
        blind_log_ts: float = 0                # anti-spam "price unavailable" log

        # MOON mode — partial take profits at +50% and +100%
        # Lesson from Cap +421%: never give back everything to trailing stop.
        # Lock 25% at +50% (you can't lose money on this trade anymore).
        # Lock another 25% at +100% (de-risked further).
        # Remaining 50% rides the trailing stop for the moonshot.
        moon_tp1_done = False   # 25% sold at +50%
        moon_tp2_done = False   # 25% sold at +100%

        # ULTRA_EARLY TPs — jackpot tier locks profits at exponential milestones
        # +100% → lock 30% (already doubled, take some off)
        # +300% → lock 30% more (4x, lock half the trade)
        # +1000% → lock 20% (10x, only 20% remains for the dream)
        # Remaining 20% rides the loose -12% trailing for the 50-100x dream
        ue_tp1_done = False
        ue_tp2_done = False
        ue_tp3_done = False

        while True:
            # ── Timeout: disabled if trailing active AND position profitable ──
            # SAM case: cut at +21.6% while trailing SL was managing exit.
            # If trailing_active=True and price > entry, let the trailing decide.
            elapsed_total = time.time() - start_time
            if elapsed_total >= timeout_secs:
                in_profit = last_known_price is not None and last_known_price > entry_price
                if trailing_active and in_profit:
                    if time.time() - timeout_log_ts > 60:  # log 1x/min max
                        pnl_now = ((last_known_price - entry_price) / entry_price) * 100
                        self.log(
                            f"  ⏱️ {token.symbol} {int(elapsed_total / 60)}min "
                            f"— trailing active at {pnl_now:+.1f}% — riding until trailing stop"
                        )
                        timeout_log_ts = time.time()
                else:
                    break  # real timeout → exit below

            try:
                current_price = await self._get_meme_price(token.address)
                if not current_price:
                    consecutive_fails += 1
                    blind_secs = int(time.time() - last_price_ts)
                    # Log every 15s to surface the problem without spamming
                    if time.time() - blind_log_ts > 15:
                        self.log(f"  ⚠️ {token.symbol}: price unavailable for {blind_secs}s")
                        blind_log_ts = time.time()
                    # Force sell if blind > 90s — YENJI: 15min without price → SL ignored → -20.7%
                    if blind_secs >= 90:
                        self.log(
                            f"  🚨 EMERGENCY SELL {token.symbol} — price unavailable {blind_secs}s"
                            f" (can't manage SL without price)"
                        )
                        ok = await self._sell_with_retry(token, ratio=1.0)
                        return None  # uncertain: we don't know if we're in profit or loss
                    await asyncio.sleep(5)
                    continue
                consecutive_fails = 0
                last_known_price = current_price  # cache for timeout check
                last_price_ts = time.time()       # timestamp of last successful price

                elapsed  = int(time.time() - start_time)
                pnl_pct  = ((current_price - entry_price) / entry_price) * 100

                # ── Peak update ──────────────────────────────────────────
                if current_price > peak_price:
                    peak_price = current_price
                    if trailing_active:
                        trailing_sl = peak_price * (1 - trailing_dist)

                # ── Progressive trailing (MOON / NEW LISTING only) ───────
                # Done after peak update so the log shows the correct value.
                # SL never moves backwards (new_sl > trailing_sl always checked).
                if moon_mode and trailing_active:
                    if pnl_pct >= 50:
                        new_dist = 0.05   # -5%: max securing past +50%
                    elif pnl_pct >= 25:
                        new_dist = 0.06   # -6%: tightening from +25%
                    else:
                        new_dist = MOON_TRAILING_DISTANCE  # -8%: normal zone < +25%
                    if new_dist < trailing_dist:  # never loosen
                        trailing_dist = new_dist
                        new_sl = peak_price * (1 - trailing_dist)
                        if new_sl > trailing_sl:
                            trailing_sl = new_sl
                            self.log(
                                f"  🔒 Trailing tightened → -{trailing_dist*100:.0f}% "
                                f"| SL: ${trailing_sl:.8f} (PnL {pnl_pct:+.1f}%)"
                            )

                # ── PARTIAL EXIT +20% (NORMAL mode only) ─────────────────
                if not moon_mode and not partial_sold and current_price >= partial_px:
                    self.log(f"  💰 PARTIAL EXIT {token.symbol} +{pnl_pct:.1f}% — selling 50%")
                    await self._sell_with_retry(token, ratio=PARTIAL_SELL_RATIO)
                    partial_sold = True
                    self.log(f"  ✅ 50% secured | remaining rides with trailing stop")

                # ── ULTRA_EARLY tiered TPs (jackpot mode) ──────────────────
                # Lock profits at exponential milestones to ride the moonshot
                if ultra_early:
                    if not ue_tp1_done and pnl_pct >= 100:
                        self.log(f"  🚀 UE TP1 {token.symbol} +{pnl_pct:.1f}% (doubled!) — locking 30%")
                        sold_ok = await self._sell_with_retry(token, ratio=0.30)
                        if sold_ok:
                            ue_tp1_done = True
                            self.log(f"  ✅ 30% locked at +100% | 70% riding for the moon")
                    if ue_tp1_done and not ue_tp2_done and pnl_pct >= 300:
                        # 30% of remaining 70% ≈ 0.43 ratio
                        self.log(f"  🚀 UE TP2 {token.symbol} +{pnl_pct:.1f}% (4x!) — locking 30% more")
                        sold_ok = await self._sell_with_retry(token, ratio=0.43)
                        if sold_ok:
                            ue_tp2_done = True
                            self.log(f"  ✅ Total 60% locked | 40% riding for 10x+")
                    if ue_tp2_done and not ue_tp3_done and pnl_pct >= 1000:
                        # 20% of remaining 40% ≈ 0.50 ratio
                        self.log(f"  🚀🚀 UE TP3 {token.symbol} +{pnl_pct:.1f}% (10x!!!) — locking 20%")
                        sold_ok = await self._sell_with_retry(token, ratio=0.50)
                        if sold_ok:
                            ue_tp3_done = True
                            self.log(f"  ✅ Total 80% locked | 20% riding for the dream")

                # ── STANDARD MOON TPs (multi-wallet, not ultra-early) ──────
                elif moon_mode and not moon_tp1_done and pnl_pct >= 50:
                    self.log(f"  🌙 MOON TP1 {token.symbol} +{pnl_pct:.1f}% — locking 25%")
                    sold_ok = await self._sell_with_retry(token, ratio=0.25)
                    if sold_ok:
                        moon_tp1_done = True
                        self.log(f"  ✅ 25% locked at +50% | 75% riding")
                if moon_mode and not ultra_early and moon_tp1_done and not moon_tp2_done and pnl_pct >= 100:
                    self.log(f"  🌙 MOON TP2 {token.symbol} +{pnl_pct:.1f}% — locking 25% more")
                    sold_ok = await self._sell_with_retry(token, ratio=0.333)
                    if sold_ok:
                        moon_tp2_done = True
                        self.log(f"  ✅ Total 50% locked | 50% moonshot riding")

                # ── TRAILING ACTIVATION ──────────────────────────────────
                if not trailing_active and pnl_pct >= TRAILING_ACTIVATE_PCT * 100:
                    trailing_active = True
                    trailing_sl = peak_price * (1 - trailing_dist)
                    trail_tag = f"-{trailing_dist*100:.0f}%"
                    self.log(f"  🔒 Trailing stop activated [{trail_tag}] | SL: ${trailing_sl:.8f}")

                trail_info = f" | Trail SL ${trailing_sl:.8f}" if trailing_active else ""
                if moon_mode and moon_tp2_done:
                    partial_info = " [50% locked]"
                elif moon_mode and moon_tp1_done:
                    partial_info = " [25% locked]"
                elif partial_sold:
                    partial_info = " [50% secured]"
                else:
                    partial_info = ""
                self.log(
                    f"  {token.symbol}: ${current_price:.8f} | PnL: {pnl_pct:+.1f}%"
                    f" | {elapsed}s{partial_info}{trail_info}"
                )

                # ── FINAL TP ─────────────────────────────────────────────
                if current_price >= tp_price:
                    remaining = 1.0
                    self.log(f"  🎯 TP HIT {token.symbol} +{pnl_pct:.1f}%")
                    sold = await self._sell_with_retry(token, ratio=remaining)
                    return True if sold else None  # None = sell failed, position uncertain

                # ── TRAILING STOP ────────────────────────────────────────
                if trailing_active and current_price <= trailing_sl:
                    peak_pnl = ((peak_price - entry_price) / entry_price) * 100
                    outcome_tag = "✅" if current_price > entry_price else "💀 GAP-RUG"
                    self.log(
                        f"  🔒 TRAILING STOP {token.symbol} {outcome_tag} | "
                        f"Peak +{peak_pnl:.1f}% → exited at {pnl_pct:+.1f}%"
                    )
                    remaining = 1.0
                    sold = await self._sell_with_retry(token, ratio=remaining)
                    if not sold:
                        return None  # sell failed, position uncertain
                    return current_price > entry_price or partial_sold

                # ── STOP LOSS ────────────────────────────────────────────
                if current_price <= sl_price:
                    self.log(f"  🛑 SL HIT {token.symbol} {pnl_pct:.1f}%")
                    remaining = 1.0
                    sold = await self._sell_with_retry(token, ratio=remaining)
                    if not sold:
                        return None  # sell failed, position uncertain
                    return partial_sold

            except Exception as e:
                self.log(f"  monitoring error: {e}")

            await asyncio.sleep(5)

        # Timeout — forced close (trailing inactive or position negative)
        elapsed_min = int((time.time() - start_time) / 60)
        self.log(f"  ⏱️ TIMEOUT {token.symbol} ({elapsed_min}min) — closing")
        remaining = 1.0 - PARTIAL_SELL_RATIO if partial_sold else 1.0
        sold = await self._sell_with_retry(token, ratio=remaining)
        if not sold:
            # Sell impossible (DNS/network) — position uncertain
            # Tokens may still be in the wallet
            return None
        # WIN if we exit in profit despite the timeout
        if last_known_price and last_known_price > entry_price:
            return True
        return partial_sold or None

    # ------------------------------------------------------------------
    async def _on_ws_buy(self, token_addr: str, wallet_addr: str,
                         whale_price: float | None = None):
        """
        WebSocket callback — called within seconds of an alpha wallet buying.

        Pipeline:
            1. Probe Jupiter to confirm token is tradeable (skip DexScreener wait)
            2. LATE-ENTRY GATE: compare current price to the whale's entry price.
               If we're already > +15% above where the whale got in, skip — we'd
               be buying their exit liquidity.
            3. Push to scanner.pending_copy WITH the Jupiter probe data
            4. The main scan loop picks it up: uses Jupiter liquidity if DexScreener stale

        whale_price: lamports of SOL the whale paid per raw token (from the parsed
        tx). The probe returns the same unit (in_amount lamports / out_amount raw
        tokens), so they're directly comparable.
        """
        # Probe Jupiter: is this token tradeable right now?
        try:
            probe = await jupiter.probe_token_tradeable(token_addr, probe_amount_sol=0.005)
        except Exception:
            probe = None

        # ── LATE-ENTRY GATE — only copy if we're close to the whale's price ──
        if probe and probe.get("tradeable") and whale_price and whale_price > 0:
            in_amt = probe.get("in_amount", 0)
            out_amt = probe.get("out_amount", 0)
            if in_amt > 0 and out_amt > 0:
                current_price = in_amt / out_amt  # lamports per raw token (same unit as whale_price)
                premium = (current_price - whale_price) / whale_price
                if premium > MAX_WHALE_PREMIUM:
                    self.log(
                        f"  ⏭️ [WS] Skip {token_addr[:8]}... — already +{premium*100:.0f}% "
                        f"above whale entry (max +{MAX_WHALE_PREMIUM*100:.0f}%)"
                    )
                    # Still track the signal for wallet stats, but don't push it as actionable
                    self.scanner.add_copy_signal(token_addr, wallet_addr, jupiter_probe=None)
                    return
                else:
                    self.log(
                        f"  🎯 [WS] {token_addr[:8]}... entry +{premium*100:.0f}% vs whale — within window"
                    )

        # Always push the signal (so wallet stats are tracked).
        # Pass the probe so the scanner can use it later (even None is fine).
        self.scanner.add_copy_signal(token_addr, wallet_addr, jupiter_probe=probe)

        if probe and probe.get("tradeable"):
            self.log(
                f"⚡ [WS-FAST] {wallet_addr[:8]}... → {token_addr[:8]}... "
                f"(tradeable via Jupiter, {probe.get('route_count')} DEXes, "
                f"impact {probe.get('price_impact', 0):.2f}%)"
            )

    # ------------------------------------------------------------------
    async def _wallet_poll_loop(self):
        """
        Fallback polling loop — runs every 30s in case WebSocket misses anything.

        WebSocket is the primary signal source (latency ~3-5s).
        This poll catches edge cases:
            - WS reconnect window (5-60s downtime)
            - Helius push delays during high load
            - Logs notification filter misses

        Results converge into the same scanner.pending_copy queue.
        """
        if not self.wallet_tracker or not self.wallet_tracker.ALPHA_WALLETS:
            return
        self.log("🐢 Fallback poll loop started (120s) — WS primary, this is the safety net")
        while self.running:
            try:
                # Look back 3 min to catch anything WS missed during reconnects
                token_wallets = await self.wallet_tracker.scan_all(since_minutes=3)
                for token_addr, wallet_list in token_wallets.items():
                    for w in wallet_list:
                        self.scanner.add_copy_signal(token_addr, w)
            except Exception:
                pass  # DNS/network — retry next cycle
            # 120s = enough to cover WS reconnect windows (~5-60s typical)
            # while staying well under Helius free tier 100K req/day quota.
            await asyncio.sleep(120)

    # ------------------------------------------------------------------
    async def run(self, scan_interval: int = 60):
        self.running = True

        # ── Retry init until the network is available ─────────────────────
        # [Errno 8] nodename nor servname = DNS dropout → the bot must not crash,
        # it must wait for the network to come back and resume automatically.
        retry = 0
        while True:
            try:
                await self.init()
                break
            except Exception as e:
                retry += 1
                wait = min(30 * retry, 300)   # 30s → 60s → 90s → ... max 5min
                err_short = str(e)[:80] or type(e).__name__
                self.log(
                    f"⚠️  Network unavailable (attempt {retry}) — "
                    f"retry in {wait}s | {err_short}"
                )
                await asyncio.sleep(wait)

        # Initial wallet performance snapshot (if any history)
        self.scanner.log_wallet_performance()
        self._last_wallet_summary_ts = time.time()

        self.log("=" * 58)
        self.log("MEME TRADER ACTIVE — CAPITAL MAXIMIZATION STRATEGY")
        self.log(f"Entry: 1h pump +{ENTRY_PUMP_1H_MIN}% → +{ENTRY_PUMP_1H_MAX}%")
        self.log(f"Rugcheck enabled | Max {MAX_CONCURRENT_TRADES} simultaneous trades")
        n_wallets = len(self.wallet_tracker.ALPHA_WALLETS)
        if n_wallets > 0:
            self.log(f"🔁 Copy trading: {n_wallets} alpha wallet(s)")
            self.log(f"   ⚡ WebSocket (primary)  — push-based, ~3s latency")
            self.log(f"   🐢 Poll fallback (30s) — safety net for WS reconnects")
            for w in self.wallet_tracker.ALPHA_WALLETS:
                self.log(f"   → {w[:8]}...{w[-4:]}")
            # Primary detection: Helius WebSocket
            asyncio.create_task(self.ws_monitor.run())
            # Fallback safety net (lower frequency now that WS is primary)
            asyncio.create_task(self._wallet_poll_loop())
        else:
            self.log("🔁 Copy trading: INACTIVE (add wallets in wallet_tracker.py)")
        self.log("=" * 58)

        while self.running:
            try:
                # Circuit breaker check — pause if intraday loss > -10%
                if await self._check_circuit_breaker():
                    await asyncio.sleep(300)  # re-check every 5min while paused
                    continue

                # Periodic wallet performance log (every 6h)
                if time.time() - self._last_wallet_summary_ts > 21600:
                    self.scanner.log_wallet_performance()
                    self.ws_monitor.print_stats()
                    self._last_wallet_summary_ts = time.time()

                # Balance snapshot for dashboard (every 30min)
                await self._snapshot_balance()

                # Clean expired entries from skip_cache
                now = time.time()
                self.skip_cache = {a: v for a, v in self.skip_cache.items() if v[0] > now}

                all_tokens = await self.scanner.fetch_trending()
                if not all_tokens:
                    await asyncio.sleep(scan_interval)
                    continue

                # Split by mode — priority: new_listing > copy_trade > normal
                # new_listing + copy_trade bypass filter_tokens (own criteria in validate_entry)
                new_listing_tokens = [
                    t for t in all_tokens
                    if getattr(t, 'new_listing', False) and not getattr(t, 'copy_trade', False)
                ]
                copy_tokens   = [t for t in all_tokens if getattr(t, 'copy_trade', False)]
                normal_tokens = [
                    t for t in all_tokens
                    if not getattr(t, 'copy_trade', False) and not getattr(t, 'new_listing', False)
                ]

                new_listing_opps = [(t, t.score()) for t in new_listing_tokens
                                    if t.address and t.price_usd > 0]
                copy_opps        = [(t, t.score()) for t in copy_tokens
                                    if t.address and t.price_usd > 0]
                normal_opps      = self.scanner.filter_tokens(normal_tokens)

                # New listings first — the 20min window closes fast
                opportunities = new_listing_opps + copy_opps + normal_opps

                for token, score in opportunities[:10]:
                    # Skip cache: if recently rejected, ignore silently
                    cached = self.skip_cache.get(token.address)
                    if cached and time.time() < cached[0]:
                        continue

                    validation = await self.validate_entry(token, score)
                    if not validation["ok"]:
                        reason = validation['reason']
                        self.log(f"Skip {token.symbol}: {reason}")
                        # Cache to avoid re-evaluating too soon
                        ttl = self._skip_ttl(reason)
                        if ttl > 0:
                            self.skip_cache[token.address] = (time.time() + ttl, reason)
                        continue

                    print(self.format_signal(token, score, validation.get("rug", {})))

                    entry_price = token.price_usd

                    # Capture balance BEFORE the buy — with MAX_CONCURRENT_TRADES=1,
                    # (final_balance - balance_before_buy) is the EXACT realized PnL.
                    try:
                        balance_before_buy = await wallet.get_sol_balance(self.pubkey)
                    except Exception:
                        balance_before_buy = None

                    bought = await self.execute_buy(token)
                    if not bought:
                        self.log(f"Buy {token.symbol} failed")
                        # 5 min cooldown to avoid re-entering on a dumping token
                        # (e.g. slippage exceeded during a pump → retry after reversal)
                        self.trade_cooldowns[token.address] = time.time() + 300
                        continue

                    self.active_trades[token.address] = {
                        "token": token,
                        "entry_price": entry_price,
                        "entry_time": time.time(),
                        "balance_before_buy": balance_before_buy,
                        "source_wallets": getattr(token, "source_wallets", []),
                    }

                    # Both new_listing and copy_trade use MOON behavior
                    # (trailing -8%, no partial exit, no fixed TP)
                    moon = getattr(token, 'copy_trade', False) or getattr(token, 'new_listing', False)
                    asyncio.create_task(self._handle_trade(token, entry_price, moon_mode=moon))
                    await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_msg = str(e)
                if "nodename nor servname" in err_msg or "Name or service not known" in err_msg \
                        or "ConnectError" in type(e).__name__:
                    self.log(f"⚠️  DNS/network unavailable — retry in {scan_interval}s")
                else:
                    self.log(f"Loop error [{type(e).__name__}]: {e}")

            await asyncio.sleep(scan_interval)

    # ------------------------------------------------------------------
    async def _handle_trade(self, token: MemeCoin, entry_price: float, moon_mode: bool = False):
        trade_info = self.active_trades.get(token.address, {})
        trade_start_ts = trade_info.get("entry_time", time.time())
        balance_before_buy = trade_info.get("balance_before_buy")
        source_wallets = trade_info.get("source_wallets", []) or []

        try:
            result = await self.monitor_and_sell(token, entry_price, moon_mode=moon_mode)

            # ── REAL PnL: balance_before_buy → final balance (concurrency=1) ──
            await asyncio.sleep(3)  # let on-chain settle
            try:
                final_balance = await wallet.get_sol_balance(self.pubkey)
            except Exception:
                final_balance = None
            if balance_before_buy is not None and final_balance is not None:
                pnl_sol = final_balance - balance_before_buy
            else:
                pnl_sol = None

            # ── Classify by REAL PnL, not just the monitor's exit reason ──
            # The monitor returns True/False/None based on exit logic, but the
            # ground truth is the realized SOL delta. A "trailing stop WIN" that
            # actually netted -8% is a LOSS. Trust the money.
            if result is None:
                result_tag = "UNCERTAIN"
                self.log(
                    f"⚠️ SELL UNCERTAIN {token.symbol} — check wallet, tokens may be unsold"
                )
            elif pnl_sol is not None:
                if pnl_sol > 0:
                    self.wins += 1
                    result_tag = "WIN"
                    self.log(f"✅ WIN {token.symbol} {pnl_sol:+.4f} SOL | {self.wins}W/{self.losses}L")
                else:
                    self.losses += 1
                    result_tag = "LOSS"
                    self.sl_blacklist[token.address] = time.time() + SL_BLACKLIST_HOURS * 3600
                    self.log(f"❌ LOSS {token.symbol} {pnl_sol:+.4f} SOL → blacklist {SL_BLACKLIST_HOURS}h | {self.wins}W/{self.losses}L")
            else:
                # Fallback to monitor's verdict if we couldn't read balance
                if result is True:
                    self.wins += 1
                    result_tag = "WIN"
                    self.log(f"✅ WIN {token.symbol} (PnL unknown) | {self.wins}W/{self.losses}L")
                else:
                    self.losses += 1
                    result_tag = "LOSS"
                    self.sl_blacklist[token.address] = time.time() + SL_BLACKLIST_HOURS * 3600
                    self.log(f"❌ LOSS {token.symbol} (PnL unknown) | {self.wins}W/{self.losses}L")

            # ── Feed wallet profit scoring (#5) — credit ALL contributing wallets ──
            if source_wallets and pnl_sol is not None:
                for w in source_wallets:
                    self.scanner.record_wallet_trade_result(w, pnl_sol)

            # ── Per-trade history log (dashboard) ──
            try:
                self._append_trade_log({
                    "ts_open": trade_start_ts,
                    "ts_close": time.time(),
                    "iso_close": datetime.now().isoformat(),
                    "symbol": token.symbol,
                    "address": token.address,
                    "mode": "ULTRA_EARLY" if self.is_ultra_early(token) else ("MOON" if moon_mode else "NORMAL"),
                    "entry_price": entry_price,
                    "result": result_tag,
                    "wallet_hits": getattr(token, "wallet_hit_count", 0),
                    "source_wallets": source_wallets,
                    "bal_before": balance_before_buy,
                    "bal_after": final_balance,
                    "pnl_sol": pnl_sol,
                })
            except Exception:
                pass

            # ── Daily PnL (now accurate with concurrency=1) ──
            try:
                if final_balance is not None and self.daily_start_balance > 0:
                    self.daily_pnl_sol = final_balance - self.daily_start_balance
            except Exception:
                pass

            self._save_stats()
        finally:
            self.active_trades.pop(token.address, None)
            self.trade_cooldowns[token.address] = time.time() + TRADE_COOLDOWN_SECONDS

    def _skip_ttl(self, reason: str) -> int:
        """Silent caching duration based on rejection reason."""
        if "6h pump too high"        in reason: return 1800  # 30 min — won't drop fast
        if "Token too old"           in reason: return 3600  # 1h    — irreversible
        if "RUG RISK"                in reason: return 1800  # 30 min — rug score doesn't change
        if "RED FLAG"                in reason: return 3600  # 1h    — red flag permanent
        if "RUG CRITICAL"            in reason: return 3600  # 1h    — rug permanent
        if "Already too pumped"      in reason: return 1200  # 20 min
        if "Pump exhausted"          in reason: return 1200  # 20 min
        if "Too many sells"          in reason: return 600   # 10 min
        if "Sell pressure"           in reason: return 300   # 5 min — can flip fast
        if "Liquidity too low"       in reason: return 300   # 5 min — can change fast
        if "Low liquidity"           in reason: return 300   # 5 min — can change fast
        if "[NEW]"                   in reason: return 120   # 2 min — new listing evolves fast
        if "Downtrend 1h"            in reason: return 600   # 10 min
        if "Active dump"             in reason: return 300   # 5 min
        if "Late entry"              in reason: return 300   # 5 min
        if "Pump already done"       in reason: return 1800  # 30 min
        if "Auto blacklist"          in reason: return 3600  # 1h — re-check after expiry
        return 0  # other reasons (cooldown, max trades, etc.) → no cache

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
async def main():
    trader = MemeTrader()
    trader.paper_mode = False
    try:
        await trader.run(scan_interval=30)
    except KeyboardInterrupt:
        trader.stop()


if __name__ == "__main__":
    asyncio.run(main())
