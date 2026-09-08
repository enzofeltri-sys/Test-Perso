# Bot de trading crypto — stratégie hybride, portefeuille, validation walk-forward

Avant tout, une chose à garder en tête en lisant ce qui suit : **aucun bot,
aussi sophistiqué soit-il, ne garantit de gagner de l'argent**. Les
marchés crypto sont liquides, très suivis et compétitifs — un avantage
("edge") systématique et durable, accessible avec des données publiques
et une stratégie mécanique, est rare. Ce que ce projet apporte, ce n'est
pas une martingale, mais une méthode plus rigoureuse pour tester une
idée honnêtement : plusieurs signaux qui doivent converger, une gestion
du risque qui protège le capital même quand la stratégie se trompe, un
portefeuille diversifié plutôt qu'un pari sur un seul actif, et surtout
une validation qui essaie activement de démontrer que la stratégie NE
fonctionne PAS (walk-forward + Monte Carlo), plutôt qu'un seul backtest
qu'on garde parce qu'il a l'air bien. Rien ici ne constitue un conseil
en investissement.

## Vue d'ensemble

```
python main.py backtest   # simule la stratégie sur l'historique, sur tout le portefeuille
python main.py validate   # walk-forward (hors-échantillon) + Monte Carlo — la partie importante
python main.py paper      # suit le marché en direct, portefeuille virtuel, zéro argent réel
```

Aucun mode n'envoie jamais d'ordre réel (voir "Passer au réel" en bas).

**Faire tourner le paper trading en continu, gratuitement, sans dépendre
de ton ordinateur** : voir `DEPLOIEMENT.md` (Render + UptimeRobot +
Supabase). C'est une architecture différente (`web_app.py` au lieu de
`paper_trader.py`) pensée pour un hébergement gratuit qui redémarre le
service à tout moment — utile seulement si tu veux le laisser tourner
sans un ordinateur allumé en permanence.

## Installation

```bash
pip install -r requirements.txt
```

## Tests

```bash
pytest tests/
```

Tests unitaires sur les indicateurs, la gestion du risque, la détection de
régime (hystérésis), la non-fuite d'état entre paires de la stratégie à
bascule, et un test de bout en bout du backtest portefeuille + walk-forward
sur données synthétiques (pas de réseau requis).

## Architecture de la stratégie

### 1. Bascule selon le régime de marché (`regime.py`, `strategy.py`)

Un croisement de moyennes mobiles marche bien en tendance et mal en
marché plat ; une stratégie de retournement fait l'inverse. Plutôt que
choisir l'une des deux pour toujours, le bot mesure la force de la
tendance avec l'**ADX** et bascule automatiquement :

- **Régime "trending"** (ADX > 25 par défaut) → stratégie de tendance
  multi-signaux (`strategy.py`) : combine EMA rapide/lente, MACD et
  confirmation par le volume, filtrée par un RSI anti-surachat.
- **Régime "ranging"** (ADX ≤ 25) → stratégie de retournement
  (`mean_reversion.py`) : achète quand le prix casse la bande de
  Bollinger basse avec un RSI en survente, revend au retour vers la
  moyenne.

Une fois une position ouverte, c'est toujours la même sous-stratégie qui
gère sa sortie (stop, target, signal), même si le régime change en cours
de route — voir `RegimeSwitchingStrategy` dans `strategy.py`.

**Hystérésis de régime** : l'ADX oscille souvent juste autour du seuil
(25) pendant plusieurs bougies d'affilée, ce qui ferait basculer la
stratégie active pour rien. Le régime effectif ne bascule donc qu'après
`regime_confirm_bars` (3 par défaut) bougies consécutives dans le nouveau
régime (`regime.debounce_regime`) — ça a réduit le nombre de trades
d'environ 40% dans nos tests, en éliminant une partie des faux signaux
aux frontières de régime.

### 2. Portefeuille multi-actifs (`portfolio_backtester.py`, `paper_trader.py`)

Le capital est partagé entre plusieurs paires (BTC/ETH/SOL par défaut,
modifiable dans `config.yaml`) plutôt que misé sur une seule. Chaque
paire a sa propre instance de stratégie. Trois garde-fous limitent la
concentration :

