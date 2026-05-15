"""Configuration du bot de scalping Solana."""

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
MAX_POSITION_PCT = 0.80       # 80% max par trade, 20% reserve pour gas
MAX_POSITION_VOLATILE = 0.90  # 90% si mode volatilité détecté
GAS_RESERVE_PCT = 0.20

# --- Règles de trading ---
TAKE_PROFIT_PCT = 0.015    # +1.5% (plus réaliste, atteint plus souvent)
STOP_LOSS_PCT = 0.005      # -0.5% (sortie rapide, limite les pertes)
MAX_SPREAD_PCT = 0.003     # 0.3% spread max
MAX_SLIPPAGE_BPS = 20      # 0.2% = 20 bps
SLIPPAGE_BPS = 15          # slippage toléré pour l'exécution
ONLY_BULLISH = True        # N'entrer que sur tendance HAUSSIERE (pas NEUTRE)
TRAILING_STOP = True       # Trailing stop : verrouille les gains
TRAILING_ACTIVATION = 0.008  # Active le trailing à +0.8%
TRAILING_DISTANCE = 0.004    # Distance du trailing stop : 0.4%

# --- Paires de trading ---
PAIRS = [
    {"name": "SOL/USDC", "input": SOL_MINT, "output": USDC_MINT, "out_decimals": 6},
    {"name": "SOL/USDT", "input": SOL_MINT, "output": USDT_MINT, "out_decimals": 6},
]

# --- Analyse ---
MIN_VOLUME_MULTIPLIER = 1.2  # volume >= 1.2x moyenne 10 bougies
CANDLE_INTERVAL = "15m"
CANDLE_COUNT = 15            # 15 bougies pour l'analyse
VOLATILITY_THRESHOLD = 0.01  # +1% en 5min = mode volatilité

# --- Timing ---
TRADE_TIMEOUT_SECONDS = 1800   # 30 min max par trade
POLL_INTERVAL_SECONDS = 5      # vérifier le prix toutes les 5s (était 3s)
SCAN_INTERVAL_SECONDS = 30     # scanner les paires toutes les 30s (était 15s)
PAUSE_AFTER_2SL_SECONDS = 3600 # 1h de pause après 2 SL consécutifs

# --- Lamports ---
SOL_DECIMALS = 9
USDC_DECIMALS = 6
LAMPORTS_PER_SOL = 10**9
PRIORITY_FEE_LAMPORTS = 100_000  # 0.0001 SOL — fee élevée pour garantir le landing

# --- Copy Trading ---
COPY_TRADE_ENABLED = True
COPY_MIN_WALLET_SCORE = 30
COPY_MAX_POSITION_SOL = 0.02       # max SOL par copy trade
COPY_MICRO_TEST_PCT = 0.10         # 10% du capital pour tester de nouvelles stratégies
COPY_MIN_LIQUIDITY_USD = 10000     # liquidité minimum en USD
COPY_MIN_TOKEN_AGE_SECONDS = 600   # 10 minutes minimum
COPY_MONITOR_INTERVAL = 30         # poll wallets toutes les 30s (était 5s)
COPY_PAPER_TRADE = True            # mode test (paper trading) par défaut
