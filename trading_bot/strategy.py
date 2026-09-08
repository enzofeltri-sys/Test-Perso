"""
strategy.py
-----------
Stratégie multi-signaux avec confirmation, long-only.

Idée générale : au lieu de se fier à un seul indicateur (qui produit
beaucoup de faux signaux), on demande à plusieurs signaux indépendants
de "voter", et on n'agit que quand ils convergent suffisamment. On
ajoute ensuite un filtre de sécurité (RSI) et un calcul de stop-loss /
take-profit basé sur la volatilité réelle du marché (ATR), plutôt qu'un
pourcentage fixe arbitraire.

Les 3 votes (chacun vaut -1, 0 ou +1) :
  - tendance   : EMA rapide au-dessus/en-dessous de l'EMA lente
  - momentum   : ligne MACD au-dessus/en-dessous de sa ligne de signal
  - volume     : le volume actuel confirme-t-il le mouvement (au-dessus
                 de sa moyenne mobile) ? (ne vote jamais négatif : un
                 volume faible n'est pas un signal baissier en soi,
                 juste une absence de confirmation)

Le filtre RSI sert de garde-fou : même si les votes sont positifs, on
n'entre pas si le RSI indique déjà un marché en surachat extrême — le
risque d'acheter juste avant un retournement est trop élevé.

Rien de tout ça n'est une martingale : c'est une manière plus robuste
de filtrer les faux signaux qu'un simple croisement de moyennes, pas
une garantie de gains.
"""

from dataclasses import dataclass

import pandas as pd

import indicators as ind


@dataclass
class StrategyParams:
    ema_fast: int = 12
    ema_slow: int = 26
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_veto_overbought: float = 75.0
    volume_ma_period: int = 20
    volume_mult: float = 1.2
    atr_period: int = 14
    atr_multiplier_stop: float = 2.0
    reward_risk_ratio: float = 2.0
    min_score_to_enter: int = 2      # sur un score qui va de -2 à +3
    # sort si le score retombe à ce niveau ou en dessous ; None = jamais de
    # sortie sur signal, seuls le stop et l'objectif ferment la position.
    # Sur bougies 1h, le score bascule à chaque croisement MACD contraire :
    # la sortie sur signal coupait les gagnants bien avant l'objectif
    # (51% des sorties, taux de gain 31%) — voir README, "Ce qu'on a observé".
    exit_score_threshold: int = None