- **`max_concurrent_positions`** : jamais plus de N paires en position
  simultanément.
- **Anti-corrélation** : une nouvelle position est refusée si elle est
  trop corrélée (`max_correlation_for_new_position`, corrélation
  glissante des rendements sur `correlation_lookback` bougies) avec une
  position déjà ouverte — sans ça, deux paires qui montent et descendent
  ensemble compteraient comme deux positions "diversifiées" alors
  qu'elles portent le même risque.
- **Priorisation par momentum** : si plusieurs paires signalent une
  entrée sur la même bougie mais qu'il ne reste qu'un slot disponible,
  le bot priorise celle avec le meilleur momentum récent
  (`momentum_lookback`) plutôt que l'ordre arbitraire de la liste.

### 3. Gestion du risque (`risk.py`)

- **Stop-loss / take-profit basés sur l'ATR** (volatilité récente),
  pas un pourcentage fixe arbitraire.
- **Dimensionnement par le risque** : la taille de chaque position est
  calculée pour ne jamais perdre plus de `risk_per_trade_pct` (1% par
  défaut) du capital total si le stop est touché.
- **Coupe-circuit de perte journalière** : arrête les nouvelles entrées
  après `max_daily_loss_pct` (3% par défaut) de perte sur une journée,
  se réinitialise le jour suivant.
- **Coupe-circuit de drawdown TOTAL** : contrairement au précédent,
  celui-ci regarde la baisse depuis le plus haut historique du capital
  (`max_total_drawdown_pct`, 20% par défaut) et, une fois déclenché,
  **reste déclenché** — il n'y a pas de "jour suivant" qui repart à
  zéro. L'idée : une perte de cette ampleur doit être analysée par un
  humain, pas juste attendre que ça reparte.
- **Slippage simulé** (`slippage_pct`) — appliqué à CHAQUE exécution
  (backtest, paper trading local et déployé), pour ne pas se mentir avec
  des prix d'exécution parfaits que même un exchange réel ne donnerait
  jamais.
- **Minimum d'ordre de l'exchange** : avant chaque entrée, le bot
  interroge les limites réelles de l'exchange (quantité et/ou valeur
  notionnelle minimum par paire, via ccxt) et refuse le trade si la
  taille calculée par le risque tombe en dessous — plutôt que
  d'arrondir vers le haut, ce qui reviendrait à risquer plus que
  `risk_per_trade_pct` sans le décider explicitement. Best-effort : si
  l'exchange ne publie pas ces limites pour une paire, aucun plancher
  n'est appliqué pour cette paire.

### 4. Validation — la partie qui compte vraiment

Un backtest classique répond à "comment cette stratégie aurait performé
sur CET historique précis". C'est une question dangereuse à elle seule,
parce qu'il est presque toujours possible d'ajuster des paramètres
jusqu'à obtenir un joli résultat SUR CET historique — sans que ça
signifie quoi que ce soit pour l'avenir. Deux outils pour contrer ça :

**Walk-forward (`walk_forward.py`)** : découpe l'historique en fenêtres
glissantes. Sur chaque fenêtre, les paramètres sont choisis UNIQUEMENT
avec les données d'entraînement (`train_days`), puis testés sur la
période suivante (`test_days`), jamais vue pendant le choix. On avance
et on recommence. Seuls les résultats de test (hors-échantillon) sont
agrégés — jamais les résultats d'entraînement, qui seraient
artificiellement optimistes. Chaque fenêtre entraîne et teste le
**portefeuille complet** (toutes les paires ensemble, via
`PortfolioBacktester`), avec les mêmes garde-fous que le backtest et le
paper trading réels (anti-corrélation, priorisation par momentum,
coupe-circuits partagés) — valider une paire isolée à la place ne dirait
rien sur l'effet de ces garde-fous-là.

