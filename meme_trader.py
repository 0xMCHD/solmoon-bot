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

# ---------------------------------------------------------------------------
# Position parameters
# ---------------------------------------------------------------------------
MEME_MAX_POSITION_SOL   = 0.08    # 0.08 SOL per trade — capital preservation
MEME_TAKE_PROFIT_PCT    = 0.40    # final TP +40%
MEME_STOP_LOSS_PCT      = 0.15    # SL -15%
MEME_TIMEOUT_SECONDS    = 2700    # 45 min max
MAX_CONCURRENT_TRADES   = 2       # max 2 positions — 36% capital exposed

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
class MemeTrader:
    """Memecoin bot with partial exit + trailing stop + 2 simultaneous trades."""

    def __init__(self, keypair=None, pubkey: str = None):
        self.keypair  = keypair
        self.pubkey   = pubkey
        self.wallet_tracker = WalletTracker(rpc_url=config.RPC_URL)
        self.scanner  = MemeScanner(wallet_tracker=self.wallet_tracker)
        self.rug_checker = RugChecker()
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

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [MEME-TRADER] {msg}")

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

        # ── MOON MODE (copy trade): relaxed filters ───────────────────────
        # We trust the whale — only rug check + position check are active.
        if moon_mode:
            if token.price_usd <= 0 or token.price_usd < 1e-10:
                # 1e-10 = price below DexScreener precision (displays $0.00000000)
                # monitor_and_sell can't compute a correct PnL → entry refused
                result["reason"] = f"[MOON] Price too small or zero: ${token.price_usd:.2e}"
                return result
            if token.liquidity_usd < 5_000:   # minimum threshold, anti-honeypot
                result["reason"] = f"[MOON] Liquidity too low: ${token.liquidity_usd:.0f}"
                return result
            # Minimum volume filter: Kirkslop had $6K vol/h → dead token, SL hit.
            # $25K minimum = enough liquidity for the copy trade to still be active.
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

        # ── NORMAL MODE (scanner): full filters ───────────────────────────
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

        # Dynamic position size based on signal strength (wallet_hit_count)
        # 3+ wallets simultaneously = exceptional signal → bet more
        # Scanner alone = weak signal → base position
        wallet_hits = getattr(token, 'wallet_hit_count', 0)
        if wallet_hits >= 3:
            position_sol = POSITION_BOOST_3W_SOL
            self.log(f"  💰 Position boosted to {position_sol} SOL ({wallet_hits} simultaneous wallets)")
        elif wallet_hits >= 2:
            position_sol = POSITION_BOOST_2W_SOL
            self.log(f"  💰 Position boosted to {position_sol} SOL ({wallet_hits} simultaneous wallets)")
        else:
            position_sol = POSITION_BASE_SOL

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
        Normal strategy : partial exit +20% → trailing -5% → TP +40% → SL -15%
        MOON strategy (copy trade): no partial exit, no fixed TP,
                                     trailing -8% only → ride the mega pump
        """
        # Mode-specific parameters
        trailing_dist = MOON_TRAILING_DISTANCE if moon_mode else TRAILING_DISTANCE_PCT
        tp_price      = float('inf') if moon_mode else entry_price * (1 + MEME_TAKE_PROFIT_PCT)
        sl_price      = entry_price * (1 - MEME_STOP_LOSS_PCT)
        partial_px    = float('inf') if moon_mode else entry_price * (1 + PARTIAL_SELL_TRIGGER)
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

        while True:
            # ── Timeout: disabled if trailing active AND position profitable ──
            # SAM case: cut at +21.6% while trailing SL was managing exit.
            # If trailing_active=True and price > entry, let the trailing decide.
            elapsed_total = time.time() - start_time
            if elapsed_total >= MEME_TIMEOUT_SECONDS:
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

                # ── TRAILING ACTIVATION ──────────────────────────────────
                if not trailing_active and pnl_pct >= TRAILING_ACTIVATE_PCT * 100:
                    trailing_active = True
                    trailing_sl = peak_price * (1 - trailing_dist)
                    trail_tag = f"-{trailing_dist*100:.0f}%"
                    self.log(f"  🔒 Trailing stop activated [{trail_tag}] | SL: ${trailing_sl:.8f}")

                trail_info = f" | Trail SL ${trailing_sl:.8f}" if trailing_active else ""
                partial_info = " [50% secured]" if partial_sold else ""
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
    async def _wallet_poll_loop(self):
        """
        Scan alpha wallets every 15s — decoupled from main scan (30s).

        WHY: with the main scan at 30s, we detect whale buys with a 30-90s
        average delay. During that window the token may already be +30-50%.
        Scanning wallets separately every 15s reduces detection delay to ~15s.

        Results are pushed into scanner.pending_copy via add_copy_signal().
        The main scan reads pending_copy and resolves via DexScreener — no double call.
        """
        if not self.wallet_tracker or not self.wallet_tracker.ALPHA_WALLETS:
            return
        self.log("⚡ Wallet poll loop started — copy trade detection every 15s")
        while self.running:
            try:
                token_wallets = await self.wallet_tracker.scan_all(since_minutes=2)
                for token_addr, wallet_list in token_wallets.items():
                    for w in wallet_list:
                        self.scanner.add_copy_signal(token_addr, w)
            except Exception:
                pass  # DNS/network — retry next cycle
            await asyncio.sleep(15)

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

        self.log("=" * 58)
        self.log("MEME TRADER ACTIVE — CAPITAL MAXIMIZATION STRATEGY")
        self.log(f"Entry: 1h pump +{ENTRY_PUMP_1H_MIN}% → +{ENTRY_PUMP_1H_MAX}%")
        self.log(f"Rugcheck enabled | Max {MAX_CONCURRENT_TRADES} simultaneous trades")
        n_wallets = len(self.wallet_tracker.ALPHA_WALLETS)
        if n_wallets > 0:
            self.log(f"🔁 Copy trading: {n_wallets} alpha wallet(s) | poll every 15s")
            for w in self.wallet_tracker.ALPHA_WALLETS:
                self.log(f"   → {w[:8]}...{w[-4:]}")
            # Launch wallet poll in background (15s — more reactive than 30s scan)
            asyncio.create_task(self._wallet_poll_loop())
        else:
            self.log("🔁 Copy trading: INACTIVE (add wallets in wallet_tracker.py)")
        self.log("=" * 58)

        while self.running:
            try:
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
        try:
            result = await self.monitor_and_sell(token, entry_price, moon_mode=moon_mode)
            if result is True:
                self.wins += 1
                self.log(f"✅ WIN {token.symbol} | {self.wins}W/{self.losses}L")
            elif result is False:
                self.losses += 1
                # 24h blacklist — avoid re-entering on a token in distribution
                self.sl_blacklist[token.address] = time.time() + SL_BLACKLIST_HOURS * 3600
                self.log(f"❌ LOSS {token.symbol} → blacklist {SL_BLACKLIST_HOURS}h | {self.wins}W/{self.losses}L")
            else:
                # None = sell failed (network/DNS) OR neutral timeout
                # Tokens may still be in the wallet → critical warning
                self.log(
                    f"⚠️ SELL UNCERTAIN {token.symbol} — "
                    f"check wallet, tokens may be unsold | {self.wins}W/{self.losses}L"
                )
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
