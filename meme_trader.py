"""Auto-trader de meme coins — stratégie maximisation capital."""

import asyncio
import httpx
import time
from datetime import datetime, timezone

import config
import jupiter
import wallet
from meme_scanner import MemeCoin, MemeScanner
from wallet_tracker import WalletTracker

# ---------------------------------------------------------------------------
# Paramètres de position
# ---------------------------------------------------------------------------
MEME_MAX_POSITION_SOL   = 0.08    # 0.08 SOL par trade — capital preservation (wallet ~0.89 SOL)
MEME_TAKE_PROFIT_PCT    = 0.40    # TP final +40%
MEME_STOP_LOSS_PCT      = 0.15    # SL -15%
MEME_TIMEOUT_SECONDS    = 2700    # 45 min max
MAX_CONCURRENT_TRADES   = 2       # 2 positions max — 36% capital exposé

# Sortie partielle — déclenchée plus tôt car on entre plus haut sur le pump
PARTIAL_SELL_TRIGGER    = 0.15    # vendre 50 % de la position à +15 % (était +20%)
PARTIAL_SELL_RATIO      = 0.50    # 50 % des tokens vendus au déclencheur

# Trailing stop (sur la moitié restante)
TRAILING_ACTIVATE_PCT   = 0.12    # s'active à +12 % (était +15%)
TRAILING_DISTANCE_PCT   = 0.05    # recule de 5 % depuis le pic

# Mode MOON — copy trade uniquement (pas de TP fixe, trailing large)
MOON_TRAILING_DISTANCE  = 0.08    # 8% — laisse respirer les mega pumps
MOON_MIN_GAIN_LOG       = 0.50    # log un avertissement si on sort sous +50% en moon mode
MOON_MAX_AGE_HOURS      = 168     # 7 jours max même pour whale copy (bloque PsyopAnime 2483h, PVE 218h)

# ---------------------------------------------------------------------------
# Critères d'entrée
# ---------------------------------------------------------------------------
ENTRY_PUMP_1H_MIN       = 5.0     # +5% — momentum confirmé
ENTRY_PUMP_1H_MAX       = 50.0    # max +50% 1h — DexScreener trending = déjà pompé naturellement
ENTRY_PUMP_6H_MAX       = 500.0   # max +500% 6h — meme coins peuvent x5 et continuer
ENTRY_BUY_RATIO_MIN     = 0.50    # 50% — majorité acheteurs
ENTRY_AGE_MIN_HOURS     = 0.17    # 10min — early
ENTRY_AGE_MAX_HOURS     = 4.0     # 4h max — ultra frais
ENTRY_MIN_SCORE         = 30      # assouplit
ENTRY_MIN_LIQUIDITY     = 30_000  # $30K — small-caps OK
ENTRY_MAX_VOL_LIQ_RATIO = 12.0    # Vol 1h / Liq < 12× — filtre pump épuisé
ENTRY_MAX_RUGCHECK_RISK = 300     # on GARDE strict — pas de rug
TRADE_COOLDOWN_SECONDS  = 10800   # 3h de cooldown après exit d'un token

# ---------------------------------------------------------------------------
# Position dynamique — scale selon force du signal
# ---------------------------------------------------------------------------
POSITION_BASE_SOL       = 0.08    # scanner seul ou 1 wallet
POSITION_BOOST_2W_SOL   = 0.10    # 2 wallets alpha simultanés
POSITION_BOOST_3W_SOL   = 0.12    # 3+ wallets alpha — signal très fort

# ---------------------------------------------------------------------------
# Blacklist auto — 24h après un SL
# ---------------------------------------------------------------------------
SL_BLACKLIST_HOURS      = 24      # token blacklisté 24h après avoir déclenché le SL

# ---------------------------------------------------------------------------
# Blacklist — stablecoins, base tokens, wrapped assets jamais tradeables
# (évite l'achat accidentel d'USDC/wSOL via copy trade DexScreener pairs)
# ---------------------------------------------------------------------------
BLACKLISTED_TOKEN_ADDRESSES: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",       # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL (Marinade)
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH (Wormhole)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",   # BTC (Wormhole)
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",   # stSOL (Lido)
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",    # bSOL (BlazeStake)
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",   # jitoSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK (éviter re-achat stale)
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",   # RAY (Raydium — base token)
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",   # PYTH
}

# ---------------------------------------------------------------------------
class RugChecker:
    """Vérifie le risque de rug via rugcheck.xyz (gratuit)."""

    API = "https://api.rugcheck.xyz/v1/tokens"

    async def check(self, token_address: str) -> dict:
        result = {
            "safe": False, "score": 999, "risks": [],
            "lp_locked": False, "top10_pct": 100, "error": None,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.API}/{token_address}/report",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result["score"] = data.get("score", 999)
                    risks = data.get("risks", [])
                    result["risks"] = [r.get("name", "") for r in risks]

                    markets = data.get("markets", [])
                    if markets:
                        lp = markets[0].get("lp", {})
                        result["lp_locked"] = lp.get("lpLockedPct", 0) > 80

                    top_holders = data.get("topHolders", [])
                    if top_holders:
                        result["top10_pct"] = sum(h.get("pct", 0) for h in top_holders[:10])

                    critical = ["Freeze Authority still enabled",
                                "Mint Authority still enabled",
                                "Honeypot"]
                    has_critical = any(r in result["risks"] for r in critical)
                    result["safe"] = result["score"] < ENTRY_MAX_RUGCHECK_RISK and not has_critical

                elif resp.status_code == 404:
                    result["error"] = "Token non trouvé sur rugcheck"
                else:
                    result["error"] = f"API erreur {resp.status_code}"
        except Exception as e:
            result["error"] = str(e)
        return result


