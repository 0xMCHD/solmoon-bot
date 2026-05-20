"""
Quick CLI to view wallet signal performance.

Usage:
    python show_wallet_stats.py
"""
import json
import os

from meme_scanner import WALLET_STATS_FILE
from wallet_tracker import WalletTracker


def main():
    if not os.path.exists(WALLET_STATS_FILE):
        print(f"No stats file found ({WALLET_STATS_FILE}). Run the bot first.")
        return

    with open(WALLET_STATS_FILE) as f:
        stats = json.load(f)

    if not stats:
        print("Stats file is empty. No wallets have generated signals yet.")
        return

    tracked = set(WalletTracker.ALPHA_WALLETS)

    rows = []
    for addr, s in stats.items():
        sent = s.get("sent", 0)
        resolved = s.get("resolved", 0)
        expired = s.get("expired", 0)
        if sent == 0:
            continue
        rate = resolved / sent * 100
        in_list = "✓" if addr in tracked else "✗"
        rows.append((addr, sent, resolved, expired, rate, in_list))

    rows.sort(key=lambda x: -x[4])

    print()
    print("─" * 88)
    print(f"📊 Wallet signal performance — {len(rows)} wallets with signals")
    print("─" * 88)
    print(f"  {'wallet':<46s} {'sent':>5s} {'resolved':>9s} {'expired':>8s} {'rate':>7s}  active")
    print("─" * 88)
    for addr, sent, resolved, expired, rate, in_list in rows:
        emoji = "🟢" if rate >= 50 else "🟡" if rate >= 25 else "🔴"
        print(f"  {emoji} {addr:<43s} {sent:>5d} {resolved:>9d} {expired:>8d} {rate:>6.0f}%   {in_list}")
    print("─" * 88)
    print()

    # Recommendations
    cold = [r for r in rows if r[1] >= 5 and r[4] < 25 and r[5] == "✓"]
    hot = [r for r in rows if r[1] >= 5 and r[4] >= 60 and r[5] == "✓"]

    if cold:
        print(f"⚠️  {len(cold)} cold wallet(s) (≥5 signals, <25% resolved) — consider removing:")
        for addr, *_ in cold:
            print(f"     → {addr}")
        print()

    if hot:
        print(f"🔥 {len(hot)} hot wallet(s) (≥5 signals, ≥60% resolved) — keep tracking:")
        for addr, *_ in hot:
            print(f"     → {addr}")
        print()

    untracked_with_signals = [r for r in rows if r[5] == "✗"]
    if untracked_with_signals:
        print(f"🔍 {len(untracked_with_signals)} wallet(s) had signals but aren't in ALPHA_WALLETS anymore (legacy data).")


if __name__ == "__main__":
    main()
