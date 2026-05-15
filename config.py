"""SolMoon Bot configuration."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Wallet & RPC ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

# --- Jupiter ---
JUPITER_API_URL = os.getenv("JUPITER_API_URL", "https://api.jup.ag/swap/v2")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# --- Token mints ---
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# --- Capital ---
INITIAL_CAPITAL_SOL = float(os.getenv("INITIAL_CAPITAL", "0.020012"))
MAX_POSITION_PCT = 0.80       # 80% max per trade, 20% reserved for gas
MAX_POSITION_VOLATILE = 0.90  # 90% if volatility mode detected
GAS_RESERVE_PCT = 0.20

# --- Trading rules ---
TAKE_PROFIT_PCT = 0.015    # +1.5% (realistic, hit more often)
STOP_LOSS_PCT = 0.005      # -0.5% (fast exit, limits losses)
MAX_SPREAD_PCT = 0.003     # 0.3% max spread
MAX_SLIPPAGE_BPS = 20      # 0.2% = 20 bps
SLIPPAGE_BPS = 15          # slippage tolerance for execution
ONLY_BULLISH = True        # only enter on BULLISH trend (skip NEUTRAL)
TRAILING_STOP = True       # trailing stop locks gains
TRAILING_ACTIVATION = 0.008  # activate trailing at +0.8%
TRAILING_DISTANCE = 0.004    # trailing stop distance: 0.4%

# --- Trading pairs ---
PAIRS = [
    {"name": "SOL/USDC", "input": SOL_MINT, "output": USDC_MINT, "out_decimals": 6},
    {"name": "SOL/USDT", "input": SOL_MINT, "output": USDT_MINT, "out_decimals": 6},
]

# --- Analysis ---
MIN_VOLUME_MULTIPLIER = 1.2  # volume >= 1.2x avg over 10 candles
CANDLE_INTERVAL = "15m"
CANDLE_COUNT = 15            # 15 candles for analysis
VOLATILITY_THRESHOLD = 0.01  # +1% in 5min = volatility mode

# --- Timing ---
TRADE_TIMEOUT_SECONDS = 1800   # 30 min max per trade
POLL_INTERVAL_SECONDS = 5      # check price every 5s (was 3s)
SCAN_INTERVAL_SECONDS = 30     # scan pairs every 30s (was 15s)
PAUSE_AFTER_2SL_SECONDS = 3600 # 1h pause after 2 consecutive SL

# --- Lamports ---
SOL_DECIMALS = 9
USDC_DECIMALS = 6
LAMPORTS_PER_SOL = 10**9
PRIORITY_FEE_LAMPORTS = 100_000  # 0.0001 SOL — high fee to guarantee landing

# --- Copy Trading ---
COPY_TRADE_ENABLED = True
COPY_MIN_WALLET_SCORE = 30
COPY_MAX_POSITION_SOL = 0.02       # max SOL per copy trade
COPY_MICRO_TEST_PCT = 0.10         # 10% of capital for testing new strategies
COPY_MIN_LIQUIDITY_USD = 10000     # min liquidity in USD
COPY_MIN_TOKEN_AGE_SECONDS = 600   # 10 minutes minimum
COPY_MONITOR_INTERVAL = 30         # poll wallets every 30s (was 5s)
COPY_PAPER_TRADE = True            # paper trading mode by default
