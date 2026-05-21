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
                buys = self._extract_buys(tx, wallet)
                for token_addr in buys:
                    self.stats["buys_detected"] += 1
                    self.log(f"🔁 {wallet[:8]}... → BUY {token_addr[:8]}... (sig {signature[:12]}...)")
                    try:
                        await self.on_buy_callback(token_addr, wallet)
                    except Exception as e:
                        self.log(f"⚠️ callback error: {type(e).__name__}: {e}")
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
    def _extract_buys(tx: dict, wallet: str) -> list[str]:
        """
        Compare preTokenBalances vs postTokenBalances for the given wallet.
        Return list of mint addresses where balance INCREASED (= bought).
        """
        bought = []
        meta = tx.get("meta", {})
        if meta.get("err"):
            return bought

        pre = meta.get("preTokenBalances", []) or []
        post = meta.get("postTokenBalances", []) or []

        pre_map: dict[str, float] = {}
        for b in pre:
            if b.get("owner") == wallet:
                mint = b.get("mint", "")
                amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                pre_map[mint] = amount

        for b in post:
            if b.get("owner") == wallet:
                mint = b.get("mint", "")
                post_amount = float(b.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                pre_amount = pre_map.get(mint, 0)
                if post_amount > pre_amount and post_amount > 0:
                    bought.append(mint)
        return bought

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