# ---------------------------------------------------------------------------
class MemeTrader:
    """Bot meme coin avec sortie partielle + trailing stop + 2 trades simultanés."""

    def __init__(self, keypair=None, pubkey: str = None):
        self.keypair  = keypair
        self.pubkey   = pubkey
        self.wallet_tracker = WalletTracker(rpc_url=config.RPC_URL)
        self.scanner  = MemeScanner(wallet_tracker=self.wallet_tracker)
        self.rug_checker = RugChecker()
        self.active_trades: dict[str, dict] = {}
        self.trade_cooldowns: dict[str, float] = {}  # token → timestamp exit
        self.skip_cache: dict[str, tuple] = {}        # token → (expiry_ts, reason)
        self.sl_blacklist: dict[str, float] = {}      # token → expiry_ts (24h après SL)
        self.trade_count = 0
        self.wins  = 0
        self.losses = 0
        self.total_pnl_sol = 0.0
        self.paper_mode = False   # LIVE MODE
        self.running = False

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [MEME-TRADER] {msg}")

    # ------------------------------------------------------------------
    async def init(self):
        if not self.keypair:
            kp = wallet.load_keypair()
            self.keypair = kp
            self.pubkey  = str(kp.pubkey())
        self.log(f"Wallet: {self.pubkey}")
        bal = await wallet.get_sol_balance(self.pubkey)
        self.log(f"Solde: {bal:.6f} SOL")
        self.log(f"Mode: {'PAPER TRADE' if self.paper_mode else '⚠️  LIVE TRADE'}")
        self.log(f"Position: {MEME_MAX_POSITION_SOL} SOL | TP: +{MEME_TAKE_PROFIT_PCT*100:.0f}% "
                 f"| SL: -{MEME_STOP_LOSS_PCT*100:.0f}% | Max trades: {MAX_CONCURRENT_TRADES}")
        self.log(f"Sortie partielle: 50% vendu à +{PARTIAL_SELL_TRIGGER*100:.0f}%"
                 f" | Trailing: activé à +{TRAILING_ACTIVATE_PCT*100:.0f}%")

    # ------------------------------------------------------------------
    async def validate_entry(self, token: MemeCoin, score: dict) -> dict:
        result = {"ok": False, "reason": "", "rug": None}
        moon_mode        = getattr(token, 'copy_trade',   False)
        new_listing_mode = getattr(token, 'new_listing',  False)

        # ── Blacklist auto SL — token ayant déjà déclenché un SL récemment ──
        # Évite de re-rentrer sur un token en distribution / tendance baissière.
        sl_expiry = self.sl_blacklist.get(token.address, 0)
        if time.time() < sl_expiry:
            remaining_h = int((sl_expiry - time.time()) / 3600) + 1
            result["reason"] = f"Blacklist auto (SL récent) — {remaining_h}h restantes"
            return result
        elif sl_expiry > 0:
            del self.sl_blacklist[token.address]  # expiré, on nettoie

