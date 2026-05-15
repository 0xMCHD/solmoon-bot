"""Interface avec Jupiter Swap API v2."""

import httpx
import config

# Headers communs avec API key
def _headers() -> dict:
    return {
        "x-api-key": config.JUPITER_API_KEY,
        "Accept": "application/json",
    }


async def get_price(input_mint: str = config.SOL_MINT,
                    output_mint: str = config.USDC_MINT) -> float | None:
    """Récupère le prix SOL via un micro-quote Jupiter /order."""
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

        # Extraire le prix depuis la réponse /order
        out_amount = int(data.get("outAmount", 0))
        if out_amount > 0:
            return out_amount / 10**config.USDC_DECIMALS

        # Tenter via le champ quote si présent
        quote = data.get("quote", {})
        out_amount = int(quote.get("outAmount", 0))
        if out_amount > 0:
            return out_amount / 10**config.USDC_DECIMALS
    except Exception:
        pass

    # Fallback CoinGecko
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


async def get_quote(input_mint: str,
                    output_mint: str,
                    amount_lamports: int,
                    slippage_bps: int = config.SLIPPAGE_BPS,
                    taker: str | None = None) -> dict | None:
    """Obtient un devis de swap via Jupiter /order.

    Si taker est fourni, Jupiter retourne aussi la transaction à signer.
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

    for attempt in range(1, 3):  # 2 tentatives (le quote expire vite — pas trop de retry)
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
            return None  # erreur Jupiter (token inconnu, no route, etc.) — pas la peine de retry
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            err_msg = str(e) or "(message réseau vide)"
            print(f"  [JUPITER /order] attempt {attempt}/2 — [{type(e).__name__}] {err_msg}")
            if attempt < 2:
                await asyncio.sleep(1)
        except Exception as e:
            print(f"  [JUPITER /order] [{type(e).__name__}] {e or '(vide)'}")
            return None

    return None


async def execute_swap(signed_tx_base64: str, request_id: str | None = None) -> dict | None:
    """Exécute un swap via Jupiter /execute (gère le landing et les retries)."""
    import asyncio

    payload = {
        "signedTransaction": signed_tx_base64,
    }
    if request_id:
        payload["requestId"] = request_id

    headers = _headers()
    headers["Content-Type"] = "application/json"

    last_error: str = ""
    for attempt in range(1, 4):  # 3 tentatives max
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{config.JUPITER_API_URL}/execute",
                    json=payload,
                    headers=headers,
                )
            # Log la réponse en cas d'erreur pour debug
            if resp.status_code != 200:
                body = resp.text[:500]
                print(f"  [JUPITER /execute] attempt {attempt}/3 — Status {resp.status_code}: {body}")
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code in (400, 404, 422):
                    # Erreur client — inutile de retry
                    return None
                await asyncio.sleep(2)
                continue
            return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            err_msg = str(e) or "(message réseau vide)"
            print(f"  [JUPITER /execute] attempt {attempt}/3 — [{type(e).__name__}] {err_msg}")
            last_error = f"{type(e).__name__}: {err_msg}"
            if attempt < 3:
                await asyncio.sleep(2)
        except Exception as e:
            print(f"  [JUPITER /execute] attempt {attempt}/3 — [{type(e).__name__}] {e or '(vide)'}")
            last_error = f"{type(e).__name__}: {e}"
            break  # erreur inattendue — pas la peine de retry

    print(f"  [JUPITER /execute] Échec après 3 tentatives — dernière erreur: {last_error}")
    return None


def analyze_quote(data: dict) -> dict:
    """Analyse un devis Jupiter pour spread et slippage."""
    # La réponse /order peut contenir les infos directement ou dans un sous-objet
    in_amount = int(data.get("inAmount", 0))
    out_amount = int(data.get("outAmount", 0))
    other_amount_threshold = int(data.get("otherAmountThreshold", 0))

    # Prix effectif
    if in_amount > 0 and out_amount > 0:
        effective_price = out_amount / in_amount
    else:
        effective_price = 0

    # Slippage estimé
    if out_amount > 0 and other_amount_threshold > 0:
        slippage_pct = (out_amount - other_amount_threshold) / out_amount
    else:
        slippage_pct = 0

    # Impact prix
    price_impact_pct = float(data.get("priceImpactPct", "0"))

    return {
        "in_amount": in_amount,
        "out_amount": out_amount,
        "effective_price": effective_price,
        "slippage_pct": slippage_pct,
        "price_impact_pct": price_impact_pct,
    }
