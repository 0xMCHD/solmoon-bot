# Installation Guide — SolMoon Bot

## Prerequisites

- **Python 3.10+** (`python3 --version`)
- **A Solana wallet** with private key access (Phantom export, or fresh wallet)
- **Funded wallet** with at least 0.5 SOL (you need SOL for trades + gas)
- **RPC endpoint** — Helius recommended (free tier 100 req/s) over Alchemy (free tier ~10 req/s)
- **Jupiter API key** (optional but recommended) — [jupiter.ag](https://jup.ag)

## Step 1 — Clone & install

```bash
git clone https://github.com/0xMchd/solmoon-bot
cd solmoon-bot
python3 -m venv venv
source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Step 2 — Get an RPC endpoint

### Recommended: Helius (free tier 100 req/s)

1. Go to [helius.dev](https://helius.dev) → Sign up
2. Create a new project → Copy RPC URL
3. Format: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`

### Alternative: Alchemy

1. Go to [alchemy.com](https://alchemy.com) → Sign up
2. Create app → Solana Mainnet → Copy HTTPS URL
3. ⚠️ Free tier rate-limited — sells may fail under load

## Step 3 — Configure `.env`

```bash
cp .env.example .env
nano .env   # or your editor of choice
```

Fill in:

```env
# Wallet — your private key in base58 format
# (export from Phantom: Settings → Security → Show Private Key)
PRIVATE_KEY=your_base58_private_key_here

# RPC endpoint (use Helius!)
RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

# Jupiter API (optional — improves quote latency)
JUPITER_API_KEY=your_jupiter_key
```

⚠️ **Never commit `.env`** — already in `.gitignore`.

## Step 4 — Test in paper mode first

Open `main.py` and ensure:

```python
trader.paper_mode = True   # ← Set to True for paper trading
```

Run:

```bash
python main.py
```

You should see:
```
[MAIN] SOLANA TRADING SYSTEM
[MEME-TRADER] Wallet: YourWallet...
[MEME-TRADER] Solde: 0.XXX SOL
[MEME-TRADER] Mode: PAPER TRADE
[MEME-TRADER] 🔁 Copy trading: 15 wallet(s) alpha | poll toutes les 15s
```

Let it run **24-48h in paper mode** to validate the signals match your risk tolerance.

## Step 5 — Going live

When ready:

1. Set `trader.paper_mode = False` in `main.py`
2. Fund your wallet with **only the SOL you can afford to lose**
3. Start with `MEME_MAX_POSITION_SOL = 0.05` for first 10 trades (smaller risk)
4. Monitor logs for first 2-3 days actively

## Step 6 — Tuning (after first 20-30 trades)

Edit `meme_trader.py` constants:

- If too many SL: lower `MEME_STOP_LOSS_PCT` to 0.10 or tighten `ENTRY_MIN_LIQUIDITY`
- If missing fast pumps: raise `buy_slippage` to 800 bps in `execute_buy`
- If alpha wallets aren't triggering: add more to `wallet_tracker.ALPHA_WALLETS` (find them on [gmgn.ai/sol/walletcopy](https://gmgn.ai))

## Troubleshooting

### "429 Too Many Requests"
You're rate-limited by your RPC. Switch to Helius or upgrade Alchemy tier.

### "DNS / Errno 8 nodename"
Your network has DNS issues. Run on a VPS ($5/mo Hetzner) for stability.

### "Slippage exceeded" on buys
Increase `buy_slippage` for MOON mode (currently 500 bps = 5%):

```python
# In execute_buy()
buy_slippage = 800 if is_moon else 200   # was 500/150
```

### Bot stops trading after some time
Check logs for asyncio errors. Restart with `Ctrl+C` then `python main.py`. Consider running with `systemd` or `pm2` for auto-restart on VPS.

## Recommended VPS setup

```bash
# Hetzner CX11 (~$5/mo) or DigitalOcean Basic
ssh root@your-vps
apt update && apt install python3-pip python3-venv git tmux -y
git clone ...
cd solmoon-bot && python3 -m venv venv && ...

# Run in tmux so it survives disconnects
tmux new -s bot
python main.py
# Ctrl+B then D to detach
```

## Need help?

- **OSS users**: GitHub Issues
- **Premium users**: Discord DM (you'll get access link after purchase)
