"""SolMoon Bot — Solana memecoin auto-trader entry point."""

import asyncio
import signal
import sys
from datetime import datetime


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [MAIN] {msg}")


async def run_meme_scanner():
    """Lance le scanner de meme coins."""
    from meme_scanner import MemeScanner

    scanner = MemeScanner()
    try:
        await scanner.run(interval_seconds=60)
    except asyncio.CancelledError:
        scanner.stop()
        log("Meme scanner arrete")
    except Exception as e:
        log(f"Erreur meme scanner: {e}")


async def run_meme_trader():
    """Lance le meme trader automatique avec rug check."""
    from meme_trader import MemeTrader

    trader = MemeTrader()
    trader.paper_mode = False  # ← changer en False pour le live
    try:
        await trader.run(scan_interval=60)
    except asyncio.CancelledError:
        trader.stop()
        log("Meme trader arrete")
    except Exception as e:
        log(f"Erreur meme trader: {e}")


async def main():
    """Point d'entree principal — meme trader uniquement."""
    log("=" * 60)
    log("SOLANA TRADING SYSTEM")
    log("=" * 60)
    log("Scalping bot: DESACTIVE (économise le rate limit Alchemy)")
    log("Copy trading: DESACTIVE (géré par wallet_tracker dans meme trader)")
    log("Meme trader:  ACTIVE — LIVE | rug check activé | copy trade intégré")
    log("=" * 60)

    # Creer les taches
    tasks = []

    # Scalping bot et copytrade désactivés :
    # — tous deux tournaient en paper mode (aucun vrai trade)
    # — leur [TRACKER] pollingait 18 wallets × 30s → 429 Alchemy en cascade
    # — ces 429 bloquaient les sells du meme trader (cf. cap +421% SELL INCERTAIN)
    # — le copy trade est déjà géré par wallet_tracker.py dans le meme trader

    # Lancer le meme trader (remplace le simple scanner)
    meme_task = asyncio.create_task(run_meme_trader())
    tasks.append(meme_task)

    # Gestion propre de l'arret
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def handle_shutdown():
        log("Signal d'arret recu - fermeture propre...")
        stop_event.set()
        for task in tasks:
            task.cancel()

    # Enregistrer les handlers de signaux
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_shutdown)
        except NotImplementedError:
            # Windows ne supporte pas add_signal_handler
            pass

    try:
        # Attendre que toutes les taches se terminent (ou soient annulees)
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Verifier les exceptions
        for task in done:
            if task.exception():
                log(f"Tache terminee avec erreur: {task.exception()}")

        # Annuler les taches restantes
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except KeyboardInterrupt:
        log("Interruption clavier")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    log("=" * 60)
    log("SYSTEME ARRETE")
    log("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Arret.")
        sys.exit(0)
