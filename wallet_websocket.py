"""
Helius WebSocket monitor — real-time alpha wallet buy detection.

Replaces the 15s polling loop (wallet_tracker.scan_all) with push-based
notifications via Helius `logsSubscribe`. Detection latency drops from
30-90s to ~2-5s.

Architecture :
─────────────
    Helius WSS  ──logsSubscribe──>  WebSocketMonitor
                                          │
                                          │ on tx notification
                                          ▼
                                    getTransaction (HTTP)
                                          │
                                          ▼
                                    parse buy (pre/post token balances)
                                          │
                                          ▼
                                    on_buy_callback(token, wallet)
                                          │
                                          ▼
                                    scanner.add_copy_signal
                                          │
                                          ▼
                                    Jupiter quote → buy

Free tier limitations :
- `logsSubscribe` allows 1 mentions filter per subscription
- For 27 wallets : 27 parallel subscriptions (cheap, just more JSON-RPC requests)
- Alternatively : 1 subscription with no filter + post-filter (more bandwidth)
                  We use the multi-subscription approach for cleaner signal:noise.

Resilience :
- Auto-reconnect on disconnect (5s backoff, exponential to 60s)
- De-dupe signatures (avoid double-processing if same tx notified twice)
- Backpressure: max 50 concurrent tx parses (avoid overwhelming RPC)
"""

import asyncio
import json
import ssl
import time
from datetime import datetime
from typing import Awaitable, Callable

import certifi
import httpx
import websockets