class MultiSignalStrategy:
    def __init__(self, params: StrategyParams):
        self.p = params

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.p

        df["ema_fast"] = ind.ema(df["close"], p.ema_fast)
        df["ema_slow"] = ind.ema(df["close"], p.ema_slow)

        macd_line, macd_signal, macd_hist = ind.macd(df["close"], p.macd_fast, p.macd_slow, p.macd_signal)
        df["macd_line"] = macd_line
        df["macd_signal"] = macd_signal
        df["macd_hist"] = macd_hist

        df["rsi"] = ind.rsi(df["close"], p.rsi_period)
        df["atr"] = ind.atr(df, p.atr_period)
        df["volume_ma"] = ind.volume_sma(df["volume"], p.volume_ma_period)

        return df

    def compute_score(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.p

        trend_vote = (df["ema_fast"] > df["ema_slow"]).astype(int) * 2 - 1          # -1 ou +1
        momentum_vote = (df["macd_line"] > df["macd_signal"]).astype(int) * 2 - 1    # -1 ou +1
        volume_vote = (df["volume"] > df["volume_ma"] * p.volume_mult).astype(int)   # 0 ou +1

        df["trend_vote"] = trend_vote
        df["momentum_vote"] = momentum_vote
        df["volume_vote"] = volume_vote
        df["score"] = trend_vote + momentum_vote + volume_vote
        return df

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcule indicateurs + score en une fois. Les premières lignes
        (avant que les indicateurs les plus lents soient définis) contiennent
        des NaN et doivent être ignorées par l'appelant."""
        df = self.compute_indicators(df)
        df = self.compute_score(df)
        return df

    def should_enter(self, row) -> bool:
        p = self.p
        return (row["score"] >= p.min_score_to_enter) and (row["rsi"] < p.rsi_veto_overbought)

    def should_exit_on_signal(self, row) -> bool:
        if self.p.exit_score_threshold is None:
            return False
        return row["score"] <= self.p.exit_score_threshold

    def on_position_closed(self):
        pass

    def compute_stop_and_target(self, entry_price: float, row):
        """Stop-loss et take-profit basés sur l'ATR au moment de l'entrée.
        Un stop plus large = marché plus volatil = position plus petite
        (voir risk.py pour le lien entre distance au stop et taille de
        position)."""
        p = self.p
        stop_distance = p.atr_multiplier_stop * row["atr"]
        stop_price = entry_price - stop_distance
        target_price = entry_price + p.reward_risk_ratio * stop_distance
        return stop_price, target_price, stop_distance


class RegimeSwitchingStrategy:
    """
    Stratégie hybride : bascule entre une stratégie de tendance
    (MultiSignalStrategy) et une stratégie de retournement
    (MeanReversionStrategy) selon le régime de marché détecté par l'ADX.

    L'idée : chaque stratégie a un terrain de jeu où elle excelle et un
    terrain où elle perd de l'argent. En choisissant dynamiquement
    laquelle utiliser, on évite d'appliquer une logique de tendance sur
    un marché plat (et inversement).

    Une fois une position ouverte par l'une des deux sous-stratégies,
    c'est TOUJOURS cette même sous-stratégie qui gère sa sortie (stop,
    target, signal de sortie) — même si le régime change entre-temps —
    car le stop/target ont été calculés avec ses propres paramètres au
    moment de l'entrée.
    """

    def __init__(self, trend_strategy, range_strategy, adx_period: int = 14, adx_trend_threshold: float = 25.0,
                 regime_confirm_bars: int = 3, market_filter_ema: int = None):
        """
        market_filter_ema : filtre de marché long terme, optionnel (None =
        désactivé). Quand il est actif, AUCUNE entrée (ni tendance, ni
        retournement) n'est prise tant que le prix est sous son EMA de
        `market_filter_ema` bougies. Rationnel : le bot est long-only ; sur
        une phase baissière prolongée, la meilleure position est de ne pas
        en avoir — les entrées "contre le courant de fond" sont celles qui
        finissent le plus souvent sur un stop.
        """
        self.trend_strategy = trend_strategy
        self.range_strategy = range_strategy
        self.adx_period = adx_period
        self.adx_trend_threshold = adx_trend_threshold
        self.regime_confirm_bars = regime_confirm_bars
        self.market_filter_ema = market_filter_ema
        self._active = None  # 'trend' ou 'range', tant qu'une position est ouverte
        self._pending_active = None  # candidat proposé par should_enter(), pas encore confirmé

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        import regime as rg

        df = self.trend_strategy.compute_indicators(df)
        df = self.trend_strategy.compute_score(df)
        df = self.range_strategy.compute_indicators(df)

        adx_df = rg.compute_adx(df, self.adx_period)
        df = pd.concat([df, adx_df], axis=1)
        raw_regime = rg.classify_regime(df["adx"], self.adx_trend_threshold)
        # hystérésis : le régime effectif ne bascule qu'après confirmation
        # sur `regime_confirm_bars` bougies consécutives (voir regime.py)
        df["regime"] = rg.debounce_regime(raw_regime, self.regime_confirm_bars).values

        if self.market_filter_ema:
            # EMA "tolérante au démarrage" (adjust=True, pas de min_periods) :
            # une EMA longue avec min_periods=span mettrait NaN — donc
            # supprimerait — les `span` premières bougies de chaque fenêtre
            # de backtest/walk-forward et de chaque cycle live. Ici les
            # premières valeurs sont juste la moyenne pondérée de l'historique
            # disponible : moins fiables au tout début, jamais absentes.
            market_ema = df["close"].ewm(span=self.market_filter_ema, adjust=True, min_periods=1).mean()
            df["market_ema"] = market_ema
            df["market_ok"] = df["close"] > market_ema
        return df

    def should_enter(self, row) -> bool:
        """Pure prédicat côté état confirmé : propose un régime candidat dans
        `_pending_active` sans toucher à `_active` tant que l'entrée n'est pas
        confirmée par un appel à `compute_stop_and_target()`. Sans cette
        distinction, un appelant (portefeuille) qui évalue plusieurs candidats
        avant d'allouer les slots disponibles pourrait laisser `_active` sur un
        régime alors qu'aucune position n'a réellement été ouverte pour ce
        symbole — et donc fausser `active_regime` / `should_exit_on_signal`."""
        regime = row["regime"]
        if pd.isna(regime):
            return False
        if self.market_filter_ema and not row["market_ok"]:
            return False
        if regime == "trending":
            if self.trend_strategy.should_enter(row):
                self._pending_active = "trend"
                return True
        else:
            if self.range_strategy.should_enter(row):
                self._pending_active = "range"
                return True
        return False

    def should_exit_on_signal(self, row) -> bool:
        if self._active == "trend":
            return self.trend_strategy.should_exit_on_signal(row)
        elif self._active == "range":
            return self.range_strategy.should_exit_on_signal(row)
        return False

    def compute_stop_and_target(self, entry_price: float, row):
        """Appelé uniquement lorsque l'entrée proposée par should_enter() est
        réellement exécutée : c'est le seul moment où on confirme le régime
        actif pour la durée de la position."""
        self._active = self._pending_active
        if self._active == "range":
            return self.range_strategy.compute_stop_and_target(entry_price, row)
        return self.trend_strategy.compute_stop_and_target(entry_price, row)

    def on_position_closed(self):
        """À appeler par le moteur de backtest/trading après une sortie,
        pour remettre l'état à neutre avant la prochaine entrée."""
        self._active = None

    @property
    def active_regime(self):
        return self._active


def strategy_from_config(strat_cfg: dict) -> MultiSignalStrategy:
    params = StrategyParams(
        ema_fast=strat_cfg["ema_fast"],
        ema_slow=strat_cfg["ema_slow"],
        macd_fast=strat_cfg["macd_fast"],
        macd_slow=strat_cfg["macd_slow"],
        macd_signal=strat_cfg["macd_signal"],
        rsi_period=strat_cfg["rsi_period"],
        rsi_veto_overbought=strat_cfg["rsi_veto_overbought"],
        volume_ma_period=strat_cfg["volume_ma_period"],
        volume_mult=strat_cfg["volume_mult"],
        atr_period=strat_cfg["atr_period"],
        atr_multiplier_stop=strat_cfg["atr_multiplier_stop"],
        reward_risk_ratio=strat_cfg["reward_risk_ratio"],
        min_score_to_enter=strat_cfg["min_score_to_enter"],
        exit_score_threshold=strat_cfg.get("exit_score_threshold"),
    )
    return MultiSignalStrategy(params)


def regime_strategy_from_config(strategy_cfg: dict) -> RegimeSwitchingStrategy:
    """strategy_cfg = cfg['strategy'] entier, avec ses sous-sections
    'trend', 'mean_reversion' et 'regime'."""
    from mean_reversion import mean_reversion_from_config

    trend_strategy = strategy_from_config(strategy_cfg["trend"])
    range_strategy = mean_reversion_from_config(strategy_cfg["mean_reversion"])
    regime_cfg = strategy_cfg["regime"]
    market_cfg = strategy_cfg.get("market_filter") or {}

    return RegimeSwitchingStrategy(
        trend_strategy=trend_strategy,
        range_strategy=range_strategy,
        adx_period=regime_cfg["adx_period"],
        adx_trend_threshold=regime_cfg["adx_trend_threshold"],
        regime_confirm_bars=regime_cfg.get("regime_confirm_bars", 3),
        market_filter_ema=market_cfg.get("ema_period"),
    )
