"""Solana memecoin scanner — detect real-time opportunities."""

import asyncio
import httpx
import time
from datetime import datetime

import config

# --- Safety filters ---
MIN_LIQUIDITY_USD = 30_000      # min $30K liquidity
MIN_VOLUME_24H_USD = 50_000     # min $50K 24h volume
MIN_PRICE_CHANGE_1H = 5.0       # min +5% in 1h
MAX_PRICE_CHANGE_1H = 200.0     # max +200% (too late if higher)
MIN_TOKEN_AGE_HOURS = 0.5       # at least 30min since launch
MAX_TOKEN_AGE_HOURS = 48        # at most 48h (we want early)
MIN_TXNS_1H = 50                # min 50 transactions in 1h
MAX_TOP10_HOLDERS_PCT = 80      # top 10 holders own <= 80% of supply

DEXSCREENER_API = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/pairs/solana"

# Stablecoins and base tokens — never tradeable (DexScreener copy-trade protection)
BLACKLISTED_TOKEN_ADDRESSES: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",       # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH (Wormhole)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",   # BTC (Wormhole)
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",   # stSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",    # bSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",   # jitoSOL
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",   # RAY
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",   # PYTH
}


class MemeCoin:
    def __init__(self, data: dict):
        self.address = data.get("baseToken", {}).get("address", "")
        self.symbol = data.get("baseToken", {}).get("symbol", "?")
        self.name = data.get("baseToken", {}).get("name", "?")
        self.price_usd = float(data.get("priceUsd", 0) or 0)
        self.liquidity_usd = float(data.get("liquidity", {}).get("usd", 0) or 0)
        self.volume_24h = float(data.get("volume", {}).get("h24", 0) or 0)
        self.volume_1h = float(data.get("volume", {}).get("h1", 0) or 0)
        self.price_change_1h = float(data.get("priceChange", {}).get("h1", 0) or 0)
        self.price_change_6h = float(data.get("priceChange", {}).get("h6", 0) or 0)
        self.price_change_24h = float(data.get("priceChange", {}).get("h24", 0) or 0)
        self.txns_1h_buys = int(data.get("txns", {}).get("h1", {}).get("buys", 0) or 0)
        self.txns_1h_sells = int(data.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
        self.market_cap = float(data.get("marketCap", 0) or 0)
        self.pair_address = data.get("pairAddress", "")
        self.dex_id = data.get("dexId", "")

        # Token age
        created_at = data.get("pairCreatedAt", 0) or 0
        self.age_hours = (time.time() - created_at / 1000) / 3600 if created_at else 999

        # Short-term momentum (5 min) — key signal for late entries
        self.price_change_5m = float(data.get("priceChange", {}).get("m5", 0) or 0)

        # Signal source
        self.copy_trade: bool   = False   # whale copy trade → MOON mode
        self.new_listing: bool  = False   # pump.fun → Raydium migration < 20min → MOON mode

        # Number of alpha wallets that bought this token (1 = normal signal, 2+ = strong signal)
        self.wallet_hit_count: int = 0

    def score(self) -> dict:
        """Compute a quality score for this token."""
        points = 0
        flags = []
        warnings = []

        # 1h volume
        if self.volume_1h > 200_000:
            points += 30
            flags.append("VOL_HIGH")
        elif self.volume_1h > 100_000:
            points += 20
        elif self.volume_1h > 50_000:
            points += 10

        # Price momentum
        if 10 <= self.price_change_1h <= 50:
            points += 25
            flags.append("MOMENTUM")
        elif 5 <= self.price_change_1h < 10:
            points += 15
        elif self.price_change_1h > 100:
            points -= 10
            warnings.append("EXTREME_PUMP")

        # Liquidity
        if self.liquidity_usd > 100_000:
            points += 20
            flags.append("LIQUID")
        elif self.liquidity_usd > 50_000:
            points += 10
        elif self.liquidity_usd < 20_000:
            points -= 20
            warnings.append("LOW_LIQUIDITY")

        # Buys/sells ratio (buy pressure)
        total_txns = self.txns_1h_buys + self.txns_1h_sells
        if total_txns > 0:
            buy_ratio = self.txns_1h_buys / total_txns
            if buy_ratio >= 0.65:
                points += 15
                flags.append("BUY_PRESSURE")
            elif buy_ratio <= 0.35:
                points -= 15
                warnings.append("SELL_PRESSURE")

        # Optimal age
        if 1 <= self.age_hours <= 12:
            points += 10
            flags.append("EARLY")
        elif self.age_hours < 0.5:
            points -= 10
            warnings.append("TOO_RECENT")
        elif self.age_hours > 48:
            points -= 5

        # Market cap (avoid too large or too small)
        if 500_000 <= self.market_cap <= 10_000_000:
            points += 10  # sweet spot
        elif self.market_cap < 100_000:
            warnings.append("MICRO_MCAP")
        elif self.market_cap > 50_000_000:
            warnings.append("HIGH_MCAP")

        # Raydium = higher trust than pump.fun
        if self.dex_id == "raydium":
            points += 5
            flags.append("RAYDIUM")

        return {
            "points": max(0, points),
            "flags": flags,
            "warnings": warnings,
        }


COPY_TRADE_WATCH_TTL      = 300   # 5 min for a copy-traded token to appear on DexScreener
NEW_LISTING_MAX_AGE_MIN   = 20    # 20 min max for a Raydium "new listing"
PUMPFUN_API               = "https://frontend-api.pump.fun/coins"


class MemeScanner:
    def __init__(self, wallet_tracker=None):
        self.running = False
        self.last_alerts: dict[str, float] = {}  # address -> timestamp
        self.alert_cooldown = 1800  # 30min between 2 alerts on the same token
        self.wallet_tracker = wallet_tracker  # optional WalletTracker
        # Copy trade queue — fed by the background wallet poll (15s)
        # Structure: {token_addr: {"ts": float, "wallets": set[str]}}
        # "wallets" = set of wallet addresses that bought this token
        self.pending_copy: dict[str, dict] = {}

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [MEME] {msg}")

    def add_copy_signal(self, token_addr: str, wallet_addr: str = ""):
        """
        Register a copy-trade signal from the background wallet poll.
        Called by MemeTrader._wallet_poll_loop every 15s.
        Each call can originate from a different wallet for the same token —
        wallet_hit_count then reflects how many whales bought simultaneously.
        """
        if not token_addr or token_addr in BLACKLISTED_TOKEN_ADDRESSES:
            return
        now = time.time()
        if token_addr not in self.pending_copy:
            self.pending_copy[token_addr] = {"ts": now, "wallets": set()}
            self.log(f"⏳ Copy trade pending DexScreener: {token_addr[:8]}...")
        if wallet_addr:
            self.pending_copy[token_addr]["wallets"].add(wallet_addr)

    async def fetch_trending(self) -> list[MemeCoin]:
        """Fetch Solana trending tokens on DexScreener (3 sources)."""
        tokens = []
        seen_addresses = set()

        async def _add_tokens(pairs_list):
            for pair in pairs_list:
                if pair.get("chainId") == "solana":
                    mc = MemeCoin(pair)
                    # Blacklist stablecoins/base tokens — DexScreener returns both
                    # tokens of a pair, which may include USDC, wSOL, etc.
                    if mc.address in BLACKLISTED_TOKEN_ADDRESSES:
                        continue
                    if mc.address not in seen_addresses:
                        seen_addresses.add(mc.address)
                        tokens.append(mc)

        async with httpx.AsyncClient(timeout=8) as client:
            # Source 1: Top boosted Solana tokens
            try:
                resp = await client.get(
                    "https://api.dexscreener.com/token-boosts/top/v1",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    sol_tokens = [t for t in data if t.get("chainId") == "solana"]
                    addresses = [t.get("tokenAddress", "") for t in sol_tokens[:20]]
                    if addresses:
                        addr_str = ",".join(addresses[:10])
                        resp2 = await client.get(
                            f"https://api.dexscreener.com/tokens/v1/solana/{addr_str}",
                        )
                        if resp2.status_code == 200:
                            pairs_data = resp2.json()
                            pairs = pairs_data if isinstance(pairs_data, list) else pairs_data.get("pairs", [])
                            await _add_tokens(pairs)
                        # Batch 2 (addresses 11-20)
                        if len(addresses) > 10:
                            addr_str_b = ",".join(addresses[10:20])
                            resp2b = await client.get(
                                f"https://api.dexscreener.com/tokens/v1/solana/{addr_str_b}",
                            )
                            if resp2b.status_code == 200:
                                pd2b = resp2b.json()
                                p2b = pd2b if isinstance(pd2b, list) else pd2b.get("pairs", [])
                                await _add_tokens(p2b)
            except Exception as e:
                self.log(f"fetch trending error: {type(e).__name__}: {e}")

            # Source 2: New pairs (early tokens)
            try:
                resp = await client.get(
                    "https://api.dexscreener.com/token-profiles/latest/v1",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    sol_profiles = [p for p in data if p.get("chainId") == "solana"]
                    addresses2 = [p.get("tokenAddress", "") for p in sol_profiles[:15]]
                    if addresses2:
                        addr_str2 = ",".join(addresses2[:10])
                        resp3 = await client.get(
                            f"https://api.dexscreener.com/tokens/v1/solana/{addr_str2}",
                        )
                        if resp3.status_code == 200:
                            pd3 = resp3.json()
                            p3 = pd3 if isinstance(pd3, list) else pd3.get("pairs", [])
                            await _add_tokens(p3)
                        if len(addresses2) > 10:
                            addr_str2b = ",".join(addresses2[10:15])
                            resp3b = await client.get(
                                f"https://api.dexscreener.com/tokens/v1/solana/{addr_str2b}",
                            )
                            if resp3b.status_code == 200:
                                pd3b = resp3b.json()
                                p3b = pd3b if isinstance(pd3b, list) else pd3b.get("pairs", [])
                                await _add_tokens(p3b)
            except Exception as e:
                self.log(f"fetch new pairs error: {type(e).__name__}: {e}")

            # Source 3: Solana gainers (tokens moving NOW)
            try:
                resp = await client.get(
                    "https://api.dexscreener.com/token-boosts/latest/v1",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    sol_latest = [t for t in data if t.get("chainId") == "solana"]
                    addresses3 = [t.get("tokenAddress", "") for t in sol_latest[:10]]
                    if addresses3:
                        addr_str3 = ",".join(addresses3[:10])
                        resp4 = await client.get(
                            f"https://api.dexscreener.com/tokens/v1/solana/{addr_str3}",
                        )
                        if resp4.status_code == 200:
                            pd4 = resp4.json()
                            p4 = pd4 if isinstance(pd4, list) else pd4.get("pairs", [])
                            await _add_tokens(p4)
            except Exception as e:
                self.log(f"fetch latest boosts error: {type(e).__name__}: {e}")

            # Source 3b: Raydium trending < 2h — tokens moving NOW
            # Tokens < 20min get promoted to new_listing if pump.fun graduation is confirmed
            try:
                resp = await client.get(
                    "https://api.dexscreener.com/latest/dex/search?q=raydium&rankBy=trendingScoreH1&order=desc",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    pairs_list = data.get("pairs", []) if isinstance(data, dict) else []
                    now_ts = time.time()
                    fresh = [
                        p for p in pairs_list
                        if p.get("chainId") == "solana"
                        and p.get("pairCreatedAt", 0)
                        and (now_ts - p["pairCreatedAt"] / 1000) / 3600 < 2.0
                        and float(p.get("volume", {}).get("h1", 0) or 0) > 5_000
                    ]
                    before_3b = len(tokens)
                    await _add_tokens(fresh[:15])
                    # Promote the very recent ones (< 20min) to new_listing
                    for mc in tokens[before_3b:]:
                        if mc.age_hours < NEW_LISTING_MAX_AGE_MIN / 60:
                            mc.new_listing = True
            except Exception as e:
                self.log(f"fetch raydium fresh error: {type(e).__name__}: {e}")

            # ──────────────────────────────────────────────────────────────
            # Source 5: pump.fun → Raydium graduations (< 20 min)
            # ──────────────────────────────────────────────────────────────
            # When a pump.fun bonding curve fills up (~$69K mcap), the token
            # automatically migrates to Raydium with ~$12K liquidity.
            # This is THE most explosive signal: 2x-10x in the first 30 minutes.
            # We pull tokens recently traded on pump.fun that have a raydium_pool,
            # then confirm via DexScreener that the pair is < 20min old.
            n_new_listing = 0
            try:
                resp5 = await client.get(
                    PUMPFUN_API,
                    params={
                        "offset": 0, "limit": 50,
                        "sort": "last_trade_timestamp",
                        "order": "DESC",
                        "includeNsfw": "false",
                    },
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                if resp5.status_code == 200:
                    coins5 = resp5.json()
                    # Select graduated tokens (raydium_pool != null)
                    graduated_mints = [
                        c["mint"] for c in coins5
                        if isinstance(c, dict)
                        and c.get("raydium_pool")
                        and c.get("mint")
                        and c["mint"] not in BLACKLISTED_TOKEN_ADDRESSES
                    ]
                    # Confirm via DexScreener that the Raydium pair is < 20min
                    now_ts = time.time()
                    for i in range(0, min(len(graduated_mints), 30), 10):
                        batch = graduated_mints[i:i+10]
                        try:
                            resp5b = await client.get(
                                f"https://api.dexscreener.com/tokens/v1/solana/{','.join(batch)}"
                            )
                            if resp5b.status_code == 200:
                                pd5 = resp5b.json()
                                p5 = pd5 if isinstance(pd5, list) else pd5.get("pairs", [])
                                fresh5 = [
                                    p for p in p5
                                    if p.get("chainId") == "solana"
                                    and p.get("dexId") == "raydium"
                                    and p.get("pairCreatedAt", 0)
                                    and (now_ts - p["pairCreatedAt"] / 1000)
                                        < NEW_LISTING_MAX_AGE_MIN * 60
                                    and float(p.get("liquidity", {}).get("usd", 0) or 0)
                                        > 10_000
                                ]
                                before5 = len(tokens)
                                await _add_tokens(fresh5)
                                for mc in tokens[before5:]:
                                    mc.new_listing = True
                                n_new_listing += len(tokens) - before5
                        except Exception as eb:
                            self.log(f"graduation batch error: {type(eb).__name__}: {eb}")
                    if n_new_listing > 0:
                        self.log(f"🆕 {n_new_listing} pump.fun→Raydium migration(s) detected")
            except Exception as e:
                self.log(f"fetch pump.fun graduations error: {type(e).__name__}: {e}")

        # Source 4: Copy trading — fed by background wallet poll (15s, see MemeTrader)
        # Wallet scanning is decoupled from the main scan for maximum reactivity.
        # Here we only read pending_copy (already populated) and resolve via DexScreener.
        n_copy = 0
        if self.wallet_tracker and self.wallet_tracker.ALPHA_WALLETS:
            try:
                now = time.time()

                # Expire pending entries older than 5 min
                expired = [
                    a for a, info in self.pending_copy.items()
                    if now - info["ts"] > COPY_TRADE_WATCH_TTL
                ]
                for addr in expired:
                    self.log(f"⌛ Copy trade expired (never appeared on DexScreener): {addr[:8]}...")
                    del self.pending_copy[addr]

                # Query DexScreener for ALL pending entries (new + retry)
                all_copy_addrs = list(self.pending_copy.keys())
                if all_copy_addrs:
                    async with httpx.AsyncClient(timeout=8) as c4:
                        for i in range(0, len(all_copy_addrs), 10):
                            batch = all_copy_addrs[i:i+10]
                            try:
                                resp_c = await c4.get(
                                    f"https://api.dexscreener.com/tokens/v1/solana/{','.join(batch)}",
                                )
                                if resp_c.status_code == 200:
                                    pd_c = resp_c.json()
                                    p_c = pd_c if isinstance(pd_c, list) else pd_c.get("pairs", [])
                                    before = len(tokens)
                                    await _add_tokens(p_c)
                                    added = len(tokens) - before
                                    n_copy += added
                                    # Mark copy_trade + wallet_hit_count from pending
                                    for mc in tokens[before:]:
                                        mc.copy_trade = True
                                        info = self.pending_copy.get(mc.address, {})
                                        mc.wallet_hit_count = len(info.get("wallets", set()))
                                        # Remove from pending if found with sufficient liquidity
                                        if mc.liquidity_usd > 5_000 and mc.address in self.pending_copy:
                                            del self.pending_copy[mc.address]
                            except Exception as e:
                                self.log(f"copy batch error: {type(e).__name__}: {e}")

                # Dedup fix: if a copy-trade token was already in the pool
                # from sources 1-3, mark it copy_trade + wallet_hit_count anyway
                copy_addr_set = set(all_copy_addrs)
                for mc in tokens:
                    if mc.address in copy_addr_set and not mc.copy_trade:
                        mc.copy_trade = True
                        info = self.pending_copy.get(mc.address, {})
                        mc.wallet_hit_count = len(info.get("wallets", set()))
                        n_copy += 1

            except Exception as e:
                self.log(f"copy trading error: {type(e).__name__}: {e}")

        sources = 5 if (self.wallet_tracker and self.wallet_tracker.ALPHA_WALLETS) else 4
        copy_info = f" +{n_copy}copy" if n_copy > 0 else ""
        new_info  = f" +{n_new_listing}new" if n_new_listing > 0 else ""
        self.log(f"📊 {len(tokens)} tokens scanned ({sources} sources{new_info}{copy_info})")
        return tokens

    def filter_tokens(self, tokens: list[MemeCoin]) -> list[tuple[MemeCoin, dict]]:
        """Filter and score tokens against safety criteria."""
        results = []

        for token in tokens:
            # Hard filters
            if token.liquidity_usd < MIN_LIQUIDITY_USD:
                continue
            if token.volume_24h < MIN_VOLUME_24H_USD:
                continue
            if token.price_change_1h < MIN_PRICE_CHANGE_1H:
                continue
            if token.price_change_1h > MAX_PRICE_CHANGE_1H:
                continue
            if token.age_hours < MIN_TOKEN_AGE_HOURS:
                continue
            if token.age_hours > MAX_TOKEN_AGE_HOURS:
                continue
            txns_total = token.txns_1h_buys + token.txns_1h_sells
            if txns_total < MIN_TXNS_1H:
                continue

            score = token.score()
            if score["points"] >= 30:
                results.append((token, score))

        # Sort by descending score
        results.sort(key=lambda x: x[1]["points"], reverse=True)
        return results

    def format_alert(self, token: MemeCoin, score: dict) -> str:
        """Format a memecoin alert."""
        age_str = f"{token.age_hours:.1f}h" if token.age_hours < 24 else f"{token.age_hours/24:.1f}d"
        buy_ratio = (
            f"{token.txns_1h_buys/(token.txns_1h_buys+token.txns_1h_sells)*100:.0f}%"
            if (token.txns_1h_buys + token.txns_1h_sells) > 0 else "?"
        )

        flags_str = " | ".join(score["flags"]) if score["flags"] else "—"
        warnings_str = " | ".join(score["warnings"]) if score["warnings"] else "None"

        mcap_str = (
            f"${token.market_cap/1_000_000:.2f}M" if token.market_cap >= 1_000_000
            else f"${token.market_cap/1_000:.0f}K"
        )
        liq_str = (
            f"${token.liquidity_usd/1_000:.0f}K"
        )

        return f"""
{'='*55}
🚨 MEME COIN OPPORTUNITY [{score['points']} pts]
Token     : {token.symbol} ({token.name})
Address   : {token.address}
DEX       : {token.dex_id.upper()}
Price     : ${token.price_usd:.8f}
Market cap: {mcap_str}
Liquidity : {liq_str}
Volume 1h : ${token.volume_1h/1_000:.0f}K
Pump 1h   : +{token.price_change_1h:.1f}%
Pump 6h   : {token.price_change_6h:+.1f}%
Age       : {age_str}
Buys/Total: {buy_ratio} ({token.txns_1h_buys}B / {token.txns_1h_sells}S in 1h)
Signals   : {flags_str}
Warnings  : ⚠️ {warnings_str}
Chart     : https://dexscreener.com/solana/{token.address}
{'='*55}"""

    async def run(self, interval_seconds: int = 60):
        """Scan loop — checks every 60s."""
        self.running = True
        self.log("Memecoin scanner started (DexScreener)")
        self.log(f"Criteria: liq>${MIN_LIQUIDITY_USD/1000:.0f}K | vol24h>${MIN_VOLUME_24H_USD/1000:.0f}K | pump1h>+{MIN_PRICE_CHANGE_1H}%")

        while self.running:
            try:
                tokens = await self.fetch_trending()
                if tokens:
                    opportunities = self.filter_tokens(tokens)
                    if opportunities:
                        for token, score in opportunities[:3]:  # top 3 max
                            # Cooldown to avoid spam
                            last = self.last_alerts.get(token.address, 0)
                            if time.time() - last > self.alert_cooldown:
                                alert = self.format_alert(token, score)
                                print(alert)
                                self.last_alerts[token.address] = time.time()
                    else:
                        self.log(f"Scan: {len(tokens)} tokens analyzed — no opportunity")
                else:
                    self.log("Scan: no tokens fetched")

            except Exception as e:
                self.log(f"Scan error: {e}")

            await asyncio.sleep(interval_seconds)

    def stop(self):
        self.running = False


async def main():
    """Standalone scanner test."""
    scanner = MemeScanner()
    try:
        await scanner.run(interval_seconds=60)
    except KeyboardInterrupt:
        scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
