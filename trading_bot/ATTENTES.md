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

# Bot #3 — observatoire large (24 paires, jusqu'à 3 rachats/paire, 10 USDT max/position)

Troisième bot, avec une méthode de dimensionnement VOLONTAIREMENT
différente des bots #1 et #2 — voir DEPLOIEMENT.md, "Un troisième bot".
Au lieu de dimensionner par le risque (% du capital selon la distance du
stop), chaque position est plafonnée à un **montant fixe** (10 USDT),
capital de départ 1000 USDT virtuels.

## v3 (11/09/2026) — panier étendu à TOUTES les paires des 3 bots

Cette section **remplace** les versions précédentes (v1 : 10 paires,
jamais de rachat ; v2 : 18 paires distinctes des bots #1/#2, avec rachat
— voir l'historique git de ce fichier pour leurs chiffres exacts).
Changement de but assumé, demandé explicitement : ce bot n'essaie plus
d'éviter toute duplication d'exposition avec les bots #1/#2 — il devient
un **observatoire large**, couvrant tout ce que les 3 bots suivent, pour
apprendre comment le marché se comporte dans son ensemble avant d'affiner
plus tard des règles plus spécifiques par paire. Argent 100% virtuel :
dupliquer l'exposition entre bots n'a aucun coût réel.

- **Rachat** (inchangé depuis v2) : jusqu'à **3 positions simultanées**
  par paire (`max_positions_per_symbol: 3`), 30 USDT max sur une seule
  crypto.
- **Cooldown** (inchangé) : `reentry_cooldown_hours: 6`.
- **Panier étendu à 24 paires** : les 18 de la v2 (ADA, DOGE, AVAX, DOT,
  LTC, TRX, ATOM, BCH, ETC, XLM, ALGO, NEAR, FIL, UNI, ICP, ARB, OP, SUI)
  **+ les 6 des bots #1/#2** (BTC, ETH, SOL, BNB, XRP, LINK).
- **`max_concurrent_positions` inchangé à 25** : déjà la contrainte la
  plus stricte — le plein backtest confirme qu'elle est bien atteinte au
  moins une fois sur l'année (voir provenance ci-dessous), donc pas
  besoin de la relever pour l'instant.

## Provenance des chiffres

Walk-forward sur un an de données réelles OKX (les 24 paires ci-dessus,
en 1h), 9 fenêtres hors-échantillon de 30 jours, entraînement sur 90
jours glissants. Historique exporté via GitHub Actions run `34587972254`
le 11/09/2026, calcul local sur le commit `3f07ba1` (celui qui applique
le panier à 24 paires au bot #3).

Deux invariants vérifiés indépendamment sur les trades produits par CE
run (pas seulement testés sur des données synthétiques), sur le plein
backtest (les 24 paires, un an, hors walk-forward) : jamais plus de 3
positions simultanées sur une même paire, jamais un rachat à moins de 6h
de la dernière activité sur cette paire — 0 violation sur les deux ; le
plafond global de 25 positions simultanées est atteint au moins une fois
sur l'année (confirme qu'il est bien la contrainte active, pas un
plafond surdimensionné qui ne sert jamais).

Résultat agrégé : **−1,36%** composé sur les 9 fenêtres (contre −1,25%
pour la v2 à 18 paires, −0,54% pour la v1 à 10 paires/pas de rachat),
pendant que le buy & hold moyen de ces 24 paires faisait **−26,95%**.
**963 trades** (~107/mois — encore plus que les ~92/mois de la v2, panier
1,33x plus large), taux de gain 34,9% (proche de la v2), pire drawdown
**−1,63%** (contre −1,09% pour la v2 — plus de paires actives en même
temps, plus d'occasions d'atteindre le plafond de 25 positions au même
moment, donc un peu plus de capital en jeu au pire moment). Sorties :
65,0% stop-loss, 34,7% objectif, 0,3% signal.

## ⚠️ Ce que ce résultat NE prouve PAS

Comme pour les versions précédentes, le drawdown encore petit en valeur
absolue (−1,63%, contre −3,2% pour le bot #1 et −5,1% pour le bot #2)
reste **en grande partie un effet mécanique** du plafond à 10 USDT/
position — même avec le rachat et 24 paires actives, l'exposition
maximale théorique reste minime face au capital total (1000 USDT). Ne
jamais présenter "petit drawdown" comme une victoire de la stratégie sur
ce bot. La tendance se confirme version après version : plus le panier
s'élargit (10 → 18 → 24) et plus le rachat joue son rôle, plus le
rendement composé se dégrade légèrement (−0,54% → −1,25% → −1,36%) et le
pire drawdown se creuse (−0,53% → −1,09% → −1,63%) — élargir le panier
n'a PAS amélioré le résultat sur cette période, ça a surtout multiplié
le nombre de trades et l'exposition simultanée. Ce bot mesure le
comportement RELATIF de la stratégie sur un très large panier — utile
pour apprendre lesquelles des 24 paires génèrent le plus/moins de
signaux, pas pour juger si "plus de paires = mieux".

## Ce qu'on attend en réel

| Métrique | Attendu |
|---|---|
| Trades | ~107 par mois (nettement plus que les v1/v2 et que les bots #1/#2) |
| Trades gagnants | 34,9% |
| Sorties sur stop-loss | ~65% |
| Sorties sur objectif | ~35% |
| Part max d'une position | 10 USDT fixes, jamais plus — PAS une part du capital |
| Positions simultanées (toutes paires) | jusqu'à 25 |
| Positions simultanées (une même paire) | jusqu'à 3 (30 USDT max sur une crypto) |
| Cooldown de rachat | jamais moins de 6h entre deux activités sur la même paire |
| Pire drawdown | proche de 0% par construction, mais le plus marqué des 3 versions (voir avertissement) |
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
- **Un nombre de trades très inférieur à 107/mois** → les minimums
  d'ordre de l'exchange (`min_cost`/`min_amount`) rejettent probablement
  une bonne partie des signaux à 10 USDT sur certaines des paires. Si
  observé, le dire explicitement plutôt que de laisser croire à un
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

_(à compléter à partir de 20 trades du bot #3 EN v3 — les trades
accumulés sous les v1/v2, si il y en a, ne doivent pas être mélangés à ce
comptage : la configuration a changé)_

| Date | Trades | Gagnants | Pire DD | Position max observée | Écart aux attentes |
|---|---|---|---|---|---|
| | | | | | |
