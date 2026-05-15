# 🌙 SolMoon — Solana Copy Trade Bot

**Auto-trade Solana memecoins by following 15+ pre-vetted alpha wallets in real-time.**

Built and battle-tested with real capital. No paper-trading promises, no "guaranteed 10x" bullshit — this is the actual code I used (and refined through losses) to follow Solana alpha wallets and ride their pumps.

---

## ⚡ What it does

- 🔁 **Copy Trade** — Polls 15+ vetted alpha wallets every 15s. When 2+ wallets buy the same token simultaneously, that's a strong signal and the bot enters.
- 📊 **Scanner Mode** — Detects trending tokens from DexScreener, Raydium new listings, GeckoTerminal trending across 5 sources.
- 🛡️ **Rug Protection** — Integrated rugcheck.xyz validation. Skips honeypots, freeze-authority-enabled tokens, copycat scams.
- 🎯 **MOON strategy** — For copy trades: trailing stop -8% with progressive tightening at +25% / +50% gains. No fixed TP — rides the full pump.
- 📈 **NORMAL strategy** — For scanner trades: partial 50% sell at +15%, trailing -5%, TP +40%, SL -15%.
- 🚨 **Auto-blacklist** — Tokens hitting SL are blacklisted 24h. No re-entry on declining trends.
- 💰 **Dynamic position sizing** — Stronger signals (3+ wallets simultaneously) get bigger positions automatically.

## 📊 Battle-tested filters

These filters were added after real losses:

| Filter | Threshold | Reason |
|---|---|---|
| Volume 1h minimum | $25K (MOON) / $5K (Scanner) | Dead tokens drained capital before |
| Dump 5min | -20% (Scanner) / -15% (MOON) | "Catching falling knives" SL'd repeatedly |
| 1h trend filter | < -15% blocks MOON entries | Following whales mid-dump = guaranteed loss |
| Pump already done | > 500% 1h blocks MOON | Whale bought way earlier, you'd buy the top |
| Failed buy cooldown | 5min | Avoid re-buy on dumping tokens after slippage failure |
| Blind monitoring | 90s timeout → force sell | If API blacks out, exit position before damage |

## 🚀 Quick start

```bash
git clone https://github.com/0xMchd/solmoon-bot
cd solmoon-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Add your wallet + RPC keys
python main.py
```

See [INSTALL.md](INSTALL.md) for full setup including RPC providers (Helius recommended over Alchemy for production).

## 🎚️ Configuration

All trading parameters in `meme_trader.py` top of file:

```python
MEME_MAX_POSITION_SOL   = 0.08    # Base position size
MEME_TAKE_PROFIT_PCT    = 0.40    # +40% TP (NORMAL mode)
MEME_STOP_LOSS_PCT      = 0.15    # -15% SL
MAX_CONCURRENT_TRADES   = 2       # Max simultaneous positions
MOON_TRAILING_DISTANCE  = 0.08    # -8% trailing for copy trades
POSITION_BOOST_2W_SOL   = 0.10    # +25% size if 2 wallets agree
POSITION_BOOST_3W_SOL   = 0.12    # +50% size if 3+ wallets agree
```

Alpha wallets list in `wallet_tracker.py` — **15 pre-vetted wallets** included (sourced from GMGN.ai, filtered by >60% win rate, >$10K weekly profit, no wash-trading flags).

## ⚠️ Disclaimer — Read this

**This bot will not make you rich.** Memecoin trading on Solana is one of the most competitive markets in crypto. You compete with:

- Jito-bundle snipers (200ms execution)
- Private mempool MEV bots
- Insiders with dev allocations
- Pump.fun snipers with custom validators

This bot's edge is **discipline + filters**, not speed. Realistic outcomes:

- **Win rate**: 30-40% (memecoins have very asymmetric distributions)
- **Average winning trade**: +25 to +40%
- **Average losing trade**: -12 to -18%
- **Best case**: rare 10x rides on pump-and-hold strategies
- **Worst case**: -100% capital if you ignore the rug filters

**Start with paper mode**. Use real capital you can afford to lose 100% of. This is not financial advice.

## 📦 What's inside

```
solmoon-bot/
├── main.py                  # Entry point
├── meme_trader.py           # Core auto-trader (1000+ lines, battle-tested)
├── meme_scanner.py          # Multi-source token discovery
├── wallet_tracker.py        # Alpha wallet copy trade detection
├── wallet.py                # Solana RPC + transaction signing
├── jupiter.py               # Jupiter Swap API v6 integration
├── config.py                # Constants
├── requirements.txt
├── .env.example
└── INSTALL.md               # Setup guide
```

## 🔑 Premium Edition

The Premium version (Gumroad — $49 one-time) includes:

- 🎯 **15 pre-vetted alpha wallets** with proven >60% win rate (vs you needing to find them yourself)
- 📲 **Telegram alerts** (trade open/close, daily PnL summary, blacklist additions)
- 🔄 **Multi-RPC failover** (Helius primary + Alchemy backup — never miss a sell)
- 📊 **Web dashboard** (live PnL, active trades, wallet performance)
- 🛡️ **Advanced rug filters** (top10 holder %, LP lock duration, mint authority renounced check)
- 💬 **Discord private support** (direct DMs to me for setup questions)
- 🔄 **Lifetime updates** (new filters, new alpha wallets added monthly)

**[Get Premium → Gumroad link]**

Free OSS version stays free forever. Premium funds further development.

## 🤝 Contributing

PRs welcome on the OSS version. Looking specifically for:

- Additional RPC provider integrations
- More rug check sources (rug.ninja, honeypot.is)
- Multi-chain expansion (Base, Hyperliquid)
- Backtesting framework

## 📜 License

MIT for OSS version. Premium version is single-user license — see `PREMIUM_LICENSE.md`.

---

**Built by [@mouradchahid](https://twitter.com/0xMchd) — 10+ years UX/Growth, lost $300 trading Solana memes to learn what works. Now sharing the receipts.**
