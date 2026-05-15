"""
Wallet Copy Tracker — suit les wallets alpha Solana et détecte leurs achats.

Comment trouver des wallets alpha :
1. Va sur https://gmgn.ai/sol/address
2. Filtre : PnL 7j > 200%, trades > 20, win rate > 60%
3. Copie les adresses des meilleurs wallets ci-dessous

Alternative : https://app.cielo.finance / https://bullx.io (Smart Money section)
"""

import asyncio
import httpx
import time
from datetime import datetime


class WalletTracker:
    """
    Surveille des wallets alpha Solana.
    Détecte leurs achats de tokens dans les 2 dernières minutes.
    Retourne les adresses de tokens pour que le scanner les évalue.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # ⚡ AJOUTE ICI LES WALLETS ALPHA (trouvés sur GMGN.ai)
    # Exemple: "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    # ─────────────────────────────────────────────────────────────────────────
    ALPHA_WALLETS: list[str] = [
        # ── F5jWYuiD — le plus actif des anciens (achats fréquents)
        "F5jWYuiDLTiaLYa54D88YbpXgEsA6NKHzWy4SN4bMYjt",

        # ── Confirmés utiles (GMGN.ai)
        "4zb5WFzzAP6UZUva5iXPEz1JbKTU4Z6TC3sNEzLbpv98",  # meilleur signal (Billy +29%)
        "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
        "7xkXams2xqCokfoMyLKUtrKRTXqs9EzyEnkkFVH459YH",
        "CATjstjdDxqxKdrXQ9R8DWF2oZp8jSd2QX2VJ1zQbT91",
        "6bVUHfK6YzdhLEysxb9VHwsNdDvMcYhk1FfSjZ9onvZx",

        # ── Ajoutés le 2026-04-27
        "8MoW9mtbEz6z3gPuAdYb1yWhjCAxQSYqpcTb1CQgN5qb",
        "Ew6qBU7N34gRNgpgUwhJ3PgrtbPYpLYWLBEG5yuQTceD",
        "7pDhG6NqfzQzw5KvtGXJbVRUh4iTBgYAn68BSKjdMNC1",
        "FFEjC9MHhpQViBPrD2iU6LmV2hEigyhLJaL7MZUZzyD4",
        "uveKTCxihqgL2E9X6CYBDQoQko69QjtwE4d6FkBFcy1",
        "CVtN7xVV3ed5x6kJfyKL1b57NeLe8BXKoAWzC29D3G8Q",

        # Retiré : 4vw54BmA — pump.fun micro-caps à $0 liq, pending_copy expire toujours

        # ── Ajoutés le 2026-05-01 (GMGN.ai rank 7j, filtres: winrate>60%, trades>30, PnL>100%)
        "FbiBbTBP2Vs7b3j74eBsAYFpxWms11m23DeGmYcc8Yoo",  # #1 new — PnL +289% / 73% WR / 107tx / $17K
        "nrzLzxvq1EENDEi5cYp2H8ZscyLKa79yfQ9XWm3zbxt",   # PnL +133% / 60% WR / 34tx — axiom+smart_degen
        "5d3jQcuUvsuHyZkhdp78FFqc7WogrzZpTtec1X9VNkuE",  # PnL +127% / 55% WR / 37tx — $30K / kol+smart_degen
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
        """Récupère les dernières signatures de transactions du wallet."""
        result = await self._rpc_call(
            client,
            "getSignaturesForAddress",
            [wallet, {"limit": limit, "commitment": "confirmed"}],
        )
        return result.get("result", [])

    # ─────────────────────────────────────────────────────────────────────────
    async def _get_transaction(self, client: httpx.AsyncClient, signature: str) -> dict | None:
        """Récupère les détails d'une transaction."""
        result = await self._rpc_call(
            client,
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        return result.get("result")

    # ─────────────────────────────────────────────────────────────────────────
    async def detect_buys(self, wallet: str, since_minutes: int = 2) -> list[str]:
        """
        Détecte les tokens achetés par le wallet dans les N dernières minutes.
        Retourne les adresses de tokens (mints).
        """
        bought_tokens = []
        cutoff = time.time() - (since_minutes * 60)

        try:
            async with httpx.AsyncClient(timeout=8) as client:
                sigs = await self._get_signatures(client, wallet)

                for sig_info in sigs:
                    sig = sig_info.get("signature", "")
                    block_time = sig_info.get("blockTime", 0) or 0

                    # Stop si la tx est plus vieille que la fenêtre
                    if block_time < cutoff:
                        break

                    # Skip si déjà vu
                    if sig in self.seen_signatures:
                        continue
                    self.seen_signatures.add(sig)

                    # Récupère la transaction complète
                    tx = await self._get_transaction(client, sig)
                    if not tx:
                        continue

                    meta = tx.get("meta", {})
                    if meta.get("err"):
                        continue  # transaction échouée

                    # Compare les balances avant/après pour trouver les tokens reçus
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

                            # Balance augmentée = token acheté
                            if post_amount > pre_amount and post_amount > 0:
                                bought_tokens.append(mint)

        except Exception as e:
            self.log(f"Erreur scan {wallet[:8]}...: {e}")

        return bought_tokens

    # ─────────────────────────────────────────────────────────────────────────
    async def scan_all(self, since_minutes: int = 2) -> dict[str, list[str]]:
        """
        Scan tous les wallets alpha en parallèle.
        Retourne un dict {token_address: [wallet1, wallet2, ...]}
        — permet de savoir combien de wallets ont acheté le même token simultanément.
        Un token acheté par 2+ wallets = signal beaucoup plus fort.
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
                self.log(f"🔁 {wallet_addr[:8]}... a acheté {len(result)} token(s)")
                for token in result:
                    if token not in token_wallets:
                        token_wallets[token] = []
                    token_wallets[token].append(wallet_addr)

        if token_wallets:
            self.log(f"🎯 {len(token_wallets)} token(s) copy-trade depuis {wallet_hits} wallet(s)")
            # Signal fort : plusieurs wallets achètent le même token au même moment
            for token, wallets in token_wallets.items():
                if len(wallets) >= 2:
                    self.log(f"  🔥 {token[:8]}... acheté par {len(wallets)} wallets — signal FORT")

        return token_wallets

    # ─────────────────────────────────────────────────────────────────────────
    def add_wallet(self, address: str):
        """Ajoute un wallet alpha dynamiquement."""
        if address not in self.ALPHA_WALLETS:
            self.ALPHA_WALLETS.append(address)
            self.log(f"✅ Wallet ajouté: {address[:8]}...")

    def remove_wallet(self, address: str):
        """Retire un wallet."""
        if address in self.ALPHA_WALLETS:
            self.ALPHA_WALLETS.remove(address)
            self.log(f"🗑️ Wallet retiré: {address[:8]}...")

    def status(self):
        """Affiche le statut du tracker."""
        self.log(f"Wallets trackés: {len(self.ALPHA_WALLETS)}")
        for w in self.ALPHA_WALLETS:
            self.log(f"  → {w[:8]}...{w[-4:]}")
