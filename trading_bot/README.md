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

Ils tournent aussi automatiquement à chaque push et pull request
(`.github/workflows/tests.yml`) — Render redéploie à chaque push sans
attendre ce résultat, donc regarder le statut de ce workflow avant de
considérer un push comme bon.

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

**Filtre de marché long terme** (`strategy.market_filter.ema_period`,
200 bougies par défaut) : aucune entrée, ni tendance ni retournement,
tant que le prix est sous son EMA 200. Le bot est long-only : en phase
baissière prolongée, la meilleure position est de ne pas en avoir. Ce
filtre est né du diagnostic sur vraies données décrit plus bas — c'est
le seul changement dont l'effet a été dans le même sens quelle que soit
la longueur d'EMA testée (150 à 720 bougies). `null` pour le désactiver.

**Pas de sortie sur signal pour la jambe de tendance**
(`strategy.trend.exit_score_threshold: null`) : seuls le stop et
l'objectif ferment une position de tendance. Sur bougies 1h, le score
bascule à chaque croisement MACD contraire, et la sortie sur signal
coupait les gagnants bien avant l'objectif (même diagnostic). Remettre
un entier (ex. `0`) pour réactiver la sortie dès que le score retombe à
ce niveau.

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
- **Plafond de concentration** (`max_position_pct_of_equity`, 25% par
  défaut) : part maximale du capital dans UNE position. Le
  dimensionnement ci-dessus est inversement proportionnel à la distance
  du stop — quand la volatilité est basse, le stop est serré et la
  taille demandée explose. Mesuré sur un an de données réelles : la
  position médiane atteignait 50% du capital, et 24% des entrées
  finissaient bornées par le cash disponible, soit ~100% du capital sur
  une seule paire (cas réel du 09/09/2026 : stop à 0,75%, sizing par le
  risque = 133% du capital). Le "1% de risque par trade" restait exact
  *si le stop est honoré*, mais ne disait plus rien du risque de trou de
  cotation, où c'est toute la position qui est exposée. En walk-forward,
  ce plafond ne coûte rien en rendement (-1,27% → -0,06%) et divise
  presque par deux le pire drawdown (-5,84% → -3,16%) ; c'est aussi lui
  qui rend `max_concurrent_positions` réellement atteignable, la
  première position ne consommant plus tout le cash.
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

### Sur un an de vraies données (walk-forward, septembre 2026)

Historique 1h BTC/ETH/SOL sur 365 jours (OKX, via `export_history.py`),
walk-forward 90 jours d'entraînement / 30 de test, 9 fenêtres
hors-échantillon, frais 0,1 % + glissement 0,05 % par exécution, tous les
garde-fous actifs. Buy & hold équipondéré sur ces 9 fenêtres : **−25 %**
(année baissière).

**Diagnostic de la stratégie d'origine** (sortie sur signal à 0, pas de
filtre de marché) : **−35 %** composé, 1 fenêtre positive sur 9,
215 trades, 31 % de trades gagnants. Ce n'était pas seulement le marché :
la stratégie perdait aussi dans les fenêtres où le buy & hold faisait
+13 % ou +33 %. Les deux mécanismes en cause, lisibles dans le détail par
fenêtre (`python main.py validate` l'affiche maintenant, et
`recalibrate.py` l'écrit dans le journal partagé) :

- 51 % des sorties étaient des sorties sur signal, déclenchées par le
  premier croisement MACD contraire sur bougies 1h — les gagnants étaient
  coupés bien avant l'objectif, et chaque aller-retour coûtait 0,3 %.
- Long-only en année baissière : les deux jambes (tendance et
  retournement) prenaient des entrées contre le courant de fond, qui
  finissaient sur un stop.

**Variantes testées, toutes en walk-forward sur les mêmes 9 fenêtres**
(sélection des paramètres en entraînement uniquement, comme en prod) :

| Variante | OOS composé | fenêtres positives | trades | pire drawdown |
|---|---|---|---|---|
| origine (1h) | −34,9 % | 11 % | 215 | −13,1 % |
| stop 3×ATR, ou objectif 3R, ou sortie signal à −1 | −30 à −33 % | 11–22 % | 166–208 | −12 à −14 % |
| sans sortie sur signal | −25,6 % | 33 % | 180 | −12,8 % |
| filtre de marché EMA 200 (ou 720) | −20 à −23 % | 11–22 % | 118–133 | −6 à −8 % |
| bougies 4h (toutes variantes) | −3 à −15 % | 33–56 % | 36–66 | −6 à −8 % |
| **filtre EMA 200 + sans sortie sur signal** (retenu) | **−1,3 %** | 33 % | 98 | **−5,8 %** |
| idem, EMA 150 / 300 / 400 | −7 à −10 % | 33–44 % | 91–108 | −7 à −9 % |
| idem + stop 3×ATR, ou objectif 3R | +0,6 % / +0,1 % | 44–56 % | 72–80 | −7 % |

Lecture honnête : **aucune variante n'a d'avantage positif** sur cette
année. La configuration retenue perd ~35 points de moins que l'origine
et divise le pire drawdown par deux, avec un effet dont le *sens* est le
même pour toutes les longueurs de filtre et sur les deux timeframes ;
mais ses voisines (EMA 150/300/400, objectif 1,5R) retombent à −7/−14 %,
et les +0,6 % de `stop 3×ATR` sont dans le bruit de quelques trades.
C'est un plateau autour de zéro, pas un edge. Le bot reste donc en paper
trading, et `recalibrate.py` continue de refuser d'écrire quoi que ce
soit tant que les fenêtres récentes ne sont pas positives — c'est
exactement le cas prévu par ses garde-fous. Les données brutes de cette
analyse sont sur la branche `history-cache` (voir `export_history.py`)
pour que n'importe qui puisse rejouer ces comparaisons.

### Sur des données synthétiques (avant l'accès aux vraies données)

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
pensé pour tourner une fois par mois via un workflow GitHub Actions
programmé (voir `DEPLOIEMENT.md`, section 3ter, et
`.github/workflows/recalibrate.yml`). Il télécharge l'historique
réel, relance le walk-forward avec le `param_grid` de `config.yaml`, et
— seulement si les fenêtres hors-échantillon RÉCENTES sont robustes
(rendement composé positif sur plusieurs fenêtres consécutives, pas une
seule fenêtre isolée) — écrit les nouveaux paramètres dans Supabase
(`tradingbot_config.strategy_overrides`), que `web_app.py` applique au
tick suivant. Ne touche jamais aux paramètres de RISQUE (ceux-là restent
sous contrôle humain exclusif via `tradingbot_config`) ni ne place
d'ordre. N'écrit rien si les critères de robustesse ne sont pas remplis.

L'historique profond nécessaire au walk-forward est téléchargé depuis
`exchange.history_exchange_id` (`okx` par défaut, voir
`config.yaml`) plutôt que `exchange.id` (`kucoin`, l'exchange EN DIRECT
de `web_app.py`) : kucoin plafonne son historique 1h public à ~83 jours
quel que soit `since_days` demandé, insuffisant pour
`walk_forward.train_days + test_days` (vérifié empiriquement). `backtest`
et `validate` utilisent le même mécanisme, pour la même raison.

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