# SSL context using certifi's root certificates — fixes "certificate verify failed"
# on macOS (Python's built-in SSL can't find system certs)
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class WebSocketMonitor:
    """
    Subscribes to Helius WebSocket and detects token buys from alpha wallets
    in real-time. Calls `on_buy_callback(token_addr, wallet_addr)` for each buy.
    """

    def __init__(
        self,
        rpc_url: str,
        alpha_wallets: list[str],
        on_buy_callback: Callable[[str, str], Awaitable[None]],
    ):
        # Convert HTTPS RPC to WSS (Helius uses same hostname)
        self.rpc_url = rpc_url
        if rpc_url.startswith("https://"):
            self.ws_url = "wss://" + rpc_url[len("https://"):]
        else:
            self.ws_url = rpc_url
        self.alpha_wallets = list(alpha_wallets)
        self.on_buy_callback = on_buy_callback
        self.running = False
        self._seen_signatures: set[str] = set()
        self._seen_max = 5000  # bounded set to avoid memory leak
        self._parse_sem = asyncio.Semaphore(50)  # max 50 parallel tx parses
        # Track subscription ID → wallet address (for inverse lookup)
        self._sub_to_wallet: dict[int, str] = {}
        # #6 Scalper detection — track recent buys to catch quick buy→sell churn
        self._recent_buys: dict[tuple, float] = {}      # (wallet, mint) → buy ts
        self._scalper_events: dict[str, list] = {}      # wallet → [ts of quick flips]
        self.SCALPER_WINDOW = 300                        # 5 min buy→sell = scalp
        self.SCALPER_THRESHOLD = 3                       # 3+ flips in 1h → flagged
        # Stats
        self.stats = {
            "logs_received": 0,
            "txs_parsed": 0,
            "buys_detected": 0,
            "errors": 0,
            "reconnects": 0,
            "last_event_ts": 0.0,
        }

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [WS-MONITOR] {msg}")

    # ─────────────────────────────────────────────────────────────────
    async def run(self):
        """Main loop with auto-reconnect."""
        self.running = True
        backoff = 5
        while self.running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ssl=SSL_CONTEXT,
                    ping_interval=30,
                    ping_timeout=15,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,  # 10 MB max message
                ) as ws:
                    self.log(f"✅ Connected to Helius WSS")
                    backoff = 5
                    await self._subscribe_all(ws)
                    # Consume messages forever (or until error)
                    async for raw_msg in ws:
                        try:
                            await self._handle_message(raw_msg)
                        except Exception as e:
                            self.stats["errors"] += 1
                            self.log(f"⚠️ msg handler error: {type(e).__name__}: {e}")
            except (websockets.ConnectionClosed, asyncio.TimeoutError) as e:
                self.stats["reconnects"] += 1
                self.log(f"🔌 Disconnected ({type(e).__name__}) — reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self.stats["reconnects"] += 1
                self.log(f"❌ WS error [{type(e).__name__}]: {e} — reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.log("🛑 WS monitor stopped")

    # ─────────────────────────────────────────────────────────────────
    async def _subscribe_all(self, ws):
        """Send N subscription requests, one per wallet (1 mention each)."""
        self._sub_to_wallet.clear()
        for i, wallet in enumerate(self.alpha_wallets):
            request = {
                "jsonrpc": "2.0",
                "id": i + 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [wallet]},
                    {"commitment": "confirmed"},
                ],
            }
            await ws.send(json.dumps(request))
        self.log(f"📡 Subscribed to {len(self.alpha_wallets)} wallets via logsSubscribe")

    # ─────────────────────────────────────────────────────────────────
    async def _handle_message(self, raw_msg: str):
        """Route incoming WS messages: subscription confirmations or notifications."""
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        # Subscription confirmation (id match → store sub_id ↔ wallet mapping)
        if "id" in data and "result" in data and isinstance(data.get("result"), int):
            req_id = data["id"]
            sub_id = data["result"]
            wallet_idx = req_id - 1
            if 0 <= wallet_idx < len(self.alpha_wallets):
                wallet = self.alpha_wallets[wallet_idx]
                self._sub_to_wallet[sub_id] = wallet
            return

        # Notification (params.result contains log data)
        if data.get("method") != "logsNotification":
            return
        params = data.get("params", {})
        sub_id = params.get("subscription")
        wallet = self._sub_to_wallet.get(sub_id, "")
        result = params.get("result", {})
        value = result.get("value", {})

        # Skip failed transactions
        if value.get("err") is not None:
            return

        signature = value.get("signature")
        if not signature or signature in self._seen_signatures:
            return

        # Bound the seen set
        if len(self._seen_signatures) >= self._seen_max:
            # Drop ~10% oldest (we don't track order, just clear half)
            self._seen_signatures = set(list(self._seen_signatures)[self._seen_max // 2:])
        self._seen_signatures.add(signature)

        self.stats["logs_received"] += 1
        self.stats["last_event_ts"] = time.time()

        # Process the tx asynchronously (don't block message loop)
        asyncio.create_task(self._process_tx(signature, wallet))

    # ─────────────────────────────────────────────────────────────────
    async def _process_tx(self, signature: str, wallet: str):
        """Fetch full tx via HTTP, parse for buys, fire callback."""
        async with self._parse_sem:
            try:
                tx = await self._fetch_transaction(signature)
                if not tx:
                    return
                self.stats["txs_parsed"] += 1

                # #6 — detect SELLS first, to catch buy→sell churn (scalper)
                sells = self._extract_sells(tx, wallet)
                now = time.time()
                for mint in sells:
                    bought_ts = self._recent_buys.get((wallet, mint))
                    if bought_ts and (now - bought_ts) < self.SCALPER_WINDOW:
                        # Quick flip: bought then sold same token within the window
                        self._scalper_events.setdefault(wallet, []).append(now)
                        # Keep only last hour of events
                        self._scalper_events[wallet] = [
                            t for t in self._scalper_events[wallet] if now - t < 3600
                        ]

                buys = self._extract_buys(tx, wallet)
                for buy in buys:
                    token_addr = buy["mint"]
                    whale_price = buy.get("whale_price")
                    self._recent_buys[(wallet, token_addr)] = now
                    self.stats["buys_detected"] += 1

                    # Suppress signals from flagged scalpers
                    if self.is_scalper(wallet):
                        self.log(f"🚫 {wallet[:8]}... BUY {token_addr[:8]}... suppressed (scalper)")
                        continue

                    self.log(f"🔁 {wallet[:8]}... → BUY {token_addr[:8]}... (sig {signature[:12]}...)")
                    try:
                        await self.on_buy_callback(token_addr, wallet, whale_price)
                    except Exception as e:
                        self.log(f"⚠️ callback error: {type(e).__name__}: {e}")

                # Bounded cleanup of _recent_buys
                if len(self._recent_buys) > 2000:
                    cutoff = now - self.SCALPER_WINDOW
                    self._recent_buys = {
                        k: v for k, v in self._recent_buys.items() if v > cutoff
                    }
            except Exception as e:
                self.stats["errors"] += 1

    # ─────────────────────────────────────────────────────────────────
    async def _fetch_transaction(self, signature: str) -> dict | None:
        """Get the parsed transaction from RPC."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        }
        # Retry once on transient errors
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    resp = await client.post(self.rpc_url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        result = data.get("result")
                        if result:
                            return result
                        # Tx not yet confirmed → small wait + retry
                        if attempt == 0:
                            await asyncio.sleep(1.5)
                            continue
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
        return None

    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _extract_buys(tx: dict, wallet: str) -> list[dict]:
        """
        Compare preTokenBalances vs postTokenBalances for the given wallet.
        Return a list of dicts for each token the wallet BOUGHT:
            {
              "mint": str,
              "tokens_raw": int,        # raw token units received
              "whale_price": float|None # lamports of SOL spent per raw token
            }

        whale_price lets us compare the whale's entry to the current price
        (the Jupiter probe returns the same unit: lamports per raw token).
        If we enter > +15% above the whale, we're buying their exit liquidity.

        whale_price is None if we can't compute SOL spent reliably (then the
        caller doesn't apply the late-entry gate — fail open, not closed).
        """
        bought = []
        meta = tx.get("meta", {})
        if meta.get("err"):
            return bought

        pre = meta.get("preTokenBalances", []) or []
        post = meta.get("postTokenBalances", []) or []

        # Map mint → raw amount (pre) for this wallet
        pre_raw: dict[str, int] = {}
        for b in pre:
            if b.get("owner") == wallet:
                mint = b.get("mint", "")
                raw = int(b.get("uiTokenAmount", {}).get("amount", "0") or 0)
                pre_raw[mint] = raw

        # Compute the wallet's net SOL spent this tx (lamports), for price calc.
        whale_sol_spent = WebSocketMonitor._wallet_sol_delta(tx, wallet)

        for b in post:
            if b.get("owner") == wallet:
                mint = b.get("mint", "")
                post_raw = int(b.get("uiTokenAmount", {}).get("amount", "0") or 0)
                pre_amount = pre_raw.get(mint, 0)
                delta = post_raw - pre_amount
                if delta > 0:
                    whale_price = None
                    if whale_sol_spent and whale_sol_spent > 0 and delta > 0:
                        whale_price = whale_sol_spent / delta  # lamports per raw token
                    bought.append({
                        "mint": mint,
                        "tokens_raw": delta,
                        "whale_price": whale_price,
                    })
        return bought

    @staticmethod
    def _extract_sells(tx: dict, wallet: str) -> list[str]:
        """Return mints where the wallet's balance DECREASED (= sold)."""
        sold = []
        meta = tx.get("meta", {})
        if meta.get("err"):
            return sold
        pre = meta.get("preTokenBalances", []) or []
        post = meta.get("postTokenBalances", []) or []
        pre_raw: dict[str, int] = {}
        for b in pre:
            if b.get("owner") == wallet:
                pre_raw[b.get("mint", "")] = int(b.get("uiTokenAmount", {}).get("amount", "0") or 0)
        post_raw: dict[str, int] = {}
        for b in post:
            if b.get("owner") == wallet:
                post_raw[b.get("mint", "")] = int(b.get("uiTokenAmount", {}).get("amount", "0") or 0)
        for mint, pre_amt in pre_raw.items():
            post_amt = post_raw.get(mint, 0)
            if post_amt < pre_amt:  # balance dropped = sold (partial or full)
                sold.append(mint)
        return sold

    def is_scalper(self, wallet: str) -> bool:
        """True if the wallet has done ≥ SCALPER_THRESHOLD quick flips in the last hour."""
        events = self._scalper_events.get(wallet, [])
        now = time.time()
        recent = [t for t in events if now - t < 3600]
        return len(recent) >= self.SCALPER_THRESHOLD

    @staticmethod
    def _wallet_sol_delta(tx: dict, wallet: str) -> int | None:
        """
        Net lamports the wallet spent in this tx (positive = SOL went out).
        Reads preBalances/postBalances indexed by accountKeys.
        Returns None if the wallet's account index can't be resolved.
        """
        try:
            meta = tx.get("meta", {})
            msg = tx.get("transaction", {}).get("message", {})
            keys = msg.get("accountKeys", []) or []
            # accountKeys can be list of strings or list of {pubkey:...}
            idx = None
            for i, k in enumerate(keys):
                pk = k if isinstance(k, str) else k.get("pubkey", "")
                if pk == wallet:
                    idx = i
                    break
            if idx is None:
                return None
            pre = meta.get("preBalances", []) or []
            post = meta.get("postBalances", []) or []
            if idx >= len(pre) or idx >= len(post):
                return None
            spent = pre[idx] - post[idx]  # lamports out (includes tx fee, negligible)
            return spent if spent > 0 else None
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────
    def stop(self):
        self.running = False

    def print_stats(self):
        s = self.stats
        last_evt = (
            f"{int(time.time() - s['last_event_ts'])}s ago"
            if s['last_event_ts'] > 0 else "never"
        )
        self.log(
            f"📊 logs={s['logs_received']} parsed={s['txs_parsed']} "
            f"buys={s['buys_detected']} reconnects={s['reconnects']} "
            f"errors={s['errors']} last_event={last_evt}"
        )
