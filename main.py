"""SolMoon Bot — Solana memecoin auto-trader entry point."""

import asyncio
import signal
import sys
from datetime import datetime


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [MAIN] {msg}")


async def run_meme_scanner():
    """Launch the memecoin scanner."""
    from meme_scanner import MemeScanner

    scanner = MemeScanner()
    try:
        await scanner.run(interval_seconds=60)
    except asyncio.CancelledError:
        scanner.stop()
        log("Meme scanner stopped")
    except Exception as e:
        log(f"Meme scanner error: {e}")


async def run_meme_trader():
    """Launch the auto trader with rug check."""
    from meme_trader import MemeTrader

    trader = MemeTrader()
    trader.paper_mode = False  # set to True for paper trading
    try:
        await trader.run(scan_interval=60)
    except asyncio.CancelledError:
        trader.stop()
        log("Meme trader stopped")
    except Exception as e:
        log(f"Meme trader error: {e}")


async def main():
    """Main entry point — meme trader only."""
    log("=" * 60)
    log("SOLMOON BOT — SOLANA TRADING SYSTEM")
    log("=" * 60)
    log("Scalping bot:  DISABLED (preserves Alchemy rate limit)")
    log("Copy trading:  DISABLED (handled by wallet_tracker in meme trader)")
    log("Meme trader:   ACTIVE — LIVE | rug check enabled | copy trade integrated")
    log("=" * 60)

    # Build task list
    tasks = []

    # Scalping bot and legacy copytrade are disabled:
    # — both ran in paper mode (no real trades)
    # — their [TRACKER] polled 18 wallets every 30s → cascading Alchemy 429s
    # — those 429s blocked meme trader sells (e.g. cap +421% SELL UNCERTAIN incident)
    # — copy trade is already handled by wallet_tracker.py inside the meme trader

    # Launch the meme trader
    meme_task = asyncio.create_task(run_meme_trader())
    tasks.append(meme_task)

    # Clean shutdown handling
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def handle_shutdown():
        log("Shutdown signal received — closing cleanly...")
        stop_event.set()
        for task in tasks:
            task.cancel()

    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_shutdown)
        except NotImplementedError:
            # Windows does not support add_signal_handler
            pass

    try:
        # Wait for all tasks to complete (or be cancelled)
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Surface any task exceptions
        for task in done:
            if task.exception():
                log(f"Task terminated with error: {task.exception()}")

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except KeyboardInterrupt:
        log("Keyboard interrupt")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    log("=" * 60)
    log("SYSTEM STOPPED")
    log("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Bye.")
        sys.exit(0)
