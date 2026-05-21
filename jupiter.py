"""Jupiter Swap API v2 integration."""

import httpx
import config


# Common headers with API key
def _headers() -> dict:
    return {
        "x-api-key": config.JUPITER_API_KEY,
        "Accept": "application/json",
    }


async def get_price(input_mint: str = config.SOL_MINT,
                    output_mint: str = config.USDC_MINT) -> float | None:
    """Fetch SOL price via a micro-quote on Jupiter /order."""
    try:
        one_sol = config.LAMPORTS_PER_SOL
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(one_sol),
            "slippageBps": 50,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{config.JUPITER_API_URL}/order",
                params=params,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        # Extract price from the /order response
        out_amount = int(data.get("outAmount", 0))
        if out_amount > 0:
            return out_amount / 10**config.USDC_DECIMALS

        # Try the nested quote field if present
        quote = data.get("quote", {})
        out_amount = int(quote.get("outAmount", 0))
        if out_amount > 0:
            return out_amount / 10**config.USDC_DECIMALS
    except Exception:
        pass

    # CoinGecko fallback
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
        price = data.get("solana", {}).get("usd")
        if price:
            return float(price)
    except Exception:
        pass

    return None


async def probe_token_tradeable(token_mint: str,
                                probe_amount_sol: float = 0.01) -> dict | None:
    """
    Quick probe: is this token currently tradeable on Solana ?

    Calls Jupiter /order with a small amount (0.01 SOL ~ $1.50) WITHOUT taker.
    If Jupiter returns a route, the token has liquidity. We don't execute —
    just check whether a swap is possible AND estimate slippage / price.

    Returns dict with:
        - tradeable      : bool — Jupiter has a route
        - price_per_token: float — derived from outAmount/inAmount
        - price_impact   : float — % price impact for our probe size
        - out_amount     : int — raw token amount we'd receive
        - route_count    : int — number of DEXes in the route
        - error          : str | None — exception or HTTP error

    None if probe failed (network down).
    """
    import asyncio

    lamports = int(probe_amount_sol * config.LAMPORTS_PER_SOL)
    params = {
        "inputMint": config.SOL_MINT,
        "outputMint": token_mint,
        "amount": str(lamports),
        "slippageBps": 500,  # 5% — large probe slippage tolerance
        "dynamicComputeUnitLimit": "true",
    }

    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(
                f"{config.JUPITER_API_URL}/order",
                params=params,
                headers=_headers(),
            )
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
        return None
    except Exception:
        return None

    if resp.status_code != 200:
        # 400/404 = no route / unknown token = not tradeable yet
        return {"tradeable": False, "error": f"HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"tradeable": False, "error": "invalid json"}

    in_amount = int(data.get("inAmount", 0))
    out_amount = int(data.get("outAmount", 0))
    if in_amount <= 0 or out_amount <= 0:
        return {"tradeable": False, "error": "zero amounts"}

    # Compute price (tokens per SOL)
    # Note: token decimals vary — Jupiter returns raw amounts. The caller
    # can use this with the token's decimals if known. For tradeability check
    # alone, we only need to know out_amount > 0.
    price_impact = 0.0
    try:
        price_impact = float(data.get("priceImpactPct", "0") or 0)
    except Exception:
        pass

    # Count DEXes touched
    route_plan = data.get("routePlan", []) or []
    route_count = len(route_plan)

    return {
        "tradeable": True,
        "in_amount": in_amount,
        "out_amount": out_amount,
        "price_impact": price_impact,
        "route_count": route_count,
        "error": None,
    }


async def get_quote(input_mint: str,
                    output_mint: str,
                    amount_lamports: int,
                    slippage_bps: int = config.SLIPPAGE_BPS,
                    taker: str | None = None) -> dict | None:
    """Get a swap quote via Jupiter /order.

    If `taker` is provided, Jupiter also returns the transaction to sign.
    """
    import asyncio

    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": slippage_bps,
        "prioritizationFeeLamports": str(config.PRIORITY_FEE_LAMPORTS),
        "dynamicComputeUnitLimit": "true",
    }
    if taker:
        params["taker"] = taker

    for attempt in range(1, 3):  # 2 attempts (quotes expire fast — limited retries)
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(
                    f"{config.JUPITER_API_URL}/order",
                    params=params,
                    headers=_headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            print(f"  [JUPITER /order] attempt {attempt}/2 — HTTP {e.response.status_code}: {e.response.text[:300]}")
            return None  # Jupiter error (unknown token, no route, etc.) — no point retrying
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            err_msg = str(e) or "(empty network error)"
            print(f"  [JUPITER /order] attempt {attempt}/2 — [{type(e).__name__}] {err_msg}")
            if attempt < 2:
                await asyncio.sleep(1)
        except Exception as e:
            print(f"  [JUPITER /order] [{type(e).__name__}] {e or '(empty)'}")
            return None

    return None


async def execute_swap(signed_tx_base64: str, request_id: str | None = None) -> dict | None:
    """Execute a swap via Jupiter /execute (handles landing and retries)."""
    import asyncio

    payload = {
        "signedTransaction": signed_tx_base64,
    }
    if request_id:
        payload["requestId"] = request_id

    headers = _headers()
    headers["Content-Type"] = "application/json"

    last_error: str = ""
    for attempt in range(1, 4):  # max 3 attempts
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{config.JUPITER_API_URL}/execute",
                    json=payload,
                    headers=headers,
                )
            # Log the response on error for debugging
            if resp.status_code != 200:
                body = resp.text[:500]
                print(f"  [JUPITER /execute] attempt {attempt}/3 — Status {resp.status_code}: {body}")
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code in (400, 404, 422):
                    # Client error — no point retrying
                    return None
                await asyncio.sleep(2)
                continue
            return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            err_msg = str(e) or "(empty network error)"
            print(f"  [JUPITER /execute] attempt {attempt}/3 — [{type(e).__name__}] {err_msg}")
            last_error = f"{type(e).__name__}: {err_msg}"
            if attempt < 3:
                await asyncio.sleep(2)
        except Exception as e:
            print(f"  [JUPITER /execute] attempt {attempt}/3 — [{type(e).__name__}] {e or '(empty)'}")
            last_error = f"{type(e).__name__}: {e}"
            break  # unexpected error — no retry

    print(f"  [JUPITER /execute] Failed after 3 attempts — last error: {last_error}")
    return None


def analyze_quote(data: dict) -> dict:
    """Analyze a Jupiter quote for spread and slippage."""
    # The /order response may contain info directly or in a nested object
    in_amount = int(data.get("inAmount", 0))
    out_amount = int(data.get("outAmount", 0))
    other_amount_threshold = int(data.get("otherAmountThreshold", 0))

    # Effective price
    if in_amount > 0 and out_amount > 0:
        effective_price = out_amount / in_amount
    else:
        effective_price = 0

    # Estimated slippage
    if out_amount > 0 and other_amount_threshold > 0:
        slippage_pct = (out_amount - other_amount_threshold) / out_amount
    else:
        slippage_pct = 0

    # Price impact
    price_impact_pct = float(data.get("priceImpactPct", "0"))

    return {
        "in_amount": in_amount,
        "out_amount": out_amount,
        "effective_price": effective_price,
        "slippage_pct": slippage_pct,
        "price_impact_pct": price_impact_pct,
    }
