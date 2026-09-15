# Déployer le bot dans le cloud, gratuitement (Render + GitHub Actions + Supabase)

Ce bot ne tourne **jamais en local** : tout se passe dans le cloud, pour
que tu puisses le suivre depuis ton téléphone sans jamais lancer quoi que
ce soit sur ta machine. Voir le README principal pour la stratégie elle-même
(EV, Kelly, features) ; ce document ne couvre que l'hébergement.

## Comment ça tient ensemble

- **Render** (gratuit) héberge `web_app.py` comme un petit service web. Un
  service gratuit s'endort après 15 minutes sans requête, et met ~1 minute
  à se réveiller.
- **UptimeRobot** (gratuit) envoie une requête à `/tick` régulièrement, ce
  qui (a) empêche le service de s'endormir et (b) déclenche un cycle
  (règlement des paris + placement de nouveaux paris) à chaque fois. Le
  bot throttle lui-même ses appels à The Odds API (voir `src/config.py`),
  donc un ping toutes les 5 minutes ne consomme pas plus de crédits API
  qu'un ping toutes les heures.
- **Supabase** stocke tout ce qui doit survivre à un redémarrage du service
  gratuit : bankroll, historique des matchs, modèle entraîné, paris,
  journal.
- **GitHub Actions** ré-entraîne le modèle chaque semaine sur l'historique
  à jour (`retrain.py`) et le publie dans Supabase — un Cron Job Render
  exige un plan payant, GitHub Actions est gratuit pour ça.

Aucun de ces services ne coûte quoi que ce soit à ce niveau d'usage. Aucun
pari réel n'est jamais placé.

## 1. Les tables Supabase

Tu peux réutiliser un projet Supabase existant (ex: celui du bot de
trading, `trading_bot/`) — ces tables ont un préfixe différent
(`footballbot_`) et ne rentrent pas en conflit. Dans le **SQL Editor** du
projet :

```sql
create table if not exists public.footballbot_state (
  id text primary key default 'default',
  bankroll numeric not null,
  last_odds_fetch_at timestamptz,
  last_scores_fetch_at timestamptz,
  odds_api_remaining int,
  api_football_remaining int,
  updated_at timestamptz not null default now()
);

create table if not exists public.footballbot_matches (
  id bigserial primary key,
  league text not null,
  season text not null,
  home_team text not null,
  away_team text not null,
  date date not null,
  fthg int,
  ftag int,
  ftr text,
  odds_h numeric,
  odds_d numeric,
  odds_a numeric,
  unique (league, season, home_team, away_team, date)
);

create table if not exists public.footballbot_model (
  id text primary key default 'default',
  model_b64 text not null,
  feature_columns jsonb,
  train_matches int,
  test_matches int,
  backtest_metrics jsonb,
  trained_at timestamptz not null default now()
);

-- Un ticket = un pari seul (1 jambe) ou combiné (jusqu'à 3 jambes, voir
-- src/strategy.build_tickets). Le détail par match vit dans bet_legs.
create table if not exists public.footballbot_bets (
  id bigserial primary key,
  ts timestamptz not null default now(),
  num_legs int not null,
  prob numeric,
  odds numeric,
  ev numeric,
  stake numeric not null,
  status text not null default 'pending' check (status in ('pending', 'won', 'lost')),
  pnl numeric,
  bankroll_after numeric,
  settled_at timestamptz
);

create table if not exists public.footballbot_bet_legs (
  id bigserial primary key,
  bet_id bigint not null references public.footballbot_bets(id) on delete cascade,
  league text not null,
  home_team text not null,
  away_team text not null,
  commence_time timestamptz,
  market text not null,
  selection text not null,
  prob numeric,
  odds numeric,
  result text not null default 'pending' check (result in ('pending', 'won', 'lost')),
  settled_at timestamptz
);

create index if not exists footballbot_bet_legs_bet_id_idx on public.footballbot_bet_legs(bet_id);
create index if not exists footballbot_bet_legs_pending_idx on public.footballbot_bet_legs(result) where result = 'pending';

create table if not exists public.footballbot_journal (
  id bigserial primary key,
  ts timestamptz not null default now(),
  author text not null check (author in ('bot', 'manager')),
  message text not null,
  data jsonb
);

-- Cache pour src/external_data.py (blessures/coupe d'Europe/logo, affichage
-- uniquement — voir README) : id API-Football, nombre de blessés et URL du
-- logo (TheSportsDB) par équipe, pour éviter de re-consommer le quota
-- gratuit à chaque cycle.
create table if not exists public.footballbot_team_refs (
  team_name text primary key,
  api_football_id int,
  injury_count int,
  injury_checked_at timestamptz,
  logo_url text,
  updated_at timestamptz not null default now()
);

create table if not exists public.footballbot_errors (
  id bigserial primary key,
  ts timestamptz not null default now(),
  message text not null
);

alter table public.footballbot_state enable row level security;
alter table public.footballbot_matches enable row level security;
alter table public.footballbot_model enable row level security;
alter table public.footballbot_bets enable row level security;
alter table public.footballbot_bet_legs enable row level security;
alter table public.footballbot_team_refs enable row level security;
alter table public.footballbot_journal enable row level security;
alter table public.footballbot_errors enable row level security;

create policy "footballbot_state_all" on public.footballbot_state
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_matches_all" on public.footballbot_matches
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_model_all" on public.footballbot_model
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_bets_all" on public.footballbot_bets
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_bet_legs_all" on public.footballbot_bet_legs
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_team_refs_all" on public.footballbot_team_refs
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_journal_all" on public.footballbot_journal
  for all to anon, authenticated using (true) with check (true);
create policy "footballbot_errors_all" on public.footballbot_errors
  for all to anon, authenticated using (true) with check (true);
```

