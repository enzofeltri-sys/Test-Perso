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
