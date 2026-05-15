"""Gestion du wallet Solana et exécution des transactions."""

import base58
import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config


def load_keypair() -> Keypair:
    """Charge le keypair depuis la clé privée en .env."""
    secret = base58.b58decode(config.PRIVATE_KEY)
    return Keypair.from_bytes(secret)


async def get_sol_balance(pubkey: str) -> float:
    """Récupère le solde SOL du wallet."""
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
    """Signe la transaction — gère 1 ou 2 signers."""
    import base64

    tx_bytes = base64.b64decode(swap_tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)

    msg = tx.message
    pubkey = keypair.pubkey()
    num_signers = msg.header.num_required_signatures
    signer_keys = list(msg.account_keys[:num_signers])

    print(f"  [SIGN] Wallet: {pubkey}")
    print(f"  [SIGN] Signers requis: {num_signers}, keys: {[str(k)[:8] + '...' for k in signer_keys]}")

    # Cas simple : 1 seul signer (nous)
    if num_signers == 1 and signer_keys[0] == pubkey:
        signed_tx = VersionedTransaction(msg, [keypair])
        return base64.b64encode(bytes(signed_tx)).decode("utf-8")

    # Cas 2+ signers : signer notre slot, laisser les autres vides
    msg_bytes = bytes(msg)
    our_sig = keypair.sign_message(msg_bytes)

    our_index = None
    for i, key in enumerate(signer_keys):
        if key == pubkey:
            our_index = i
            break

    if our_index is None:
        raise ValueError("Notre wallet n'est pas dans les signers de la transaction")

    # Reconstruire les bytes manuellement
    sig_bytes = bytearray()

    # Compact-u16 pour le nombre de signatures
    if num_signers < 128:
        sig_bytes.append(num_signers)
    else:
        sig_bytes.append((num_signers & 0x7F) | 0x80)
        sig_bytes.append(num_signers >> 7)

    for i in range(num_signers):
        if i == our_index:
            sig_bytes.extend(bytes(our_sig))
        else:
            # Slot vide (zéros) — Jupiter le remplira via /execute
            sig_bytes.extend(b'\x00' * 64)

    # Ajouter le message
    result = bytes(sig_bytes) + msg_bytes

    print(f"  [SIGN] Signature ajoutée à l'index {our_index}")
    return base64.b64encode(result).decode("utf-8")


async def get_token_balance(pubkey: str, mint: str) -> int:
    """Récupère le solde réel d'un token SPL (en unités brutes)."""
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
    # connect=3s couvre la résolution DNS + TCP handshake.
    # Sans ce timeout granulaire, un dropout DNS peut geler cette coroutine
    # indéfiniment même avec timeout=10, bloquant _sell_with_retry pendant des minutes.
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
    """Signe et envoie une transaction de swap."""
    import base64

    # Désérialiser la transaction
    tx_bytes = base64.b64decode(swap_tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)

    # Signer
    tx = VersionedTransaction(tx.message, [keypair])

    # Sérialiser pour envoi
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
        print(f"  [ERREUR TX] {data['error']}")
        return None

    return data.get("result")


async def confirm_transaction(tx_sig: str, timeout: int = 60) -> bool:
    """Attend la confirmation d'une transaction."""
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
                print(f"  [TX ECHOUEE] {status.get('err')}")
                return False

        await asyncio.sleep(2)

    print("  [TIMEOUT] Transaction non confirmée")
    return False
