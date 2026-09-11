# Attentes de performance — à écrire AVANT de voir les résultats

Ce document existe pour une seule raison : **empêcher la justification a
posteriori**. Si on regarde les résultats réels sans avoir noté ce qu'on
attendait, on trouvera toujours une explication qui les rend acceptables.
Les chiffres ci-dessous ont donc été figés le 9 septembre 2026, avant
d'avoir observé le moindre trade en conditions réelles avec cette version
de la stratégie.

Ne modifie pas ce fichier pour le faire coller aux résultats. S'il se
révèle faux, c'est une information — la conclusion s'écrit en dessous, on
n'efface pas la prévision.

Ce document couvre plusieurs bots indépendants (même stratégie, mêmes
règles de risque, paniers de paires différents — voir DEPLOIEMENT.md,
"Un deuxième bot en parallèle"). Chaque bot a sa propre section, ses
propres chiffres figés et son propre tableau de résultats observés :
ne compare jamais le tableau de résultats de l'un aux attentes de l'autre.

---

# Bot #1 — BTC/ETH/SOL

## Provenance des chiffres

Walk-forward sur un an de données réelles OKX (BTC, ETH, SOL en 1h),
9 fenêtres hors-échantillon de 30 jours, entraînement sur 90 jours
glissants. Exécution réelle : GitHub Actions run `34326778960`, le
09/09/2026 à 08:02 UTC, sur le commit `4650bcd`.

Configuration correspondante : filtre de marché EMA 200, pas de sortie
sur signal (`exit_score_threshold: null`), plafond de position à 25% du
capital, risque 1% par trade.

Résultat agrégé de ce run : **−0,14%** composé sur les 9 fenêtres,
pendant que le buy & hold faisait **−33,29%**.

## Ce qu'on attend en réel

| Métrique | Attendu |
|---|---|
| Trades | ~10 par mois |
| Trades gagnants | 38,9% |
| Sorties sur stop-loss | ~60% |
| Sorties sur objectif | ~38% |
| Sorties sur signal | ~2% |
| Part max d'une position | 25% du capital, jamais plus |
| Pire drawdown | −3,2% |
| Rendement | proche de 0%, quelle que soit la direction du marché |

Cette stratégie est **défensive par construction**. Elle préserve le
capital quand le marché baisse et ne capte presque rien quand il monte.
Un mois plat est le comportement nominal, pas une panne. Un mois à +8%
n'est pas une réussite : c'est un écart qui mérite d'être expliqué autant
qu'un mois à −8%.

## Ce qui invaliderait la stratégie

À évaluer **seulement à partir de 20 trades réels** (soit environ 3
semaines). En dessous, tout jugement est du bruit statistique.

- **Taux de gain nettement sous 30%** sur 20+ trades → l'avantage
  supposé n'existe pas en réel.
- **Drawdown au-delà de −8%** → le backtest sous-estimait le risque ;
  revoir le dimensionnement avant toute autre chose.
- **Exposition observée au-dessus de 25%** → le plafond ne fonctionne
  pas comme prévu. C'est un bug, pas un résultat de marché.
- **Nettement plus de 10 trades par mois** → le filtre de marché ne
  filtre pas ce qu'on croit qu'il filtre.
- **Sorties sur signal nettement au-dessus de 2%** → `exit_score_threshold`
  n'est pas appliqué comme prévu en live.

## Rappel de méthode

Le 9 septembre 2026, un walk-forward donnait +5,77% sur les 3 fenêtres
récentes. Neuf heures de données supplémentaires plus tard, le même
calcul donnait +0,98% et un verdict inverse. Un avantage réel ne
s'évapore pas en neuf heures : c'était du bruit.

Ce précédent vaut pour tout ce qui suivra. Un résultat encourageant sur
peu de données n'est pas un résultat.

## Résultats observés

_(à compléter à partir de 20 trades — ne rien conclure avant)_

| Date | Trades | Gagnants | Pire DD | Exposition max | Écart aux attentes |
|---|---|---|---|---|---|
| | | | | | |


---

# Bot #2 — BNB/XRP/LINK

