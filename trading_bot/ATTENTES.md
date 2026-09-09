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