⚠️ Si tu avais déjà créé les anciennes tables (schéma avant les paris
combinés), ce bloc ne les met pas à jour automatiquement (`create table
if not exists` ne touche pas une table existante). Le plus simple : dans
le SQL Editor, `drop table if exists public.footballbot_bets cascade;`
puis relance ce bloc — aucune perte de données si tu n'avais pas encore
de paris (`select count(*) from footballbot_bets;` pour vérifier avant).

Récupère ensuite, dans **Project Settings → API** :
- l'**URL du projet** (`https://xxxx.supabase.co`)
- la clé **`anon` (legacy)** — jamais la `service_role`.

## 2. Premier entraînement (GitHub Actions)

1. Pousse ce dossier dans le repo GitHub (déjà fait si tu lis ce fichier
   depuis le repo).
2. Sur GitHub, dans le repo : **Settings → Secrets and variables → Actions
   → New repository secret**. Ajoute `SUPABASE_URL` et `SUPABASE_KEY`.
3. Onglet **Actions** → « Ré-entraînement hebdomadaire du bot de paris
   football » → **Run workflow** (déclenchement manuel). Ça télécharge
   l'historique football-data.co.uk, entraîne le modèle, backteste, et
   publie le modèle dans `footballbot_model`.
4. Vérifie dans les logs du run : nombre de matchs, métriques de backtest
   (ROI, win rate...). Sans ce premier run, `web_app.py` ne pourra placer
   aucun pari (il attend qu'un modèle existe).
5. Le workflow se relance ensuite automatiquement chaque lundi à 4h UTC.

## 3. Déployer sur Render

1. Sur [render.com](https://render.com) : **New → Blueprint**, connecte le
   repo, pointe vers `fun-betting-bot-football/render.yaml`. Si le
   Blueprint ne détecte pas le sous-dossier automatiquement, crée le
   service manuellement (**New → Web Service**) avec **Root Directory** =
   `fun-betting-bot-football`, build command `pip install -r
   requirements.txt`, start command `gunicorn web_app:app --bind
   0.0.0.0:$PORT --timeout 60`.
2. Renseigne les variables d'environnement (**Environment**) :
   - `SUPABASE_URL`, `SUPABASE_KEY` (mêmes valeurs qu'à l'étape 1)
   - `ODDS_API_KEY` — ta clé The Odds API (optionnel, mais sans elle le bot
     reste en mode "matchs d'exemple" et ne peut jamais régler ses paris)
   - `API_FOOTBALL_KEY`, `FOOTBALL_DATA_ORG_KEY` — optionnelles, affichage
     uniquement dans le journal (blessures, coupe d'Europe — voir README)
   - `TABLE_PREFIX` = `footballbot` (déjà dans render.yaml)
3. Déploie. L'URL ressemble à
   `https://football-betting-bot-xxxx.onrender.com`. Vérifie que `/`
   affiche la page de statut, et que `/tick` répond un JSON
   (`{"bets_placed": ..., "bets_settled": ..., "bankroll": ..., "errors": []}`).

## 4. Configurer UptimeRobot

1. Sur [uptimerobot.com](https://uptimerobot.com) : **Add New Monitor**.
2. Type **HTTP(s)**, URL `https://football-betting-bot-xxxx.onrender.com/tick`
   (bien `/tick`, pas `/`).
3. Intervalle : **5 minutes** (le minimum gratuit). Le bot throttle
   lui-même ses appels réseau (voir `ODDS_FETCH_INTERVAL_HOURS` /
   `SCORES_FETCH_INTERVAL_HOURS` dans `src/config.py`), donc ce ping
   fréquent ne consomme pas le quota gratuit de The Odds API (500
   crédits/mois — le bot utilise environ 120/mois avec 2 ligues).

## 5. Vérifier que ça tourne

- **Page de statut** : `https://football-betting-bot-xxxx.onrender.com/`
  — bankroll, derniers paris, dernier backtest, journal. Consultable
  depuis le navigateur de ton téléphone.
- **Table Editor Supabase** : `footballbot_bets` (historique complet),
  `footballbot_journal` (le bot y explique chaque décision), `footballbot_errors`.
- **Logs Render** : dashboard Render → onglet **Logs**.

## 6. Alertes optionnelles

Même mécanisme que `trading_bot/alerts.py` (fichier copié tel quel dans ce
projet). Ajoute `ALERT_WEBHOOK_URL` (Discord/Slack/Telegram) sur Render, et
`ALERT_TEST_TOKEN` pour tester via `/alert-test?token=...`. Sans ces
variables, le module est inerte et le bot tourne normalement.

## Limites à connaître

- **Correspondance des noms d'équipes** (The Odds API vs football-data.co.uk)
  est construite à la main dans `src/team_names.py`, avec un rapprochement
  flou en repli — elle n'a pas pu être vérifiée contre les vraies API
  depuis l'environnement où ce projet a été écrit (réseau restreint). Si le
  journal montre des entrées `unmatched_team` répétées pour une équipe,
  complète la table `ODDS_API_TO_FOOTBALL_DATA`.
- **Quota The Odds API** (free tier, ~500 crédits/mois) : throttlé à ~1
  appel/jour/ligue pour les cotes et 2/jour/ligue pour les scores. Si tu
  ajoutes des ligues, réduis la fréquence dans `src/config.py` en
  conséquence.
- **Règlement des paris** dépend entièrement de The Odds API (`/scores`) :
  sans clé API, les paris restent `pending` indéfiniment.
- Le plan gratuit Render offre 750h d'instance/mois et par compte — un
  mois complet tient dedans SI ce service est seul à tourner sur ce compte.
