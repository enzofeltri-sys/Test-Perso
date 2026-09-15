# Pistes d'amélioration

Liste vivante des trucs intéressants repérés en cours de route, pas encore
traités (ou traités partiellement), à ajuster au fur et à mesure plutôt que
tout corriger d'un coup. Chaque entrée : quand ça a été repéré, ce qu'on a
observé, l'hypothèse, et une piste de correctif si on décide de s'y mettre.

## Modèle

### Le modèle Poisson n'a aucune notion de championnat
*Repéré le 2026-09-15.*

Après l'ajout de Liga/Bundesliga/Serie A, le backtest s'est effondré
(-11,34% → -88,8% ROI). Une partie de la cause était un vrai bug (voir
"Historique des correctifs" ci-dessous), mais même après ce correctif le
ROI reste négatif (-31,81%).

Vérifié en base (`footballbot_matches`) : les 5 championnats ont des
profils de buts nettement différents —

| Championnat | Buts/match | % victoire dom. |
|---|---|---|
| Bundesliga (D1) | 3.16 | 43.5% |
| Premier League (E0) | 2.86 | 43.9% |
| Ligue 1 (F1) | 2.75 | 43.0% |
| Serie A (I1) | 2.72 | 41.0% |
| Liga (SP1) | 2.57 | 45.0% |

`src/features.py` (FEATURE_COLUMNS) n'inclut aucune colonne "championnat" —
le modèle apprend une seule relation forme→buts, appliquée uniformément
aux 5 championnats, alors que leur baseline de buts diffère de ~23% entre
le plus et le moins offensif. Hypothèse : ça biaise systématiquement les
probabilités, surtout gênant pour les combinés (l'erreur de calibration
se compose entre les jambes).

**Piste** : ajouter le championnat comme feature catégorielle (one-hot)
dans `src/model.py` (`ColumnTransformer` : `OneHotEncoder` pour la ligue +
`StandardScaler` pour le reste), et propager dans
`features.build_features_for_match` (passer la ligue du match à venir).
Retester le backtest avant/après pour confirmer l'effet.

## Historique / statistiques

### Historique buts/résultats par équipe (pas seulement par championnat)
*Repéré le 2026-09-15, demandé par Enzo.*

On a aujourd'hui la moyenne de buts et les % de victoire dom/ext par
CHAMPIONNAT (calculable à la demande via SQL sur `footballbot_matches`),
mais rien par équipe, et rien d'affiché en permanence sur la web app.

**Piste** : soit garder ça en requête ponctuelle (SQL sur demande), soit
ajouter une page `/stats` sur `web_app.py` avec un classement par équipe
(buts marqués/encaissés, % victoire dom/ext, forme récente) — à discuter
si Enzo veut que ce soit permanent ou juste consultable au coup par coup.

## Blessures / fatigue (API-Football)

### Le suivi des blessures ne fonctionne pas
*Repéré le 2026-09-15.*

`footballbot_team_refs` n'a qu'UNE ligne (Leeds) après 2+ jours et des
dizaines d'équipes traitées, avec `injury_count` à `NULL`. Aucune mention
"blessé(s)" dans le journal.

Root cause pas encore confirmée : `_api_football_get()` avalait tout
échec silencieusement (corrigé le 2026-09-15, voir plus bas), mais le
premier cycle suivant le correctif n'a rejoué AUCUN appel API-Football
(les 3 seuls matchs de ce cycle-là ont été rejetés avant, pour cause
d'historique insuffisant — voir plus bas). Donc toujours pas de vraie
réponse : à revérifier au prochain cycle qui traite au moins un match
valide.

**Piste** : rien à coder de plus pour l'instant — attendre qu'un cycle
avec un vrai match déclenche `fetch_injury_count`, puis lire
`footballbot_errors` pour la raison exacte.

### La fatigue coupe d'Europe semble marcher, mais peut-être seulement via le fallback
*Repéré le 2026-09-15.*

Les messages "a joué en coupe d'Europe le ..." apparaissent bien dans le
journal, mais rien ne prouve que ça vient d'API-Football
(`fetch_recent_uefa_fixture`, essayé en premier) plutôt que du fallback
football-data.org (`played_in_europe_recently`). Si API-Football échoue
silencieusement pour la même raison que les blessures, cette fonctionnalité
tourne peut-être à 100% sur le second avis sans qu'on le sache.

**Piste** : une fois la vraie cause de l'échec blessures identifiée
(voir ci-dessus), vérifier si elle affecte aussi `fetch_recent_uefa_fixture`
(même fonction `_api_football_get` sous-jacente).

## Données manquantes

### 3 clubs espagnols absents de l'historique
*Repéré le 2026-09-14/15.*

Deportivo La Coruña, Real Racing Club de Santander et Málaga n'apparaissent
sous aucune orthographe dans `footballbot_matches` — pas un bug de mapping
(`team_names.py`), une vraie absence : ces clubs jouent actuellement en
division inférieure espagnole, non couverte par le flux SP1 (Liga, D1) de
football-data.co.uk.

**Piste** : si ça devient gênant (beaucoup de matchs ignorés), regarder si
football-data.co.uk fournit un flux SP2 (Segunda División) à ajouter en
config — sinon laisser ces matchs se faire ignorer comme prévu
("Historique insuffisant").

## Throttle / infra

### Règlement des paris retardé jusqu'à 24h après la fin du match
*Repéré le 2026-09-14/15, pas un bug — trade-off déclibéré.*

`SCORES_FETCH_INTERVAL_HOURS=24` (relevé de 12h lors de l'ajout des 3
championnats, pour rester sous le quota The Odds API à 5 ligues). Effet de
bord : un match qui se termine juste après un fetch peut attendre jusqu'à
24h avant d'être réglé.

**Piste** : si le quota le permet un jour (moins de ligues suivies, ou
upgrade de plan), redescendre l'intervalle. Pour l'instant, laissé tel
quel — le quota est plus contraignant que le confort d'affichage.

---

## Historique des correctifs déjà appliqués (pour mémoire, pas des pistes ouvertes)

- **2026-09-15** : `strategy.build_tickets` ne vérifiait `config.MAX_SANE_EV`
  que jambe par jambe, jamais sur l'EV composée du ticket combiné — des
  combinés à EV délirante (+2901%) passaient. Corrigé : le filtre
  s'applique maintenant aussi à l'EV du ticket final. Backtest : ROI
  -88,8% → -31,81% après correctif.
- **2026-09-15** : `_api_football_get` avalait tout échec silencieusement
  (aucune trace de la vraie raison). Ajout d'un log dédupliqué par
  (endpoint, raison) dans `footballbot_errors`.
