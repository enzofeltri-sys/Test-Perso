# Déployer le bot en continu, gratuitement (Render + UptimeRobot + Supabase)

Ce guide déploie `web_app.py` — la version du bot pensée pour tourner
sans dépendre de ton ordinateur ou de ton téléphone, gratuitement. Voir
le README principal pour comprendre la stratégie elle-même ; ce document
ne couvre que l'hébergement.

## Comment ça tient ensemble

- **Render** (gratuit) héberge le bot comme un petit service web. Un
  service gratuit s'endort après 15 minutes sans requête, et met ~1
  minute à se réveiller.
- **UptimeRobot** (gratuit) envoie une requête à `/tick` toutes les 5
  minutes, ce qui (a) empêche le service de s'endormir et (b) déclenche
  un cycle complet de vérification du marché à chaque fois.
- **Supabase** stocke l'état du bot (cash, positions ouvertes,
  coupe-circuits) et l'historique des trades, parce que le service peut
  redémarrer à tout moment sur un hébergement gratuit — rien ne peut
  rester seulement "en mémoire" comme dans la version `paper_trader.py`
  d'origine.

Aucun de ces trois services ne coûte quoi que ce soit à ce niveau
d'usage. Aucun ordre réel n'est jamais envoyé, ici comme ailleurs dans
ce projet.

## 1. Les tables Supabase

Si tu utilises le projet Supabase déjà connecté à cette conversation,
les tables `tradingbot_state`, `tradingbot_trades` et `tradingbot_errors`
existent déjà — rien à faire. Si tu déploies depuis un autre projet
Supabase, crée-les avec ce SQL (`SQL Editor` dans le dashboard Supabase) :

```sql
create table if not exists public.tradingbot_state (
  id text primary key default 'default',
  cash numeric not null,
  positions jsonb not null default '{}'::jsonb,
  daily_current_day date,
  daily_equity_at_day_start numeric,
  daily_tripped_today boolean not null default false,
  total_dd_peak_equity numeric,
  total_dd_tripped boolean not null default false,
  updated_at timestamptz not null default now()
);

create table if not exists public.tradingbot_trades (
  id bigserial primary key,
  ts timestamptz not null default now(),
  symbol text not null,
  side text not null,
  price numeric not null,
  qty numeric not null,
  reason text not null,
  cash_after numeric not null,
  equity_after numeric not null
);

create table if not exists public.tradingbot_errors (
  id bigserial primary key,
  ts timestamptz not null default now(),
  message text not null
);

alter table public.tradingbot_state enable row level security;
alter table public.tradingbot_trades enable row level security;
alter table public.tradingbot_errors enable row level security;

create policy "tradingbot_state_all" on public.tradingbot_state
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_trades_all" on public.tradingbot_trades
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_errors_all" on public.tradingbot_errors
  for all to anon, authenticated using (true) with check (true);
```

Récupère ensuite, dans **Project Settings → API** :
- l'**URL du projet** (`https://xxxx.supabase.co`)
- la clé **`anon` (legacy)** — pas la `service_role`, jamais celle-là ici.

## 2. Déployer sur Render

1. Pousse ce dossier (`trading_bot/`) dans un repo GitHub.
2. Sur [render.com](https://render.com) : **New → Blueprint**, connecte
   le repo. Render détecte `render.yaml` automatiquement et propose de
   créer le service `trading-bot-paper` (plan Free).
3. Avant le premier déploiement (ou juste après, dans **Environment**),
   renseigne les deux variables : `SUPABASE_URL` et `SUPABASE_KEY` (la
   clé anon récupérée à l'étape 1).
4. Déploie. Une fois en ligne, l'URL ressemble à
   `https://trading-bot-paper-xxxx.onrender.com`. Vérifie que
   `https://.../` répond "OK" dans un navigateur, puis que
   `https://.../tick` répond un petit JSON (premier cycle : normal que
   `trades_this_tick` soit vide la plupart du temps).

## 3. Configurer UptimeRobot

1. Sur [uptimerobot.com](https://uptimerobot.com) : **Add New Monitor**.
2. Type : **HTTP(s)**. URL : `https://trading-bot-paper-xxxx.onrender.com/tick`
   (bien `/tick`, pas juste `/` — sinon aucun cycle de trading n'est
   jamais déclenché, seul le "réveil" du service se produit).
3. Intervalle : **5 minutes** (le minimum du plan gratuit — largement
   suffisant pour une stratégie sur bougies 1h).
4. Sauvegarde. UptimeRobot va maintenant pinguer le bot en continu,
   24h/24, gratuitement.

## 4. Vérifier que ça tourne

- **Historique des trades** : dans Supabase, `Table Editor →
  tradingbot_trades` — visible depuis un navigateur mobile, pas besoin
  d'être devant un ordinateur.
- **État courant** (cash, positions ouvertes, coupe-circuits) :
  `tradingbot_state`.
- **Erreurs éventuelles** : `tradingbot_errors` (un souci de récupération
  de données sur une paire, par exemple, n'interrompt jamais les autres
  paires ni ne fait planter le cycle).
- **Logs Render** : dashboard Render → onglet **Logs** du service,
  consultable aussi depuis un navigateur mobile.

## Limites à connaître

- Le plan gratuit Render offre 750 heures d'instance par mois et par
  compte — un mois complet (744h max) tient dedans SI ce service est le
  seul à tourner sur ce compte Render.
- Un projet Supabase gratuit se met en pause après une semaine
  d'inactivité — mais comme le bot y écrit à chaque cycle (toutes les 5
  minutes), il ne sera jamais inactif tant qu'UptimeRobot ping le bot.
- Ce montage (ping externe pour empêcher la veille) n'est pas un usage
  "officiellement documenté" par Render — juste toléré en pratique pour
  ce genre de projet perso. Rien de garanti à très long terme ; si
  Render change son comportement un jour, la solution de repli reste un
  petit VPS à 5-8€/mois avec l'architecture `paper_trader.py` d'origine.