# ── Blacklist universelle — stablecoins / base tokens ─────────────
        # DexScreener renvoie les DEUX tokens d'une paire — sans ce filtre,
        # copier un wallet qui achète TOKEN/USDC ferait acheter USDC lui-même.
        if token.address in BLACKLISTED_TOKEN_ADDRESSES:
            result["reason"] = f"Token blacklisté (stablecoin/base): {token.symbol}"
            return result
        # Heuristique prix : un stablecoin a toujours ~$1 ± 5%
        if 0.95 <= token.price_usd <= 1.05 and token.market_cap > 500_000_000:
            result["reason"] = f"Probable stablecoin (prix ${token.price_usd:.4f}, mcap ${token.market_cap/1e9:.1f}B)"
            return result

        # ── MODE NEW LISTING (migration pump.fun → Raydium < 20min) ─────────
        # Token qui vient de lister sur Raydium : potentiel x2-x10 dans les 30min.
        # Pas de données 1h disponibles → critères adaptés.
        # Rug check STRICT (nouvelles listings = terrain de rugs fréquents).
        if new_listing_mode and not moon_mode:
            if token.price_usd <= 0 or token.price_usd < 1e-10:
                result["reason"] = "[NEW] Prix indisponible"
                return result
            if token.liquidity_usd < 15_000:
                result["reason"] = f"[NEW] Liquidité trop faible: ${token.liquidity_usd:.0f}"
                return result
            total_txns = token.txns_1h_buys + token.txns_1h_sells
            if total_txns > 10:
                buy_ratio = token.txns_1h_buys / total_txns
                if buy_ratio < 0.55:
                    result["reason"] = f"[NEW] Sell pressure: {buy_ratio*100:.0f}% buys (min 55%)"
                    return result
            if token.address in self.active_trades:
                result["reason"] = "Déjà en position sur ce token"
                return result
            cooldown_until = self.trade_cooldowns.get(token.address, 0)
            if time.time() < cooldown_until:
                remaining_min = int((cooldown_until - time.time()) / 60)
                result["reason"] = f"Cooldown actif — {remaining_min}min restantes"
                return result
            if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
                result["reason"] = f"Max {MAX_CONCURRENT_TRADES} trades simultanés atteint"
                return result
            # Rug check strict — timeout = skip (pas assez de confiance sans données)
            self.log(f"[NEW] 🆕 Rug check {token.symbol} (migration pump.fun→Raydium)...")
            try:
                rug = await asyncio.wait_for(self.rug_checker.check(token.address), timeout=8.0)
            except asyncio.TimeoutError:
                result["reason"] = "[NEW] Rugcheck timeout — skip (prudence nouvelle listing)"
                return result
            result["rug"] = rug
            risks_str = ', '.join(rug.get('risks', [])[:3]) or 'aucun'
            self.log(f"  [NEW] Score: {rug['score']} | LP locked: {rug['lp_locked']} | Risques: {risks_str}")
            critical = ["Freeze Authority still enabled", "Mint Authority still enabled", "Honeypot"]
            if any(r in rug.get("risks", []) for r in critical):
                result["reason"] = "[NEW] RUG CRITIQUE — skip"
                return result
            if rug["score"] > 300:  # seuil strict = même que mode normal
                result["reason"] = f"[NEW] RUG RISK: {rug['score']}"
                return result
            result["ok"] = True
            return result

        # ── MODE MOON (copy trade) : filtres allégés ─────────────────────
        # On fait confiance au whale — seuls rug check + position check actifs
        if moon_mode:
            if token.price_usd <= 0 or token.price_usd < 1e-10:
                # 1e-10 = prix en dessous de la précision DexScreener (s'affiche $0.00000000)
                # monitor_and_sell ne peut pas calculer de PnL correct → entrée refusée
                result["reason"] = f"[MOON] Prix trop petit ou nul: ${token.price_usd:.2e}"
                return result
            if token.liquidity_usd < 5_000:   # seuil minimal anti-honeypot
                result["reason"] = f"[MOON] Liquidité trop faible: ${token.liquidity_usd:.0f}"
                return result
            # Filtre volume minimum : Kirkslop avait $6K vol/h → dead token, SL déclenché.
            # $25K minimum = liquidité suffisante pour que le copy trade soit encore actif.
            if token.volume_1h < 25_000:
                result["reason"] = f"[MOON] Volume 1h trop faible: ${token.volume_1h:.0f} — token mort ou trop early"
                return result
            if token.age_hours > MOON_MAX_AGE_HOURS:
                result["reason"] = f"[MOON] Token trop vieux: {token.age_hours:.0f}h (max {MOON_MAX_AGE_HOURS}h)"
                return result
            # Filtre entrée tardive : si déjà +60% en 5min, on rentre au pic local
            # Le whale a acheté, les copycats ont suivi, la vague est passée.
            # Seuil 60% (pas 40%) car les gros pumps MOON peuvent continuer même après un spike initial.
            if token.price_change_5m > 60:
                result["reason"] = f"[MOON] Entrée tardive: +{token.price_change_5m:.0f}% en 5min — pic local probable"
                return result
            # Filtre pompe déjà faite : +500%+ en 1h = le whale a acheté bien avant la détection.
            # SCRIBBLE : +1605% en 1h, seulement +14.9% en 5min → sommet déjà atteint → SL en 68s.
            # On copie le wallet APRÈS la vague, pas pendant. On passe.
            if token.price_change_1h > 500:
                result["reason"] = f"[MOON] Pompe déjà faite: +{token.price_change_1h:.0f}% en 1h — copy trop tardif"
                return result
            # Filtre dump actif 5min
            if token.price_change_5m < -15:
                result["reason"] = f"[MOON] Dump actif: {token.price_change_5m:.0f}% en 5min — whale déjà sorti"
                return result
            # Filtre tendance 1h négative : si le token baisse depuis 1h, le whale a acheté
            # bien avant le signal. Tendies : -18.4% 1h + rebond +13% 5min = dead cat bounce → SL -15.8%.
            if token.price_change_1h < -15:
                result["reason"] = f"[MOON] Tendance baissière 1h: {token.price_change_1h:.0f}% — copy trop tardif"
                return result
            if token.address in self.active_trades:
                result["reason"] = "Déjà en position sur ce token"
                return result
            cooldown_until = self.trade_cooldowns.get(token.address, 0)
            if time.time() < cooldown_until:
                remaining_min = int((cooldown_until - time.time()) / 60)
                result["reason"] = f"Cooldown actif — {remaining_min}min restantes"
                return result
            if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
                result["reason"] = f"Max {MAX_CONCURRENT_TRADES} trades simultanés atteint"
                return result
            self.log(f"[MOON] 🌙 Rug check {token.symbol} (copié depuis wallet alpha)...")
            try:
                # Hard deadline 8s — un rug check lent ne bloque pas le scan loop
                rug = await asyncio.wait_for(self.rug_checker.check(token.address), timeout=8.0)
            except asyncio.TimeoutError:
                self.log(f"  [MOON] Rugcheck timeout ({token.symbol}) — on entre quand même (whale trust)")
                rug = {"safe": True, "score": 0, "risks": [], "lp_locked": False, "top10_pct": 0, "error": "Timeout"}
            result["rug"] = rug
            rug_err = rug.get("error")
            if rug_err and rug_err != "Timeout":
                # 404 = token pas encore indexé sur rugcheck (souvent très récent)
                # Autres erreurs = API down / réseau
                # Dans les deux cas : score par défaut = 999 (pas de vraie donnée)
                # → on N'entre PAS sur score 999 par défaut, trop risqué
                not_indexed = "non trouvé" in str(rug_err) or "404" in str(rug_err)
                if not_indexed:
                    self.log(f"  [MOON] Token non indexé sur rugcheck — skip par prudence (score inconnu)")
                    result["reason"] = "[MOON] Rugcheck : token non indexé — skip"
                    return result
                else:
                    # Autre erreur API (timeout réseau etc.) → whale trust
                    self.log(f"  [MOON] Rugcheck erreur: {rug_err} — on entre quand même (whale trust)")
            else:
                risks_str = ', '.join(rug.get('risks', [])[:3]) or 'aucun'
                self.log(f"  [MOON] Score: {rug['score']} | LP locked: {rug['lp_locked']} | Risques: {risks_str}")
                critical = ["Freeze Authority still enabled", "Mint Authority still enabled", "Honeypot"]
                if any(r in rug.get("risks", []) for r in critical):
                    result["reason"] = "[MOON] RUG CRITIQUE — skip"
                    return result
                # Multi-wallet boost : 2+ wallets = confiance accrue → seuil rug relevé
                # Raisonnement : si plusieurs whales indépendants achètent simultanément,
                # la probabilité d'un rug coordonné est beaucoup plus faible.
                wallet_hits = getattr(token, 'wallet_hit_count', 0)
                max_rug_score = 600 if wallet_hits >= 2 else 450
                if wallet_hits >= 2:
                    self.log(f"  [MOON] ⚡ Signal fort: {wallet_hits} wallets ont acheté — seuil rug relevé à {max_rug_score}")
                if rug["score"] > max_rug_score:
                    result["reason"] = f"[MOON] RUG RISK trop élevé: {rug['score']}"
                    return result
            result["ok"] = True
            return result

        # ── MODE NORMAL (scanner) : filtres complets ─────────────────────
        if token.price_usd < 1e-10:
            result["reason"] = f"Prix trop petit: ${token.price_usd:.2e} (précision DexScreener insuffisante)"
            return result
        if token.price_change_1h < ENTRY_PUMP_1H_MIN:
            result["reason"] = f"Pump 1h insuffisant: {token.price_change_1h:.1f}%"
            return result
        if token.price_change_1h > ENTRY_PUMP_1H_MAX:
            result["reason"] = f"Déjà trop pompé: {token.price_change_1h:.1f}%"
            return result
        if abs(token.price_change_6h) > ENTRY_PUMP_6H_MAX:
            result["reason"] = f"Pump 6h trop élevé: {token.price_change_6h:.1f}%"
            return result

        total_txns = token.txns_1h_buys + token.txns_1h_sells
        if total_txns > 0:
            buy_ratio = token.txns_1h_buys / total_txns
            if buy_ratio < ENTRY_BUY_RATIO_MIN:
                result["reason"] = f"Trop de sells: {buy_ratio*100:.0f}% buys"
                return result

        if token.age_hours < ENTRY_AGE_MIN_HOURS:
            result["reason"] = f"Token trop récent: {token.age_hours:.1f}h"
            return result
        if token.age_hours > ENTRY_AGE_MAX_HOURS:
            result["reason"] = f"Token trop vieux: {token.age_hours:.1f}h"
            return result
        # Filtre dump actif scanner : -20% en 5min = distribution en cours.
        # BABYTROLL : -22.1% 5min à l'entrée → gap-rug de -35.8% (SL skippé).
        # Si le token baisse déjà vite, le pump 1h est terminé — on entre dans le dump.
        if hasattr(token, 'price_change_5m') and token.price_change_5m < -20:
            result["reason"] = f"Dump actif: {token.price_change_5m:.0f}% en 5min — distribution en cours"
            return result
        if token.liquidity_usd < ENTRY_MIN_LIQUIDITY:
            result["reason"] = f"Liquidité insuffisante: ${token.liquidity_usd/1000:.0f}K"
            return result
        # Filtre pump épuisé : si volume >> liquidité, tout le monde est déjà passé
        if token.liquidity_usd > 0:
            vol_liq = token.volume_1h / token.liquidity_usd
            if vol_liq > ENTRY_MAX_VOL_LIQ_RATIO:
                result["reason"] = f"Pump épuisé: Vol/Liq {vol_liq:.1f}× (max {ENTRY_MAX_VOL_LIQ_RATIO}×)"
                return result
        if score["points"] < ENTRY_MIN_SCORE:
            result["reason"] = f"Score trop bas: {score['points']}"
            return result
        if token.address in self.active_trades:
            result["reason"] = "Déjà en position sur ce token"
            return result

        # Cooldown 3h après exit
        cooldown_until = self.trade_cooldowns.get(token.address, 0)
        if time.time() < cooldown_until:
            remaining_min = int((cooldown_until - time.time()) / 60)
            result["reason"] = f"Cooldown actif — {remaining_min}min restantes"
            return result

        if len(self.active_trades) >= MAX_CONCURRENT_TRADES:
            result["reason"] = f"Max {MAX_CONCURRENT_TRADES} trades simultanés atteint"
            return result

        # Rug check — hard deadline 12s pour ne pas bloquer le scan loop
        self.log(f"Rug check {token.symbol}...")
        try:
            rug = await asyncio.wait_for(self.rug_checker.check(token.address), timeout=12.0)
        except asyncio.TimeoutError:
            self.log(f"  Rugcheck timeout ({token.symbol}) — skip par prudence")
            result["reason"] = f"Rugcheck timeout — skip"
            return result
        result["rug"] = rug

        if rug["error"]:
            self.log(f"  Rugcheck erreur: {rug['error']} — prudence")
        else:
            self.log(f"  Score: {rug['score']} | LP locked: {rug['lp_locked']} | Top10: {rug['top10_pct']:.1f}%")
            if rug["risks"]:
                self.log(f"  Risques: {', '.join(rug['risks'][:3])}")
            if not rug["safe"]:
                result["reason"] = f"RUG RISK — score: {rug['score']}"
                return result
            if rug["top10_pct"] > 70:
                result["reason"] = f"Top 10 trop concentrés: {rug['top10_pct']:.1f}%"
                return result

            # Blacklist risques spécifiques qui précèdent un rug
            rug_red_flags = [
                "Low amount of LP Providers",
                "Single LP Provider",
                "Mutable metadata",
                "Large Amount of LP Unlocked",
            ]
            for flag in rug_red_flags:
                if flag in rug["risks"]:
                    result["reason"] = f"RED FLAG: {flag}"
                    return result

        result["ok"] = True
        return result

    # ------------------------------------------------------------------
    def format_signal(self, token: MemeCoin, score: dict, rug: dict) -> str:
        self.trade_count += 1
        if rug:
            rug_err = rug.get("error")
            if rug_err and rug_err != "Timeout":
                rug_score = "ERR"   # erreur API (pas un vrai score) — ne pas afficher 999
            else:
                rug_score = rug.get("score", "?")
        else:
            rug_score = "?"
        lp_str           = "✅" if rug and rug.get("lp_locked") else "❓"
        moon_mode        = getattr(token, 'copy_trade',  False)
        new_listing_mode = getattr(token, 'new_listing', False)
        wallet_hits      = getattr(token, 'wallet_hit_count', 0)
        if moon_mode:
            source_tag = "🌙 COPY TRADE — MODE MOON"
            if wallet_hits >= 2:
                source_tag = f"🔥 COPY TRADE x{wallet_hits} WALLETS — MODE MOON"
            strat_str  = f"Trailing -{MOON_TRAILING_DISTANCE*100:.0f}% depuis pic | Pas de TP fixe | Ride until drop"
        elif new_listing_mode:
            source_tag = "🆕 NOUVELLE LISTING RAYDIUM — MODE MOON"
            strat_str  = f"Trailing -{MOON_TRAILING_DISTANCE*100:.0f}% depuis pic | Pas de TP fixe | Ride until drop"
        else:
            source_tag = "📊 SCANNER — MODE NORMAL"
            strat_str  = f"50% sorti à +{PARTIAL_SELL_TRIGGER*100:.0f}% → trailing -{TRAILING_DISTANCE_PCT*100:.0f}% | TP +{MEME_TAKE_PROFIT_PCT*100:.0f}%"
        pump5_str = f" | 5min: {token.price_change_5m:+.1f}%" if token.price_change_5m else ""
        return f"""
{'='*58}
🚀 MEME TRADE N°{self.trade_count} — {'PAPER' if self.paper_mode else 'LIVE'} | {source_tag}
Token     : {token.symbol} ({token.name})
Adresse   : {token.address}
Prix      : ${token.price_usd:.8f}
Pump 1h   : {token.price_change_1h:+.1f}%{pump5_str}
Liquidité : ${token.liquidity_usd/1000:.0f}K  |  Vol 1h: ${token.volume_1h/1000:.0f}K
Age       : {token.age_hours:.1f}h
Rug score : {rug_score}/1000 | LP locked: {lp_str}
Signaux   : {' | '.join(score['flags']) if score['flags'] else '-'}
Position  : {MEME_MAX_POSITION_SOL} SOL
Stratégie : {strat_str}
SL        : -{MEME_STOP_LOSS_PCT*100:.0f}%
Chart     : https://dexscreener.com/solana/{token.address}
{'='*58}"""

    # ------------------------------------------------------------------
    async def execute_buy(self, token: MemeCoin) -> bool:
        if self.paper_mode:
            self.log(f"[PAPER] BUY {token.symbol} @ ${token.price_usd:.8f}")
            return True

        # MOON (copy trade / new listing) : pump rapide → slippage 500 bps pour éviter
        # les "Slippage exceeded" qui forcent un retry et font acheter dans le dump.
        # QuantumCat : 150 bps → 2 échecs slippage → acheté à -49% → SL -48%.
        is_moon = getattr(token, 'copy_trade', False) or getattr(token, 'new_listing', False)
        buy_slippage = 500 if is_moon else 150

        # Position dynamique selon force du signal (wallet_hit_count)
        # 3+ wallets simultanés = signal exceptionnel → on mise plus
        # Scanner seul = signal faible → position de base
        wallet_hits = getattr(token, 'wallet_hit_count', 0)
        if wallet_hits >= 3:
            position_sol = POSITION_BOOST_3W_SOL
            self.log(f"  💰 Position boostée {position_sol} SOL ({wallet_hits} wallets simultanés)")
        elif wallet_hits >= 2:
            position_sol = POSITION_BOOST_2W_SOL
            self.log(f"  💰 Position boostée {position_sol} SOL ({wallet_hits} wallets simultanés)")
        else:
            position_sol = POSITION_BASE_SOL

        position_lamports = int(position_sol * config.LAMPORTS_PER_SOL)
        try:
            order = await jupiter.get_quote(
                config.SOL_MINT, token.address, position_lamports,
                slippage_bps=buy_slippage, taker=self.pubkey,
            )
            if not order:
                self.log(f"❌ BUY {token.symbol}: get_quote retourné vide")
                return False
            # Vérifier si Jupiter a retourné une erreur dans le body
            if "error" in order or "code" in order:
                self.log(f"❌ BUY {token.symbol}: Jupiter erreur — {order.get('error') or order.get('message', order)}")
                return False
            swap_tx = order.get("transaction") or order.get("swapTransaction")
            if not swap_tx:
                self.log(f"❌ BUY {token.symbol}: pas de transaction dans le quote — {list(order.keys())}")
                return False
            signed_tx = wallet.sign_transaction(swap_tx, self.keypair)
            result = await jupiter.execute_swap(signed_tx, request_id=order.get("requestId"))
            if result and result.get("status", "").lower() == "success":
                self.log(f"✅ BUY {token.symbol} confirmé: {result.get('signature','')[:20]}...")
                return True
            else:
                self.log(f"❌ BUY {token.symbol} échoué: status={result.get('status') if result else 'None'} | {result}")
                return False
        except Exception as e:
            self.log(f"Erreur BUY {token.symbol}: [{type(e).__name__}] {e or '(message vide)'}")
            return False

    # ------------------------------------------------------------------
    async def execute_sell(self, token: MemeCoin, ratio: float = 1.0) -> bool:
        """Vend `ratio` de la position (1.0 = tout, 0.5 = moitié)."""
        if self.paper_mode:
            pct = int(ratio * 100)
            self.log(f"[PAPER] SELL {pct}% {token.symbol}")
            return True
        try:
            token_balance = await wallet.get_token_balance(self.pubkey, token.address)
            if token_balance <= 0:
                self.log(f"Aucun {token.symbol} à vendre")
                return False

            amount_to_sell = int(token_balance * ratio)
            if amount_to_sell <= 0:
                return False

            order = await jupiter.get_quote(
                token.address, config.SOL_MINT, amount_to_sell,
                slippage_bps=300, taker=self.pubkey,
            )
            if not order:
                return False
            swap_tx = order.get("transaction") or order.get("swapTransaction")
            if not swap_tx:
                return False
            signed_tx = wallet.sign_transaction(swap_tx, self.keypair)
            result = await jupiter.execute_swap(signed_tx, request_id=order.get("requestId"))
            if result and result.get("status", "").lower() == "success":
                pct = int(ratio * 100)
                self.log(f"✅ SELL {pct}% {token.symbol} confirmé")
                return True
        except Exception as e:
            self.log(f"Erreur SELL: {e}")
        return False

    # ------------------------------------------------------------------
    async def _get_meme_price(self, token_address: str) -> float | None:
        try:
            # connect=3s : inclut le DNS lookup — évite que les dropouts DNS
            # dilatent le monitoring loop à 10-20s par itération au lieu de 5s
            timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"https://api.dexscreener.com/tokens/v1/solana/{token_address}",
                )
                if resp.status_code == 200:
                    data  = resp.json()
                    pairs = data if isinstance(data, list) else data.get("pairs", [])
                    if pairs:
                        price = float(pairs[0].get("priceUsd", 0) or 0)
                        return price if price > 0 else None
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    async def _sell_with_retry(self, token: MemeCoin, ratio: float = 1.0,
                                max_attempts: int = 5) -> bool:
        """
        Vente avec 5 tentatives et backoff exponentiel.
        Backoff [5, 10, 20, 30s] entre tentatives — couvre les 429 Alchemy
        (rate limit ~30s) et les coupures DNS/réseau brèves.

        Hard deadline de 90s par tentative via asyncio.wait_for :
        - get_token_balance :  ~3s (connect timeout)
        - get_quote Jupiter  : ~12s (1–2 essais)
        - execute_swap       : ~60s (1 essai → tx landing Solana)
        → 90s = assez pour 1 tentative complète sans geler des heures si DNS down.

        Cap +421% : sell raté avec Alchemy 429 après 3 tentatives à 3s.
        5 tentatives + 30s dernier délai = 2 min max avant abandon.
        """
        # Délais entre tentatives : 5s, 10s, 20s, 30s (couvre le cooldown 429 Alchemy)
        retry_delays = [5, 10, 20, 30]
        for attempt in range(1, max_attempts + 1):
            try:
                ok = await asyncio.wait_for(
                    self.execute_sell(token, ratio=ratio),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                self.log(
                    f"  ⚠️ SELL {token.symbol} timeout 90s "
                    f"(tentative {attempt}/{max_attempts}) — vérif wallet conseillée"
                )
                ok = False
            if ok:
                return True
            if attempt < max_attempts:
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                self.log(
                    f"  ⚠️ SELL {token.symbol} échec "
                    f"(tentative {attempt}/{max_attempts}) — retry dans {delay}s"
                )
                await asyncio.sleep(delay)
        self.log(
            f"  🚨 SELL {token.symbol} IMPOSSIBLE après {max_attempts} tentatives "
            f"— vérifie le wallet MANUELLEMENT (tokens peut-être encore présents)"
        )
        return False

    # ------------------------------------------------------------------
    async def monitor_and_sell(self, token: MemeCoin, entry_price: float,
                               moon_mode: bool = False):
        """
        Stratégie normale  : sortie partielle +20% → trailing -5% → TP +40% → SL -15%
        Mode MOON (copy trade) : pas de sortie partielle, pas de TP fixe,
                                  trailing -8% uniquement → ride le mega pump
        """
        # Paramètres selon le mode
        trailing_dist = MOON_TRAILING_DISTANCE if moon_mode else TRAILING_DISTANCE_PCT
        tp_price      = float('inf') if moon_mode else entry_price * (1 + MEME_TAKE_PROFIT_PCT)
        sl_price      = entry_price * (1 - MEME_STOP_LOSS_PCT)
        partial_px    = float('inf') if moon_mode else entry_price * (1 + PARTIAL_SELL_TRIGGER)
        start_time    = time.time()

        partial_sold    = False
        trailing_active = False
        peak_price      = entry_price
        trailing_sl     = 0.0

        mode_tag = "🌙 MOON" if moon_mode else "📊 NORMAL"
        if moon_mode:
            self.log(
                f"Monitor {token.symbol} [{mode_tag}] | Entry ${entry_price:.8f} "
                f"| Trailing -{MOON_TRAILING_DISTANCE*100:.0f}% | SL ${sl_price:.8f} "
                f"| Pas de TP fixe — ride jusqu'au trailing"
            )
        else:
            self.log(
                f"Monitor {token.symbol} [{mode_tag}] | Entry ${entry_price:.8f} "
                f"| Partial +{PARTIAL_SELL_TRIGGER*100:.0f}% @ ${partial_px:.8f} "
                f"| TP ${tp_price:.8f} | SL ${sl_price:.8f}"
            )

        last_check = time.time()
        consecutive_fails = 0
        last_known_price: float | None = None  # dernier prix confirmé (persiste entre itérations)
        last_price_ts: float = time.time()     # timestamp du dernier prix réussi
        timeout_log_ts: float = 0              # anti-spam log timeout
        blind_log_ts: float = 0               # anti-spam log "prix indisponible"

        while True:
            # ── Timeout : désactivé si trailing actif ET position profitable ──
            # Cas SAM : coupé à +21.6% alors que trailing SL gérait la sortie.
            # Si trailing_active=True et prix > entry, on laisse le trailing décider.
            elapsed_total = time.time() - start_time
            if elapsed_total >= MEME_TIMEOUT_SECONDS:
                in_profit = last_known_price is not None and last_known_price > entry_price
                if trailing_active and in_profit:
                    if time.time() - timeout_log_ts > 60:  # log 1×/min max
                        pnl_now = ((last_known_price - entry_price) / entry_price) * 100
                        self.log(
                            f"  ⏱️ {token.symbol} {int(elapsed_total / 60)}min "
                            f"— trailing actif à {pnl_now:+.1f}% — ride jusqu'au trailing stop"
                        )
                        timeout_log_ts = time.time()
                else:
                    break  # timeout réel → sortie ci-dessous

            try:
                current_price = await self._get_meme_price(token.address)
                if not current_price:
                    consecutive_fails += 1
                    blind_secs = int(time.time() - last_price_ts)
                    # Log toutes les 15s pour signaler le problème sans spammer
                    if time.time() - blind_log_ts > 15:
                        self.log(f"  ⚠️ {token.symbol}: prix indisponible depuis {blind_secs}s")
                        blind_log_ts = time.time()
                    # Vente forcée si aveugle > 90s — YENJI : 15min sans prix → SL ignoré → -20.7%
                    if blind_secs >= 90:
                        self.log(
                            f"  🚨 VENTE URGENCE {token.symbol} — prix indisponible {blind_secs}s"
                            f" (on ne peut pas gérer le SL sans prix)"
                        )
                        ok = await self._sell_with_retry(token, ratio=1.0)
                        return None  # incertain : on ne sait pas si en gain ou perte
                    await asyncio.sleep(5)
                    continue
                consecutive_fails = 0
                last_known_price = current_price  # mémorise pour le check timeout
                last_price_ts = time.time()       # timestamp du dernier prix réussi

                elapsed  = int(time.time() - start_time)
                pnl_pct  = ((current_price - entry_price) / entry_price) * 100

                # ── Mise à jour du pic ────────────────────────────────────
                if current_price > peak_price:
                    peak_price = current_price
                    if trailing_active:
                        trailing_sl = peak_price * (1 - trailing_dist)

                # ── Trailing progressif (MOON / NEW LISTING uniquement) ──────
                # Après mise à jour du pic pour que le log affiche la bonne valeur.
                # Le SL ne recule jamais (new_sl > trailing_sl toujours vérifié).
                if moon_mode and trailing_active:
                    if pnl_pct >= 50:
                        new_dist = 0.05   # -5% : sécurisation maximale au-delà de +50%
                    elif pnl_pct >= 25:
                        new_dist = 0.06   # -6% : resserrement à partir de +25%
                    else:
                        new_dist = MOON_TRAILING_DISTANCE  # -8% : zone normale < +25%
                    if new_dist < trailing_dist:  # on ne desserre jamais
                        trailing_dist = new_dist
                        new_sl = peak_price * (1 - trailing_dist)
                        if new_sl > trailing_sl:
                            trailing_sl = new_sl
                            self.log(
                                f"  🔒 Trailing resserré → -{trailing_dist*100:.0f}% "
                                f"| SL: ${trailing_sl:.8f} (PnL {pnl_pct:+.1f}%)"
                            )

                # ── SORTIE PARTIELLE +20% (mode NORMAL uniquement) ───────
                if not moon_mode and not partial_sold and current_price >= partial_px:
                    self.log(f"  💰 SORTIE PARTIELLE {token.symbol} +{pnl_pct:.1f}% — vente 50%")
                    await self._sell_with_retry(token, ratio=PARTIAL_SELL_RATIO)
                    partial_sold = True
                    self.log(f"  ✅ 50% sécurisé | Reste en ride avec trailing stop")

                # ── ACTIVATION TRAILING ──────────────────────────────────
                if not trailing_active and pnl_pct >= TRAILING_ACTIVATE_PCT * 100:
                    trailing_active = True
                    trailing_sl = peak_price * (1 - trailing_dist)
                    trail_tag = f"-{trailing_dist*100:.0f}%"
                    self.log(f"  🔒 Trailing stop activé [{trail_tag}] | SL: ${trailing_sl:.8f}")

                trail_info = f" | Trail SL ${trailing_sl:.8f}" if trailing_active else ""
                partial_info = " [50% secured]" if partial_sold else ""
                self.log(
                    f"  {token.symbol}: ${current_price:.8f} | PnL: {pnl_pct:+.1f}%"
                    f" | {elapsed}s{partial_info}{trail_info}"
                )

                # ── TP FINAL ─────────────────────────────────────────────
                if current_price >= tp_price:
                    remaining = 1.0
                    self.log(f"  🎯 TP ATTEINT {token.symbol} +{pnl_pct:.1f}%")
                    sold = await self._sell_with_retry(token, ratio=remaining)
                    return True if sold else None  # None = sell raté, position incertaine

                # ── TRAILING STOP ─────────────────────────────────────────
                if trailing_active and current_price <= trailing_sl:
                    peak_pnl = ((peak_price - entry_price) / entry_price) * 100
                    outcome_tag = "✅" if current_price > entry_price else "💀 GAP-RUG"
                    self.log(
                        f"  🔒 TRAILING STOP {token.symbol} {outcome_tag} | "
                        f"Peak +{peak_pnl:.1f}% → sorti à {pnl_pct:+.1f}%"
                    )
                    remaining = 1.0
                    sold = await self._sell_with_retry(token, ratio=remaining)
                    if not sold:
                        return None  # sell raté, position incertaine
                    return current_price > entry_price or partial_sold

                # ── STOP LOSS ─────────────────────────────────────────────
                if current_price <= sl_price:
                    self.log(f"  🛑 SL TOUCHÉ {token.symbol} {pnl_pct:.1f}%")
                    remaining = 1.0
                    sold = await self._sell_with_retry(token, ratio=remaining)
                    if not sold:
                        return None  # sell raté, position incertaine
                    return partial_sold

            except Exception as e:
                self.log(f"  Erreur monitoring: {e}")

            await asyncio.sleep(5)

        # Timeout — fermeture forcée (trailing inactif ou position négative)
        elapsed_min = int((time.time() - start_time) / 60)
        self.log(f"  ⏱️ TIMEOUT {token.symbol} ({elapsed_min}min) — fermeture")
        remaining = 1.0 - PARTIAL_SELL_RATIO if partial_sold else 1.0
        sold = await self._sell_with_retry(token, ratio=remaining)
        if not sold:
            # Sell impossible (DNS/réseau) — position incertaine
            # Les tokens sont peut-être encore dans le wallet
            return None
        # WIN si on sort en profit malgré le timeout
        if last_known_price and last_known_price > entry_price:
            return True
        return partial_sold or None

    # ------------------------------------------------------------------
    async def _wallet_poll_loop(self):
        """
        Scan les wallets alpha toutes les 15s — découplé du scan principal (30s).

        POURQUOI : avec le scan principal à 30s, on détecte l'achat du whale avec
        un délai moyen de 30-90s. Pendant ce temps le token peut déjà être +30-50%.
        En scannant les wallets séparément toutes les 15s, on réduit le délai à ~15s.

        Les résultats sont poussés dans scanner.pending_copy via add_copy_signal().
        Le scan principal lit pending_copy et résout via DexScreener — pas de double appel.
        """
        if not self.wallet_tracker or not self.wallet_tracker.ALPHA_WALLETS:
            return
        self.log("⚡ Wallet poll loop démarrée — détection copy trade toutes les 15s")
        while self.running:
            try:
                token_wallets = await self.wallet_tracker.scan_all(since_minutes=2)
                for token_addr, wallet_list in token_wallets.items():
                    for w in wallet_list:
                        self.scanner.add_copy_signal(token_addr, w)
            except Exception:
                pass  # DNS/réseau — réessai au prochain cycle
            await asyncio.sleep(15)

    # ------------------------------------------------------------------
    async def run(self, scan_interval: int = 60):
        self.running = True

        # ── Retry init jusqu'à ce que le réseau soit disponible ──────────────
        # [Errno 8] nodename nor servname = DNS dropout → le bot ne doit pas crasher,
        # il doit attendre le retour du réseau et reprendre automatiquement.
        retry = 0
        while True:
            try:
                await self.init()
                break
            except Exception as e:
                retry += 1
                wait = min(30 * retry, 300)   # 30s → 60s → 90s → … max 5min
                err_short = str(e)[:80] or type(e).__name__
                self.log(
                    f"⚠️  Réseau indisponible (tentative {retry}) — "
                    f"retry dans {wait}s | {err_short}"
                )
                await asyncio.sleep(wait)

        self.log("=" * 58)
        self.log("MEME TRADER ACTIF — STRATÉGIE MAXIMISATION CAPITAL")
        self.log(f"Entrée: pump 1h +{ENTRY_PUMP_1H_MIN}% → +{ENTRY_PUMP_1H_MAX}%")
        self.log(f"Rugcheck activé | Max {MAX_CONCURRENT_TRADES} trades simultanés")
        n_wallets = len(self.wallet_tracker.ALPHA_WALLETS)
        if n_wallets > 0:
            self.log(f"🔁 Copy trading: {n_wallets} wallet(s) alpha | poll toutes les 15s")
            for w in self.wallet_tracker.ALPHA_WALLETS:
                self.log(f"   → {w[:8]}...{w[-4:]}")
            # Lancer le wallet poll en background (15s — plus réactif que le scan 30s)
            asyncio.create_task(self._wallet_poll_loop())
        else:
            self.log("🔁 Copy trading: INACTIF (ajoute des wallets dans wallet_tracker.py)")
        self.log("=" * 58)

        while self.running:
            try:
                # Nettoyer le skip_cache des entrées expirées
                now = time.time()
                self.skip_cache = {a: v for a, v in self.skip_cache.items() if v[0] > now}

                all_tokens = await self.scanner.fetch_trending()
                if not all_tokens:
                    await asyncio.sleep(scan_interval)
                    continue

                # Séparer par mode — priorité : new_listing > copy_trade > normal
                # new_listing + copy_trade bypass filter_tokens (critères propres dans validate_entry)
                new_listing_tokens = [
                    t for t in all_tokens
                    if getattr(t, 'new_listing', False) and not getattr(t, 'copy_trade', False)
                ]
                copy_tokens   = [t for t in all_tokens if getattr(t, 'copy_trade', False)]
                normal_tokens = [
                    t for t in all_tokens
                    if not getattr(t, 'copy_trade', False) and not getattr(t, 'new_listing', False)
                ]

                new_listing_opps = [(t, t.score()) for t in new_listing_tokens
                                    if t.address and t.price_usd > 0]
                copy_opps        = [(t, t.score()) for t in copy_tokens
                                    if t.address and t.price_usd > 0]
                normal_opps      = self.scanner.filter_tokens(normal_tokens)

                # Nouvelles listings en tête — la fenêtre de 20min ferme vite
                opportunities = new_listing_opps + copy_opps + normal_opps

                for token, score in opportunities[:10]:
                    # Skip cache : si rejeté récemment, ignorer silencieusement
                    cached = self.skip_cache.get(token.address)
                    if cached and time.time() < cached[0]:
                        continue

                    validation = await self.validate_entry(token, score)
                    if not validation["ok"]:
                        reason = validation['reason']
                        self.log(f"Skip {token.symbol}: {reason}")
                        # Mettre en cache pour éviter de réévaluer trop vite
                        ttl = self._skip_ttl(reason)
                        if ttl > 0:
                            self.skip_cache[token.address] = (time.time() + ttl, reason)
                        continue

                    print(self.format_signal(token, score, validation.get("rug", {})))

                    entry_price = token.price_usd
                    bought = await self.execute_buy(token)
                    if not bought:
                        self.log(f"Achat {token.symbol} échoué")
                        # Cooldown 5 min pour éviter de re-rentrer sur un token en dump
                        # (ex: slippage exceeded pendant un pump → retry après retournement)
                        self.trade_cooldowns[token.address] = time.time() + 300
                        continue

                    self.active_trades[token.address] = {
                        "token": token,
                        "entry_price": entry_price,
                        "entry_time": time.time(),
                    }

                    # new_listing et copy_trade utilisent tous les deux le comportement MOON
                    # (trailing -8%, pas de sortie partielle, pas de TP fixe)
                    moon = getattr(token, 'copy_trade', False) or getattr(token, 'new_listing', False)
                    asyncio.create_task(self._handle_trade(token, entry_price, moon_mode=moon))
                    await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_msg = str(e)
                if "nodename nor servname" in err_msg or "Name or service not known" in err_msg \
                        or "ConnectError" in type(e).__name__:
                    self.log(f"⚠️  DNS/Réseau indisponible — retry dans {scan_interval}s")
                else:
                    self.log(f"Erreur boucle [{type(e).__name__}]: {e}")

            await asyncio.sleep(scan_interval)

    # ------------------------------------------------------------------
    async def _handle_trade(self, token: MemeCoin, entry_price: float, moon_mode: bool = False):
        try:
            result = await self.monitor_and_sell(token, entry_price, moon_mode=moon_mode)
            if result is True:
                self.wins += 1
                self.log(f"✅ WIN {token.symbol} | {self.wins}W/{self.losses}L")
            elif result is False:
                self.losses += 1
                # Blacklist 24h — évite de re-rentrer sur un token en distribution
                self.sl_blacklist[token.address] = time.time() + SL_BLACKLIST_HOURS * 3600
                self.log(f"❌ LOSS {token.symbol} → blacklist {SL_BLACKLIST_HOURS}h | {self.wins}W/{self.losses}L")
            else:
                # None = sell raté (réseau/DNS) OU timeout neutre
                # Les tokens peuvent encore être dans le wallet → avertissement critique
                self.log(
                    f"⚠️ SELL INCERTAIN {token.symbol} — "
                    f"vérifie le wallet, tokens peut-être non vendus | {self.wins}W/{self.losses}L"
                )
        finally:
            self.active_trades.pop(token.address, None)
            self.trade_cooldowns[token.address] = time.time() + TRADE_COOLDOWN_SECONDS

    def _skip_ttl(self, reason: str) -> int:
        """Durée de mise en cache silencieuse selon raison de rejet."""
        if "Pump 6h trop élevé" in reason:  return 1800  # 30 min — ça ne baisse pas vite
        if "Token trop vieux"   in reason:  return 3600  # 1h   — irréversible
        if "RUG RISK"           in reason:  return 1800  # 30 min — le score rug ne change pas
        if "RED FLAG"           in reason:  return 3600  # 1h   — red flag permanent
        if "RUG CRITIQUE"       in reason:  return 3600  # 1h   — rug permanent
        if "Déjà trop pompé"    in reason:  return 1200  # 20 min
        if "Pump épuisé"        in reason:  return 1200  # 20 min
        if "Trop de sells"      in reason:  return 600   # 10 min
        if "Sell pressure"      in reason:  return 300   # 5 min — peut s'inverser vite
        if "Liquidité trop faible" in reason: return 300 # 5 min — peut changer vite
        if "[NEW]"              in reason:  return 120   # 2 min — nouvelle listing évolue vite
        return 0  # autres raisons (cooldown, max trades, etc.) → pas de cache

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
async def main():
    trader = MemeTrader()
    trader.paper_mode = False
    try:
        await trader.run(scan_interval=30)
    except KeyboardInterrupt:
        trader.stop()


if __name__ == "__main__":
    asyncio.run(main())
