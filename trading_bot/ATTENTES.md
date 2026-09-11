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

# Bot #3 — micro-paris diversifiés (10 paires, 10 USDT max/position)

Troisième bot, avec une méthode de dimensionnement VOLONTAIREMENT
différente des bots #1 et #2 — voir DEPLOIEMENT.md, "Un troisième bot".
Au lieu de dimensionner par le risque (% du capital selon la distance du
stop), chaque position est plafonnée à un **montant fixe** (10 USDT),
sur un panier de 10 paires, jusqu'à 10 positions simultanées, capital de
départ 1000 USDT virtuels. Au plus 100 USDT sur 1000 jamais investis en
même temps.

## Provenance des chiffres

Walk-forward sur un an de données réelles OKX (BTC, ETH, SOL, BNB, XRP,
LINK, ADA, DOGE, AVAX, DOT en 1h), 9 fenêtres hors-échantillon de 30
jours. Historique exporté via GitHub Actions run `34579866367` le
11/09/2026, calcul local sur le commit `e49023c` (celui qui introduit
`max_position_notional_usd`). Fenêtres calculées un jour après celles des
bots #1/#2 (11/09 contre 10/09) — comparaison directionnelle valable,
pas chiffre à chiffre.

Résultat agrégé : **−0,54%** composé sur les 9 fenêtres, **329 trades**
(~36,6/mois), taux de gain 34,3%, pire drawdown **−0,53%**.

## ⚠️ Ce que ce résultat NE prouve PAS

Le rendement proche de zéro et le drawdown minuscule (−0,53%, contre
−3,2% pour le bot #1 et −5,1% pour le bot #2) ne sont **pas** un signe
que cette approche est plus sûre ou plus intelligente. C'est un effet
**mécanique** : avec 10 USDT plafonnés par position sur 1000 USDT de
capital, la perte totale sur TOUTE la période de test n'a été que de
**5,40 USDT**. Le résultat est proche de zéro parce que presque rien n'a
jamais été réellement en jeu — pas parce que la stratégie a mieux
fonctionné sur ce panier. Ne jamais présenter "plus petit drawdown" comme
une victoire pour ce bot en particulier : c'est la conséquence directe et
attendue de la taille des mises, pas une découverte sur le marché.

Ce que ce bot mesure vraiment : le comportement RELATIF de la stratégie
sur 10 paires en parallèle (quelles paires génèrent le plus de signaux,
lesquelles gagnent/perdent le plus souvent) — utile pour apprendre, pas
pour juger si "ça marche mieux à 10 paires qu'à 3".

## Ce qu'on attend en réel

| Métrique | Attendu |
|---|---|
| Trades | ~37 par mois (bien plus que les bots #1/#2 — 10 paires, positions minuscules) |
| Trades gagnants | 34,3% |
| Sorties sur stop-loss | ~66% |
| Sorties sur objectif | ~34% |
| Part max d'une position | 10 USDT fixes, jamais plus — PAS une part du capital |
| Positions simultanées | jusqu'à 10 |
| Pire drawdown | proche de 0% par construction (voir avertissement ci-dessus) |
| Rendement | proche de 0%, dans les deux sens — l'ampleur en dollars compte plus que le % ici |

## Ce qui invaliderait la configuration (pas seulement la stratégie)

À évaluer à partir de 20 trades réels **de ce bot uniquement**
(`select count(*) from microbot_trades where side='sell';` — jamais
mélangé aux comptages des bots #1/#2) :

- **Une position au-dessus de 10 USDT observée** → bug du plafond fixe,
  pas un résultat de marché : c'est LE mécanisme central de ce bot, une
  violation ici est plus grave que pour les bots #1/#2.
- **Plus de 10 positions simultanées** → `max_concurrent_positions` non
  respecté, bug de portefeuille.
- **Un nombre de trades très inférieur à 37/mois** → les minimums d'ordre
  de l'exchange (`min_cost`/`min_amount`) rejettent probablement une
  bonne partie des signaux à 10 USDT — sur certaines paires, 10 USDT peut
  être sous le plancher réel de l'exchange. Si observé, le dire
  explicitement plutôt que de laisser croire à un filtre de marché plus
  strict que prévu.
- Un drawdown ou un rendement qui s'écarte nettement de "proche de zéro"
  → contredit directement l'effet mécanique attendu ci-dessus, signe que
  quelque chose dans le dimensionnement ne fonctionne pas comme prévu.

## Rappel de méthode

Même précédent que les bots #1/#2 : un résultat sur peu de données n'est
pas un résultat. Ici plus encore, parce que la faible taille des mises
rend chaque trade individuel presque invisible dans le bruit — il faudra
BEAUCOUP de trades avant que quoi que ce soit ici soit interprétable.

## Résultats observés

_(à compléter à partir de 20 trades du bot #3 — ne rien conclure avant)_

| Date | Trades | Gagnants | Pire DD | Position max observée | Écart aux attentes |
|---|---|---|---|---|---|
| | | | | | |
