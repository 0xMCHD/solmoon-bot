"""
Wallet Copy Tracker — follows Solana alpha wallets and detects their buys.

How to find alpha wallets:
1. Go to https://gmgn.ai/sol/address
2. Filter: 7d PnL > 200%, trades > 20, win rate > 60%
3. Copy the best wallet addresses into the list below

Alternatives: https://app.cielo.finance / https://bullx.io (Smart Money section)
"""

import asyncio
import httpx
import time
from datetime import datetime


class WalletTracker:
    """
    Monitor Solana alpha wallets.
    Detect their token buys within the last N minutes.
    Returns token addresses for the scanner to evaluate.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # ⚡ ADD YOUR ALPHA WALLETS HERE (find them on GMGN.ai)
    # Example: "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    # ─────────────────────────────────────────────────────────────────────────
    ALPHA_WALLETS: list[str] = [
        # F5jWYuiD — most active legacy wallet (frequent buys)
        "F5jWYuiDLTiaLYa54D88YbpXgEsA6NKHzWy4SN4bMYjt",

        # Confirmed strong signals (GMGN.ai vetted)
        "4zb5WFzzAP6UZUva5iXPEz1JbKTU4Z6TC3sNEzLbpv98",  # best signal (Billy +29%)
        "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
        "7xkXams2xqCokfoMyLKUtrKRTXqs9EzyEnkkFVH459YH",
        "CATjstjdDxqxKdrXQ9R8DWF2oZp8jSd2QX2VJ1zQbT91",
        "6bVUHfK6YzdhLEysxb9VHwsNdDvMcYhk1FfSjZ9onvZx",

        # Added 2026-04-27
        "8MoW9mtbEz6z3gPuAdYb1yWhjCAxQSYqpcTb1CQgN5qb",
        "Ew6qBU7N34gRNgpgUwhJ3PgrtbPYpLYWLBEG5yuQTceD",
        "7pDhG6NqfzQzw5KvtGXJbVRUh4iTBgYAn68BSKjdMNC1",
        "FFEjC9MHhpQViBPrD2iU6LmV2hEigyhLJaL7MZUZzyD4",
        "uveKTCxihqgL2E9X6CYBDQoQko69QjtwE4d6FkBFcy1",
        "CVtN7xVV3ed5x6kJfyKL1b57NeLe8BXKoAWzC29D3G8Q",

        # Removed: 4vw54BmA — pump.fun micro-caps with $0 liq, pending_copy always expired

        # Added 2026-05-01 (GMGN.ai 7d rank, filters: winrate>60%, trades>30, PnL>100%)
        "FbiBbTBP2Vs7b3j74eBsAYFpxWms11m23DeGmYcc8Yoo",  # top new — +289% PnL / 73% WR / 107tx / $17K
        "nrzLzxvq1EENDEi5cYp2H8ZscyLKa79yfQ9XWm3zbxt",   # +133% PnL / 60% WR / 34tx — axiom+smart_degen
        "5d3jQcuUvsuHyZkhdp78FFqc7WogrzZpTtec1X9VNkuE",  # +127% PnL / 55% WR / 37tx — $30K / kol+smart_degen

        # Added 2026-05-13 (curated from GMGN.ai 7d+30d cross-validation)
        "74oNN9VfJv2V2SJf4RFSiKPKbrgyMdz7agkaeikRSgm5",  # +319% PnL 7d / 68% WR / 388tx 30d — axiom+launchpad_smart
        "FRcHp2wrSk2Ej9Y81DFEVJ7U8arL1ki5RX6c49Wa3M8J",  # +161% PnL 7d / 100% WR! / 35tx — smart_degen
        "FWNmzY26FnsmpaWPQQJXQ24PQAyKtDByJz9HMa24s1z5",  # VOLUME BEAST — 5377 trades 30d / 93% WR / $62K profit
        "D1gwKFveaQ2mogvv5dhHNa7cfdcuGQphtfiwe22sm3nL",  # +77% PnL 7d / 57% WR — smart_degen+trojan
        "HtbXDAE1xX35vXhE3raVuYiZrykWiFWcnDJyy3WLo9So",  # +71% PnL / 511tx 30d — axiom+launchpad_smart
        "8FiuwM6FmVKmBLCaJ6QcNScnVw4NuNs7Tt4Skf91saF8",  # BEST CONSISTENCY — 88% WR 7d AND 85% WR 30d
        "21cutQKXw6opqwAcUfVVSDFbjKjs9NA94cUTfYBy4SZ6",  # 657 trades 30d — axiom+launchpad_smart+padre
        "ExkBDVdrrN7woPX42v5sfJa7AvHKhAb2uU9Fwzhhokno",  # 965 trades 30d — axiom+launchpad_smart+padre
        "CevfH18c59S1TwPuP3vgLD6W4q1Nf8nRdVNNLHnUMiWp",  # 671 trades 30d / 60% WR 30d — launchpad_smart+padre

        # Added 2026-05-19 (Day 0 of Personal Challenge — fresh GMGN scrape)
        "7S3E2L25kr6oN2cMP2GQ5tMEfg8jwcmoYo35vvv8rxhW",  # TOP — +118% PnL 7d / 76% WR / 83tx / $12.8K — axiom+smart_degen
        "Gf2wYM2k5ojfPzN5Uqi3mbP1nWEdZhhzMyDBsKrHA3kC",  # +62% PnL 7d / 60% WR + 1d momentum +108% — launchpad_smart+padre
        "THXcGyTMLSKWmvpDpdgL8G224xfMXksCfA29LBoJfUJ",   # +71% PnL 7d / 65% WR / 90tx — axiom+bullx+smart_degen
    ]

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.seen_signatures: set[str] = set()
        self._log_prefix = "[WALLET-TRACKER]"

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {self._log_prefix} {msg}")

    # ─────────────────────────────────────────────────────────────────────────
    async def _rpc_call(self, client: httpx.AsyncClient, method: str, params: list) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        try:
            resp = await client.post(self.rpc_url, json=payload)
            return resp.json()
        except Exception:
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    async def _get_signatures(self, client: httpx.AsyncClient, wallet: str, limit: int = 8) -> list[dict]:
        """Fetch the wallet's most recent transaction signatures."""
        result = await self._rpc_call(
            client,
            "getSignaturesForAddress",
            [wallet, {"limit": limit, "commitment": "confirmed"}],
        )
        return result.get("result", [])

    # ─────────────────────────────────────────────────────────────────────────
    async def _get_transaction(self, client: httpx.AsyncClient, signature: str) -> dict | None:
        """Fetch full transaction details."""
        result = await self._rpc_call(
            client,
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        return result.get("result")

    # ─────────────────────────────────────────────────────────────────────────
    async def detect_buys(self, wallet: str, since_minutes: int = 2) -> list[str]:
        """
        Detect tokens bought by the wallet in the last N minutes.
        Returns token mint addresses.
        """
        bought_tokens = []
        cutoff = time.time() - (since_minutes * 60)

        try:
            async with httpx.AsyncClient(timeout=8) as client:
                sigs = await self._get_signatures(client, wallet)

                for sig_info in sigs:
                    sig = sig_info.get("signature", "")
                    block_time = sig_info.get("blockTime", 0) or 0

                    # Stop if the tx is older than the lookback window
                    if block_time < cutoff:
                        break

                    # Skip already-seen signatures
                    if sig in self.seen_signatures:
                        continue
                    self.seen_signatures.add(sig)

                    # Fetch full transaction
                    tx = await self._get_transaction(client, sig)
                    if not tx:
                        continue

                    meta = tx.get("meta", {})
                    if meta.get("err"):
                        continue  # failed transaction

                    # Compare pre/post balances to detect received tokens
                    pre_balances = meta.get("preTokenBalances", [])
                    post_balances = meta.get("postTokenBalances", [])

                    pre_map: dict[str, float] = {}
                    for b in pre_balances:
                        if b.get("owner") == wallet:
                            mint = b.get("mint", "")
                            amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                            pre_map[mint] = amount

                    for b in post_balances:
                        if b.get("owner") == wallet:
                            mint = b.get("mint", "")
                            post_amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                            pre_amount = pre_map.get(mint, 0)

                            # Balance increased = token bought
                            if post_amount > pre_amount and post_amount > 0:
                                bought_tokens.append(mint)

        except Exception as e:
            self.log(f"Scan error {wallet[:8]}...: {e}")

        return bought_tokens

    # ─────────────────────────────────────────────────────────────────────────
    async def scan_all(self, since_minutes: int = 2) -> dict[str, list[str]]:
        """
        Scan all alpha wallets in parallel.
        Returns a dict {token_address: [wallet1, wallet2, ...]}
        — lets us know how many wallets bought the same token simultaneously.
        A token bought by 2+ wallets is a much stronger signal.
        """
        if not self.ALPHA_WALLETS:
            return {}

        tasks = [self.detect_buys(w, since_minutes) for w in self.ALPHA_WALLETS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        token_wallets: dict[str, list[str]] = {}
        wallet_hits = 0

        for i, result in enumerate(results):
            if isinstance(result, list) and result:
                wallet_hits += 1
                wallet_addr = self.ALPHA_WALLETS[i]
                self.log(f"🔁 {wallet_addr[:8]}... bought {len(result)} token(s)")
                for token in result:
                    if token not in token_wallets:
                        token_wallets[token] = []
                    token_wallets[token].append(wallet_addr)

        if token_wallets:
            self.log(f"🎯 {len(token_wallets)} copy-trade token(s) from {wallet_hits} wallet(s)")
            # Strong signal: multiple wallets buying the same token at the same time
            for token, wallets in token_wallets.items():
                if len(wallets) >= 2:
                    self.log(f"  🔥 {token[:8]}... bought by {len(wallets)} wallets — STRONG signal")

        return token_wallets

    # ─────────────────────────────────────────────────────────────────────────
    def add_wallet(self, address: str):
        """Add an alpha wallet at runtime."""
        if address not in self.ALPHA_WALLETS:
            self.ALPHA_WALLETS.append(address)
            self.log(f"✅ Wallet added: {address[:8]}...")

    def remove_wallet(self, address: str):
        """Remove a wallet."""
        if address in self.ALPHA_WALLETS:
            self.ALPHA_WALLETS.remove(address)
            self.log(f"🗑️ Wallet removed: {address[:8]}...")

    def status(self):
        """Print tracker status."""
        self.log(f"Tracked wallets: {len(self.ALPHA_WALLETS)}")
        for w in self.ALPHA_WALLETS:
            self.log(f"  → {w[:8]}...{w[-4:]}")