Deuxième bot, déployé en parallèle du #1 pour comparer, à méthode et
règles strictement identiques, comment la même stratégie se comporte sur
un panier de paires différent (voir DEPLOIEMENT.md, "Un deuxième bot en
parallèle"). Capital de départ identique (1000 USDT virtuels), même
config de risque et de stratégie que le bot #1 — seul `portfolio.symbols`
change (`config_altbot.yaml`).

## Provenance des chiffres

Walk-forward sur un an de données réelles OKX (BNB, XRP, LINK en 1h),
9 fenêtres hors-échantillon de 30 jours, entraînement sur 90 jours
glissants — mêmes fenêtres temporelles que le run du bot #1 du
10/09/2026 (comparaison sur le même jour, mêmes bornes). Historique
exporté via GitHub Actions run `34487551050` sur le commit `cdba62c`,
calcul local sur le commit `8b0bbce`.

Résultat agrégé : **−3,95%** composé sur les 9 fenêtres, contre **−2,51%**
pour le bot #1 calculé le même jour. Le panier altcoins est plus volatil
(LINK a atteint −37% sur une fenêtre, XRP −24% sur une autre) — avec les
mêmes stops que le bot #1, ça se traduit par un drawdown plus profond.

## Ce qu'on attend en réel

| Métrique | Attendu | Pour comparaison, bot #1 |
|---|---|---|
| Trades | ~14 par mois | ~10 par mois |
| Trades gagnants | 34,1% | 38,9% |
| Sorties sur stop-loss | ~66% | ~60% |
| Sorties sur objectif | ~34% | ~38% |
| Part max d'une position | 25% du capital, jamais plus | idem |
| Pire drawdown | −5,1% | −3,2% |
| Rendement | proche de −4%, plus volatil que le bot #1 | proche de 0% |

Même stratégie défensive, mais un panier plus bruyant — donc un résultat
attendu plus négatif et plus dispersé. Ce n'est pas un signal que la
stratégie "marche moins bien" : c'est le même filtre appliqué à un
univers différent avec ses propres caractéristiques.

## Ce qui invaliderait la comparaison

Mêmes seuils absolus que le bot #1 (voir sa section), évalués **séparément**
à partir de 20 trades réels sur CE bot — ne jamais mélanger les trades
des deux bots dans un même comptage :

- Taux de gain nettement sous 25% sur 20+ trades du bot #2.
- Drawdown au-delà de −10% (le panier étant plus volatil, le seuil
  d'alerte est plus haut que pour le bot #1, pas identique).
- Exposition observée au-dessus de 25% → bug, comme pour le bot #1.
- Un écart de comportement entre backtest et réel structurellement
  DIFFÉRENT de celui du bot #1 → signe que le filtre de marché ou le
  dimensionnement réagit différemment selon la volatilité du panier,
  utile à savoir même si aucun seuil n'est franchi.

## Résultats observés

_(à compléter à partir de 20 trades du bot #2 — ne rien conclure avant)_

| Date | Trades | Gagnants | Pire DD | Exposition max | Écart aux attentes |
|---|---|---|---|---|---|
| | | | | | |

---

# Bot #3 — micro-paris diversifiés (18 paires, jusqu'à 3 rachats/paire, 10 USDT max/position)

Troisième bot, avec une méthode de dimensionnement VOLONTAIREMENT
différente des bots #1 et #2 — voir DEPLOIEMENT.md, "Un troisième bot".
Au lieu de dimensionner par le risque (% du capital selon la distance du
stop), chaque position est plafonnée à un **montant fixe** (10 USDT),
capital de départ 1000 USDT virtuels.

## v2 (11/09/2026) — rachat + panier élargi

Cette section **remplace** la version précédente (10 paires, jamais de
rachat, figée le 11/09/2026 au matin — voir l'historique git de ce
fichier pour ses chiffres exacts). Changements :

- **Rachat** : une paire peut désormais porter jusqu'à **3 positions
  simultanées** (`max_positions_per_symbol: 3`), donc jusqu'à 30 USDT sur
  une seule crypto au lieu de 10.
- **Cooldown** : `reentry_cooldown_hours: 6` — aucune nouvelle entrée sur
  une paire dans les 6h suivant la dernière activité (achat OU vente) sur
  cette même paire. Calé sur le timeframe (bougies 1h) : assez pour ne
  pas se refaire immédiatement dessus après un stop-loss sur le même
  bruit, assez court pour rester dans la même tendance journalière.
- **Panier élargi à 18 paires** (contre 10) : ADA, DOGE, AVAX, DOT, LTC,
  TRX, ATOM, BCH, ETC, XLM, ALGO, NEAR, FIL, UNI, ICP, ARB, OP, SUI —
  toutes DISTINCTES des paniers des bots #1 (BTC/ETH/SOL) et #2
  (BNB/XRP/LINK), choisies parmi les cryptos les plus liquides sur OKX.
- **`max_concurrent_positions` relevé de 10 à 25** : sans ça, chaque
  position gardée pour un rachat aurait mangé un slot qui, avant, servait
  à diversifier sur une paire supplémentaire — brider les deux en même
  temps aurait vidé l'un ou l'autre de son sens.

## Provenance des chiffres

Walk-forward sur un an de données réelles OKX (les 18 paires ci-dessus,
en 1h), 9 fenêtres hors-échantillon de 30 jours, entraînement sur 90
jours glissants. Historique exporté via GitHub Actions run `34586668527`
le 11/09/2026, calcul local sur le commit `6415794` (celui qui introduit
le mécanisme générique `max_positions_per_symbol`/`reentry_cooldown_hours`,
juste avant ce commit-ci qui l'applique au bot #3).

Deux invariants vérifiés indépendamment sur les trades produits par CE
run (pas seulement testés sur des données synthétiques) : jamais plus de
3 positions simultanées sur une même paire, jamais un rachat à moins de
6h de la dernière activité sur cette paire — 0 violation sur les deux.

Résultat agrégé : **−1,25%** composé sur les 9 fenêtres (contre −0,54%
pour la v1 à 10 paires/pas de rachat), pendant que le buy & hold moyen de
ces 18 paires faisait **−30,74%**. **830 trades** (~92,2/mois — bien plus
que les ~36,6/mois de la v1 : panier 1,8x plus large, jusqu'à 3 positions
par paire, cooldown de seulement 6h), taux de gain 34,3% (identique à la
v1), pire drawdown **−1,09%** (contre −0,53% pour la v1 — le rachat
augmente mécaniquement l'exposition maximale possible sur une paire,
30 USDT contre 10, donc un peu plus de capital en jeu au pire moment).
Sorties : 65,5% stop-loss, 34,2% objectif, 0,2% signal.

## ⚠️ Ce que ce résultat NE prouve PAS

Comme pour la v1, le drawdown encore petit en valeur absolue (−1,09%,
contre −3,2% pour le bot #1 et −5,1% pour le bot #2) reste **en grande
partie un effet mécanique** du plafond à 10 USDT/position — même avec le
rachat, l'exposition maximale théorique par paire (30 USDT) reste minime
face au capital total (1000 USDT). Ne jamais présenter "petit drawdown"
comme une victoire de la stratégie sur ce bot. Ce qui a changé par
rapport à la v1 : le rendement composé est PLUS négatif (−1,25% contre
−0,54%) et le pire drawdown PLUS profond (−1,09% contre −0,53%) — le
rachat n'a pas amélioré le résultat sur cette période, il a surtout
multiplié le nombre de trades. Ce bot mesure toujours le comportement
RELATIF de la stratégie sur un large panier (et maintenant, avec rachat)
— utile pour apprendre, pas pour juger si "racheter marche mieux".

## Ce qu'on attend en réel

| Métrique | Attendu |
|---|---|
| Trades | ~92 par mois (nettement plus que la v1 et que les bots #1/#2) |
| Trades gagnants | 34,3% |
| Sorties sur stop-loss | ~66% |
| Sorties sur objectif | ~34% |
| Part max d'une position | 10 USDT fixes, jamais plus — PAS une part du capital |
| Positions simultanées (toutes paires) | jusqu'à 25 |
| Positions simultanées (une même paire) | jusqu'à 3 (30 USDT max sur une crypto) |
| Cooldown de rachat | jamais moins de 6h entre deux activités sur la même paire |
| Pire drawdown | proche de 0% par construction, mais plus marqué qu'en v1 (voir avertissement) |
| Rendement | proche de 0% à légèrement négatif — l'ampleur en dollars compte plus que le % ici |

## Ce qui invaliderait la configuration (pas seulement la stratégie)

À évaluer à partir de 20 trades réels **de ce bot uniquement**
(`select count(*) from microbot_trades where side='sell';` — jamais
mélangé aux comptages des bots #1/#2) :

- **Une position au-dessus de 10 USDT observée** → bug du plafond fixe,
  pas un résultat de marché : c'est LE mécanisme central de ce bot, une
  violation ici est plus grave que pour les bots #1/#2.
- **Plus de 3 positions simultanées sur une même paire** →
  `max_positions_per_symbol` non respecté, bug de portefeuille.
- **Un rachat à moins de 6h de la dernière activité sur cette paire** →
  `reentry_cooldown_hours` non respecté, bug de portefeuille.
- **Plus de 25 positions simultanées au total** →
  `max_concurrent_positions` non respecté, bug de portefeuille.
- **Un nombre de trades très inférieur à 92/mois** → les minimums d'ordre
  de l'exchange (`min_cost`/`min_amount`) rejettent probablement une
  bonne partie des signaux à 10 USDT sur certaines des nouvelles paires.
  Si observé, le dire explicitement plutôt que de laisser croire à un
  filtre de marché plus strict que prévu.
- Un drawdown ou un rendement qui s'écarte nettement des chiffres
  ci-dessus → signe que quelque chose dans le dimensionnement ou le
  rachat ne fonctionne pas comme prévu.

## Rappel de méthode

Même précédent que les bots #1/#2 : un résultat sur peu de données n'est
pas un résultat. Ici plus encore, parce que la faible taille des mises
rend chaque trade individuel presque invisible dans le bruit — il faudra
BEAUCOUP de trades avant que quoi que ce soit ici soit interprétable.

## Résultats observés

_(à compléter à partir de 20 trades du bot #3 EN v2 — les trades
accumulés sous la v1, si il y en a, ne doivent pas être mélangés à ce
comptage : la configuration a changé)_

| Date | Trades | Gagnants | Pire DD | Position max observée | Écart aux attentes |
|---|---|---|---|---|---|
| | | | | | |
