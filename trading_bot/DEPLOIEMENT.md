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

Un quatrième composant optionnel, `recalibrate.py` (section 3ter),
revalide la stratégie une fois par mois sur l'historique réel et ajuste
ses paramètres si la validation est robuste — un Cron Job Render séparé,
pas nécessairement gratuit contrairement aux trois premiers (voir cette
section pour le détail du coût).

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

-- optionnelle : réglages ajustables à la volée sans redéployer, voir
-- section "Ajuster le bot à la volée" plus bas. Le bot fonctionne très
-- bien sans cette table (load_config_overrides() se rabat sur
-- config.yaml si elle n'existe pas).
create table if not exists public.tradingbot_config (
  id text primary key default 'default',
  risk_per_trade_pct numeric,
  max_daily_loss_pct numeric,
  max_total_drawdown_pct numeric,
  max_correlation_for_new_position numeric,
  correlation_lookback int,
  momentum_lookback int,
  max_concurrent_positions int,
  active_symbols jsonb,
  strategy_overrides jsonb,
  note text,
  updated_by text,
  updated_at timestamptz not null default now()
);
insert into public.tradingbot_config (id) values ('default')
  on conflict (id) do nothing;

alter table public.tradingbot_state enable row level security;
alter table public.tradingbot_trades enable row level security;
alter table public.tradingbot_errors enable row level security;
alter table public.tradingbot_config enable row level security;

create policy "tradingbot_state_all" on public.tradingbot_state
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_trades_all" on public.tradingbot_trades
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_errors_all" on public.tradingbot_errors
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_config_all" on public.tradingbot_config
  for all to anon, authenticated using (true) with check (true);
```

Récupère ensuite, dans **Project Settings → API** :
- l'**URL du projet** (`https://xxxx.supabase.co`)
- la clé **`anon` (legacy)** — pas la `service_role`, jamais celle-là ici.

## 2. Déployer sur Render

⚠️ **Historique — blocage géographique Binance** : `api.binance.com`
renvoie une erreur 451 "restricted location" pour les IP US, et Render
héberge par défaut en Oregon (US) sur le plan gratuit. Le bot tourne
maintenant sur **KuCoin** plutôt que Binance (`exchange.id: kucoin` dans
`config.yaml`), qui ne bloque pas ce type d'IP — mais `render.yaml` cible
quand même `region: frankfurt` (bon choix par défaut de toute façon,
latence plus faible vers Europe). Si jamais KuCoin bloquait à son tour un
jour, la solution de repli est de changer `exchange.id` dans
`config.yaml` (`kraken`, `bybit`, `okx`...) — vérifie juste que les
symboles du portefeuille existent bien sur le nouvel exchange.

**Render ne permet pas de changer la région d'un service déjà créé** —
pour changer de région il faut supprimer le service et en recréer un
(le Blueprint s'en charge à la création).

1. Pousse ce dossier (`trading_bot/`) dans un repo GitHub.
2. Sur [render.com](https://render.com) : **New → Blueprint**, connecte
   le repo. Render détecte `render.yaml` automatiquement et propose de
   créer le service `trading-bot-paper` (plan Free, région Frankfurt).
3. Avant le premier déploiement (ou juste après, dans **Environment**),
   renseigne les deux variables : `SUPABASE_URL` et `SUPABASE_KEY` (la
   clé anon récupérée à l'étape 1).
4. Déploie. Une fois en ligne, l'URL ressemble à
   `https://trading-bot-paper-xxxx.onrender.com`. Vérifie que
   `https://.../` affiche la page de statut dans un navigateur, puis que
   `https://.../tick` répond un petit JSON avec des paires bien
   récupérées (`errors: []`).

## 3. Configurer UptimeRobot

1. Sur [uptimerobot.com](https://uptimerobot.com) : **Add New Monitor**.
2. Type : **HTTP(s)**. URL : `https://trading-bot-paper-xxxx.onrender.com/tick`
   (bien `/tick`, pas juste `/` — sinon aucun cycle de trading n'est
   jamais déclenché, seul le "réveil" du service se produit).
3. Intervalle : **5 minutes** (le minimum du plan gratuit — largement
   suffisant pour une stratégie sur bougies 1h).
4. Sauvegarde. UptimeRobot va maintenant pinguer le bot en continu,
   24h/24, gratuitement.

## 3bis. Ajuster le bot à la volée (sans redéployer) — `tradingbot_config`

La table `tradingbot_config` (une seule ligne, `id='default'`, voir SQL
plus haut) permet de surcharger certains réglages de `config.yaml`
**entre deux cycles**, sans toucher au code ni redéployer sur Render —
pratique pour réagir vite si quelque chose mérite d'être resserré.
Une valeur laissée à `null` (ou la colonne absente de la mise à jour)
veut dire "garde celle de `config.yaml`".

Colonnes disponibles : `risk_per_trade_pct`, `max_daily_loss_pct`,
`max_total_drawdown_pct`, `max_correlation_for_new_position`,
`correlation_lookback`, `momentum_lookback`, `max_concurrent_positions`,
et `active_symbols` (ex : `["BTC/USDT"]` pour désactiver ETH/SOL en
nouvelle entrée — les positions déjà ouvertes sur une paire désactivée
continuent d'être surveillées et fermées normalement, seules les
NOUVELLES entrées sur cette paire sont bloquées).

Exemple, dans le SQL Editor de Supabase, pour ne garder que BTC actif et
réduire le risque par trade à 0,5% :

```sql
update public.tradingbot_config
set active_symbols = '["BTC/USDT"]'::jsonb,
    risk_per_trade_pct = 0.005,
    note = 'resserré temporairement',
    updated_by = 'enzo',
    updated_at = now()
where id = 'default';
```

Pour tout remettre aux valeurs de `config.yaml`, remets chaque colonne à
`null` (`active_symbols` à `null` réactive toutes les paires).

Chaque réponse de `/tick` indique `"config_overrides_active"` et
`"active_symbols"` — de quoi vérifier d'un coup d'œil qu'une surcharge
est bien prise en compte. Si la table n'existe pas encore (déploiement
sans cette étape optionnelle), le bot tourne normalement avec les
valeurs de `config.yaml` — aucune erreur.

⚠️ **Migration si `tradingbot_config` existe déjà** (déployée avant
l'ajout de `recalibrate.py`, voir section 3ter) : la colonne
`strategy_overrides` n'y est pas encore. Ajoute-la une fois, dans le SQL
Editor de Supabase :

```sql
alter table public.tradingbot_config
  add column if not exists strategy_overrides jsonb;
```

## 3ter. Recalibrage mensuel automatique — `recalibrate.py`

Un second script, indépendant de `web_app.py`, revalide périodiquement
la stratégie sur l'historique réel et ajuste ses PARAMÈTRES (pas le
risque) si — et seulement si — la validation récente est robuste. Voir
la docstring de `recalibrate.py` pour le détail du critère de robustesse
et tout ce qu'il ne touche jamais (risque, `active_symbols`, aucun
ordre).

Contrairement à `web_app.py` (service web réveillé par UptimeRobot), ce
script tourne comme **Cron Job Render séparé** — un processus qui
démarre, s'exécute jusqu'au bout, puis s'arrête, sur un planning
(`render.yaml`, service `trading-bot-recalibrate`, une fois par mois).

- **Il écrit dans `tradingbot_config.strategy_overrides`** (colonne
  distincte des réglages de risque existants — jamais écrasés) —
  `web_app.py` les applique au tick suivant, avant de construire la
  stratégie.
- **Il n'écrit RIEN** si les fenêtres hors-échantillon récentes ne sont
  pas robustes : le bot continue avec les derniers paramètres en place.
- Se lance aussi à la main, pour vérifier avant de laisser le cron
  tourner seul :
  ```bash
  python recalibrate.py --dry-run   # calcule et affiche, n'écrit jamais
  python recalibrate.py             # calcule et écrit si robuste
  ```

Déploiement (comme le service web, mais **New → Cron Job**, pas
**Blueprint**, pour les mêmes raisons de Root Directory) :

1. Sur [render.com](https://render.com) : **New → Cron Job**, connecte
   le même repo GitHub.
2. **Root Directory** : `trading_bot`. **Runtime** : Python 3. **Build
   Command** : `pip install -r requirements.txt`. **Start Command** :
   `python recalibrate.py`.
3. **Schedule** : `0 3 1 * *` (le 1er de chaque mois à 3h UTC).
4. Renseigne les mêmes variables d'environnement que le service web :
   `SUPABASE_URL`, `SUPABASE_KEY`, `PYTHON_VERSION` (`3.11.9`).
5. Un Cron Job Render n'est **pas nécessairement gratuit** comme le
   service web (les plans disponibles dépendent de ton compte) — vérifie
   le coût affiché avant de confirmer la création. Une exécution par mois
   reste minime en tout état de cause (quelques minutes de calcul).

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
