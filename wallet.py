"""Solana wallet management and transaction execution."""

import base58
import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config


def load_keypair() -> Keypair:
    """Load keypair from PRIVATE_KEY in .env."""
    secret = base58.b58decode(config.PRIVATE_KEY)
    return Keypair.from_bytes(secret)


async def get_sol_balance(pubkey: str) -> float:
    """Fetch SOL balance for the wallet."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(config.RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    lamports = data.get("result", {}).get("value", 0)
    return lamports / config.LAMPORTS_PER_SOL


def sign_transaction(swap_tx_base64: str, keypair: Keypair) -> str:
    """Sign a transaction — handles 1 or 2 signers."""
    import base64

    tx_bytes = base64.b64decode(swap_tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)

    msg = tx.message
    pubkey = keypair.pubkey()
    num_signers = msg.header.num_required_signatures
    signer_keys = list(msg.account_keys[:num_signers])

    print(f"  [SIGN] Wallet: {pubkey}")
    print(f"  [SIGN] Required signers: {num_signers}, keys: {[str(k)[:8] + '...' for k in signer_keys]}")

    # Simple case: single signer (us)
    if num_signers == 1 and signer_keys[0] == pubkey:
        signed_tx = VersionedTransaction(msg, [keypair])
        return base64.b64encode(bytes(signed_tx)).decode("utf-8")

    # 2+ signers case: sign our slot, leave others empty (Jupiter fills via /execute)
    msg_bytes = bytes(msg)
    our_sig = keypair.sign_message(msg_bytes)

    our_index = None
    for i, key in enumerate(signer_keys):
        if key == pubkey:
            our_index = i
            break

    if our_index is None:
        raise ValueError("Our wallet is not among the transaction signers")

    # Rebuild bytes manually
    sig_bytes = bytearray()

    # Compact-u16 encoding for number of signatures
    if num_signers < 128:
        sig_bytes.append(num_signers)
    else:
        sig_bytes.append((num_signers & 0x7F) | 0x80)
        sig_bytes.append(num_signers >> 7)

    for i in range(num_signers):
        if i == our_index:
            sig_bytes.extend(bytes(our_sig))
        else:
            # Empty slot (zeros) — Jupiter will fill via /execute
            sig_bytes.extend(b'\x00' * 64)

    # Append the message
    result = bytes(sig_bytes) + msg_bytes

    print(f"  [SIGN] Signature added at index {our_index}")
    return base64.b64encode(result).decode("utf-8")


async def check_mint_authority(mint: str) -> dict:
    """
    On-chain mint safety check — reads the token mint account directly.

    Faster and more reliable than rugcheck.xyz (which lags/times out on
    fresh tokens — exactly the ones we target in ULTRA_EARLY).

    Two killer red flags:
        - freezeAuthority != null → dev can FREEZE your token account,
          making your sell impossible (= a form of honeypot)
        - mintAuthority != null   → dev can MINT infinite new tokens,
          diluting holders to zero

    A safe memecoin has BOTH renounced (= null).

    Returns dict:
        safe             : bool — both authorities renounced
        reason           : str
        freeze_authority : str | None
        mint_authority   : str | None
        checked          : bool — False if the RPC call failed (don't block on error)
    """
    result = {
        "safe": False, "reason": "", "freeze_authority": None,
        "mint_authority": None, "checked": False,
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    _timeout = httpx.Timeout(connect=3.0, read=6.0, write=6.0, pool=6.0)
    try:
        async with httpx.AsyncClient(timeout=_timeout) as client:
            resp = await client.post(config.RPC_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        result["reason"] = f"RPC error: {type(e).__name__} (not blocking)"
        return result  # checked=False → caller decides (we don't block on RPC failure)

    value = data.get("result", {}).get("value")
    if not value:
        result["reason"] = "mint account not found"
        return result

    info = value.get("data", {}).get("parsed", {}).get("info", {})
    freeze_auth = info.get("freezeAuthority")
    mint_auth = info.get("mintAuthority")
    result["freeze_authority"] = freeze_auth
    result["mint_authority"] = mint_auth
    result["checked"] = True

    if freeze_auth is not None:
        result["reason"] = "🧊 FREEZE AUTHORITY active — dev can freeze your sell"
        return result
    if mint_auth is not None:
        result["reason"] = "🖨️ MINT AUTHORITY active — infinite dilution risk"
        return result

    result["safe"] = True
    result["reason"] = "ok — both authorities renounced"
    return result


async def get_token_balance(pubkey: str, mint: str) -> int:
    """Fetch the actual SPL token balance (raw units)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    }
    # connect=3s covers DNS resolution + TCP handshake.
    # Without this granular timeout, a DNS dropout can freeze this coroutine
    # indefinitely even with timeout=10, blocking _sell_with_retry for minutes.
    _timeout = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=_timeout) as client:
        resp = await client.post(config.RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    accounts = data.get("result", {}).get("value", [])
    total = 0
    for acc in accounts:
        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        token_amount = info.get("tokenAmount", {})
        total += int(token_amount.get("amount", "0"))
    return total


async def send_transaction(swap_tx_base64: str, keypair: Keypair) -> str | None:
    """Sign and send a swap transaction."""
    import base64

    # Deserialize the transaction
    tx_bytes = base64.b64decode(swap_tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)

    # Sign
    tx = VersionedTransaction(tx.message, [keypair])

    # Serialize for sending
    raw_tx = base64.b64encode(bytes(tx)).decode("utf-8")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            raw_tx,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            },
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(config.RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        print(f"  [TX ERROR] {data['error']}")
        return None

    return data.get("result")


async def confirm_transaction(tx_sig: str, timeout: int = 60) -> bool:
    """Wait for a transaction to be confirmed."""
    import asyncio

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[tx_sig], {"searchTransactionHistory": False}],
    }

    for _ in range(timeout // 2):
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(config.RPC_URL, json=payload)
            data = resp.json()

        statuses = data.get("result", {}).get("value", [])
        if statuses and statuses[0]:
            status = statuses[0]
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                if status.get("err") is None:
                    return True
                print(f"  [TX FAILED] {status.get('err')}")
                return False

        await asyncio.sleep(2)

    print("  [TIMEOUT] Transaction not confirmed")
    return False