**Monte Carlo (`monte_carlo.py`)** : un backtest ne montre qu'UN seul
chemin possible (l'ordre exact où les trades sont arrivés). Le bootstrap
ré-échantillonne les gains/pertes de chaque trade pour générer des
centaines de chemins alternatifs, et rapporte une fourchette de
résultats (percentiles) plutôt qu'un seul chiffre flatteur.

```bash
python main.py validate --config config.yaml
```

lance les deux et affiche un rapport complet, y compris `pct_windows_positive`
(fraction des fenêtres de test qui ont été profitables) et la probabilité
de finir en perte selon Monte Carlo — les deux chiffres à regarder en
premier, avant le rendement total qui peut facilement induire en erreur.

## Ce qu'on a observé en le testant

Sur des données synthétiques (marché aléatoire avec un léger cycle,
générées pour vérifier que le code fonctionne, PAS de vraies données de
marché), le backtest portefeuille complet (BTC/ETH/SOL, tous les
garde-fous v4 actifs : hystérésis de régime, anti-corrélation,
priorisation par momentum, double coupe-circuit) ressort **négatif** sur
la période testée (autour de -15 à -20% selon la graine aléatoire
utilisée pour générer les données, avec 60 à 100 trades sur l'année
simulée — nettement moins qu'avant l'ajout de l'hystérésis de régime,
qui élimine une bonne partie des faux signaux aux frontières entre
régimes, et donc des frais qui vont avec). Ce n'est pas un problème du
code : sur du bruit pur, il n'y a par construction rien à exploiter, et
le coupe-circuit de drawdown total se déclenche logiquement (et reste
déclenché, comme prévu) une fois le seuil de -20% franchi. C'est
exactement le genre de résultat que ces outils sont censés révéler
plutôt que cacher — un backtest qui a l'air très bien sur une seule
courbe, mais qui s'effondre en walk-forward ou en Monte Carlo, est un
signal de surapprentissage, pas de succès.

J'ai aussi vérifié le pipeline complet sur de vraies données en direct
(BTC/ETH/SOL/USDT simultanément, via un connecteur de marché crypto) :
avec seulement ~50 bougies par paire, il ne reste que 17 bougies
utilisables une fois les indicateurs les plus lents "chauffés", et le
bot n'a déclenché aucun trade sur cette fenêtre — mais surtout, il a
exécuté sans erreur l'ensemble du pipeline v4 sur les trois paires en
même temps : filtre anti-corrélation, priorisation par momentum,
hystérésis de régime et coupe-circuit de drawdown total, tous exercés
ensemble. C'est un test de correction mécanique ("le code tourne
correctement sur de vraies données"), pas une validation de performance
— 17 bougies ne permettent de tirer aucune conclusion sur la rentabilité.
Ce connecteur ne fournit que les ~50 dernières bougies, insuffisant pour
un vrai backtest ou walk-forward (qui ont besoin de mois d'historique) :
**avant d'utiliser ce bot pour de vrai, lance `python main.py validate`
sur ton propre ordinateur**, avec l'accès complet à l'historique via
`ccxt`, sur PLUSIEURS périodes de marché différentes (haussière,
baissière, plate) — pas seulement la période récente.

## Configuration (`config.yaml`)

Toutes les sections sont commentées dans le fichier : `portfolio`
(paires et plafond de positions), `strategy.regime` / `strategy.trend` /
`strategy.mean_reversion`, `risk`, `backtest`, `walk_forward` (fenêtres
et grille de paramètres testés), `monte_carlo`, `paper_trading`.

⚠️ Le `param_grid` du walk-forward par défaut est volontairement petit
(2×3 combinaisons) pour rester rapide. L'élargir augmente le temps de
calcul rapidement (une simulation complète par combinaison et par
fenêtre) et surtout le risque de sur-ajustement si tu élargis trop —
plus on essaie de combinaisons, plus il est facile d'en trouver une qui
a l'air bonne par hasard.

## Backtest seul

```bash
python main.py backtest --config config.yaml
```

Roule le portefeuille sur tout l'historique disponible et affiche le
détail par paire (`per_symbol`), la répartition des sorties par raison
(stop-loss / take-profit / signal), et génère `backtest_equity_curve.png`.

## Paper trading (virtuel, en direct)

```bash
python main.py paper --config config.yaml
python main.py paper --config config.yaml --once   # une seule itération, pour tester
```

Suit toutes les paires du portefeuille en direct, avec le même moteur de
risque que le backtest. Portefeuille virtuel, aucune clé API, aucun
ordre réel. Logue chaque trade simulé (avec la paire concernée) dans
`logs/paper_trades.csv`. Laisse tourner plusieurs semaines avant
d'envisager le réel — et compare ce qui se passe en live à ce que le
walk-forward avait laissé attendre.

## Recalibrage automatique (`recalibrate.py`)

```bash
python recalibrate.py --dry-run   # calcule et affiche, n'écrit jamais
python recalibrate.py             # calcule et écrit si robuste
```

Un script séparé, indépendant du déploiement continu (`web_app.py`),
pensé pour tourner une fois par mois (voir `DEPLOIEMENT.md`, section
3ter, pour le déployer en Cron Job Render). Il télécharge l'historique
réel, relance le walk-forward avec le `param_grid` de `config.yaml`, et
— seulement si les fenêtres hors-échantillon RÉCENTES sont robustes
(rendement composé positif sur plusieurs fenêtres consécutives, pas une
seule fenêtre isolée) — écrit les nouveaux paramètres dans Supabase
(`tradingbot_config.strategy_overrides`), que `web_app.py` applique au
tick suivant. Ne touche jamais aux paramètres de RISQUE (ceux-là restent
sous contrôle humain exclusif via `tradingbot_config`) ni ne place
d'ordre. N'écrit rien si les critères de robustesse ne sont pas remplis.

## Passer au réel — à lire avant d'aller plus loin

Ce projet s'arrête volontairement avant l'envoi d'ordres réels.
`paper_trader.py` contient en bas un bloc de notes détaillant les étapes
nécessaires (testnet d'abord, gestion des erreurs et rate limits,
passage des stop-loss/take-profit en vrais ordres côté exchange,
stockage sécurisé des clés API, surveillance active, montée en capital
progressive). Une erreur de code avec de l'argent réel coûte bien plus
cher qu'en simulation — quand tu voudras franchir cette étape, on la
fera ensemble, progressivement.

## Limites connues

- Un seul sens (long-only), pas de vente à découvert, pas de levier.
- Le stop-loss/take-profit est vérifié sur le high/low de chaque bougie
  avec slippage simulé, mais reste une approximation : pas de vraie
  latence réseau, pas de carnet d'ordres. Si le stop ET le take-profit
  sont techniquement touchés dans la MÊME bougie, on ne peut pas savoir
  dans quel ordre c'est arrivé (on n'a que high/low/close, pas la
  séquence intra-bougie) — le code tranche systématiquement en faveur du
  stop-loss, l'hypothèse la plus conservatrice, mais ça reste une
  approximation à garder en tête sur les bougies larges (1h+).
- Le filtre anti-corrélation utilise une corrélation glissante calculée
  sur l'historique récent (`correlation_lookback`) — or c'est justement
  pendant les périodes de stress de marché que les corrélations entre
  cryptos ont tendance à monter brutalement (tout baisse ensemble), donc
  ce filtre peut sous-estimer le risque de concentration au moment précis
  où il compte le plus.
- Le bootstrap Monte Carlo suppose les trades indépendants, ce qui
  sous-estime probablement un peu le risque réel de drawdown (des pertes
  peuvent être groupées dans le temps, par exemple pendant un mois de
  marché difficile pour cette stratégie).
- Walk-forward et Monte Carlo réduisent le risque de surapprentissage
  mais ne l'éliminent pas, et ne disent rien sur des conditions de
  marché qui n'existent pas encore dans l'historique testé.

## Avertissement

Ceci est un outil éducatif de simulation et de validation. Rien ici ne
constitue un conseil en investissement. Les performances passées
(backtest, walk-forward ou paper trading) ne préjugent en rien des
performances futures, et le trading d'actifs comme les cryptomonnaies
comporte un risque de perte en capital.
