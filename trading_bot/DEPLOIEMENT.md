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
ses paramètres si la validation est robuste — via un workflow GitHub
Actions programmé (gratuit), pas Render (son Cron Job exige un plan
payant, contrairement au service web).

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
  max_position_pct_of_equity numeric,
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

-- optionnelle : journal partagé bot <-> manager (humain ou Claude Cowork),
-- voir section "Le bot explique ses décisions" plus bas. Le bot fonctionne
-- très bien sans cette table (get_recent_journal()/log_journal_entry()
-- sont best-effort).
create table if not exists public.tradingbot_journal (
  id bigserial primary key,
  ts timestamptz not null default now(),
  author text not null check (author in ('bot', 'manager')),
  message text not null,
  data jsonb
);

alter table public.tradingbot_state enable row level security;
alter table public.tradingbot_trades enable row level security;
alter table public.tradingbot_errors enable row level security;
alter table public.tradingbot_config enable row level security;
alter table public.tradingbot_journal enable row level security;

create policy "tradingbot_state_all" on public.tradingbot_state
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_trades_all" on public.tradingbot_trades
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_errors_all" on public.tradingbot_errors
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_config_all" on public.tradingbot_config
  for all to anon, authenticated using (true) with check (true);
create policy "tradingbot_journal_all" on public.tradingbot_journal
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

Colonnes disponibles : `risk_per_trade_pct`, `max_position_pct_of_equity`,
`max_daily_loss_pct`,
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

⚠️ **Migration pour `max_position_pct_of_equity`** (plafond de
concentration, voir README section "Gestion du risque") — même principe,
la colonne n'existe pas dans les déploiements antérieurs :

```sql
alter table public.tradingbot_config
  add column if not exists max_position_pct_of_equity numeric;
```

Cette colonne est **optionnelle** : tant qu'elle n'existe pas, le bot
applique simplement la valeur de `config.yaml` (0.25) — le plafond est
actif dans tous les cas, seule la possibilité de l'ajuster à la volée
sans redéployer dépend de cette migration.

⚠️ **Migration pour `tradingbot_journal`** (voir section "Le bot explique
ses décisions" plus bas) — table entièrement nouvelle, à créer une fois
si elle n'existe pas déjà :

```sql
create table if not exists public.tradingbot_journal (
  id bigserial primary key,
  ts timestamptz not null default now(),
  author text not null check (author in ('bot', 'manager')),
  message text not null,
  data jsonb
);
alter table public.tradingbot_journal enable row level security;
create policy "tradingbot_journal_all" on public.tradingbot_journal
  for all to anon, authenticated using (true) with check (true);
```

## 3ter. Recalibrage mensuel automatique — `recalibrate.py`

Un second script, indépendant de `web_app.py`, revalide périodiquement
la stratégie sur l'historique réel et ajuste ses PARAMÈTRES (pas le
risque) si — et seulement si — la validation récente est robuste. Voir
la docstring de `recalibrate.py` pour le détail du critère de robustesse
et tout ce qu'il ne touche jamais (risque, `active_symbols`, aucun
ordre).

Contrairement à `web_app.py` (service web réveillé par UptimeRobot), ce
script tourne comme **workflow GitHub Actions programmé** — pas sur
Render : un Cron Job Render exige un plan payant (vérifié en essayant
d'en créer un : aucune option gratuite, contrairement au service web),
alors que GitHub Actions est gratuit pour un déclenchement mensuel de
quelques minutes (généreux quota gratuit, y compris sur un repo privé).

- **Il écrit dans `tradingbot_config.strategy_overrides`** (colonne
  distincte des réglages de risque existants — jamais écrasés) —
  `web_app.py` les applique au tick suivant, avant de construire la
  stratégie.
- **Il n'écrit RIEN** si les fenêtres hors-échantillon récentes ne sont
  pas robustes : le bot continue avec les derniers paramètres en place.
- Se lance aussi à la main, en local, pour vérifier avant de laisser le
  planning tourner seul :
  ```bash
  python recalibrate.py --dry-run   # calcule et affiche, n'écrit jamais
  python recalibrate.py             # calcule et écrit si robuste
  ```

Le workflow (`.github/workflows/recalibrate.yml`) est déjà dans le
repo — il ne reste que les secrets à configurer :

1. Sur GitHub, dans le repo : **Settings → Secrets and variables →
   Actions → New repository secret**.
2. Ajoute `SUPABASE_URL` et `SUPABASE_KEY` (les mêmes valeurs que celles
   renseignées dans Render pour le service web).
3. Le workflow se déclenche automatiquement le 1er de chaque mois à 3h
   UTC. Pour tester sans attendre : onglet **Actions** du repo →
   « Recalibrage mensuel du bot de trading » → **Run workflow**
   (déclenchement manuel, `workflow_dispatch`).
4. Résultat visible dans les logs du run (Actions → le run en question)
   et, si un recalibrage a été appliqué, dans
   `tradingbot_config.strategy_overrides` sur Supabase.

## 3quater. Le bot explique ses décisions — `tradingbot_journal`

Deux canaux distincts, à ne pas confondre :

- **`tradingbot_config`** = le PILOTAGE réel du bot. Colonnes typées
  (risque, `active_symbols`, `strategy_overrides`) — c'est le seul canal
  qui change effectivement le comportement du bot. Voir section 3bis.
- **`tradingbot_journal`** = la VISIBILITÉ/discussion. Texte libre, sans
  aucun pouvoir sur le bot — juste un journal partagé, lisible depuis
  Supabase (mobile inclus) ou sur la page de statut (`/`, section
  « Journal »).

Le bot y écrit (`author='bot'`), en langage clair :

- **`recalibrate.py`, à chaque exécution** : combien de fenêtres
  hors-échantillon trouvées, le rendement composé récent, si c'était
  robuste, et s'il a recalibré ou non (avec les nouveaux paramètres le
  cas échéant). Même donnée que les logs GitHub Actions, mais persistée.
- **`web_app.py`, en direct, sur les événements notables seulement**
  (pas à chaque tick) :
  - **acquittement d'un changement de `tradingbot_config`** — quand le
    manager (ou `recalibrate.py`) modifie les réglages, le bot le dit une
    fois (« modifiés par X, note : …, appliqués à partir de ce cycle »)
    au lieu d'appliquer en silence. Détecté sur le contenu, pas sur
    `updated_at` — un manager qui oublie de mettre `updated_at` à jour
    est quand même acquitté ;
  - **chaque trade**, avec sa raison (signal de tendance ou de
    retournement à l'achat ; stop, objectif ou signal de sortie à la
    vente, avec le résultat en % avant frais) ;
  - **un coupe-circuit qui se déclenche** (journalier ou drawdown
    total), une fois à la transition.

`data` (jsonb) porte un champ `event` (`config_change`, `trade`,
`circuit_breaker`, ou rien pour les entrées de `recalibrate.py`) pour
filtrer facilement.

Le **manager** — toi, ou Claude Cowork avec les mêmes accès Supabase —
peut y répondre en écrivant ses propres entrées (`author='manager'`),
par exemple pour valider une décision, en expliquer une autre, ou
annoncer un changement à venir :

```sql
insert into public.tradingbot_journal (author, message)
values ('manager', 'Vu le recalibrage de ce mois — je laisse tel quel, RAS.');
```

Pour effectivement CADRER le bot (pas juste commenter), c'est
`tradingbot_config` qu'il faut modifier (section 3bis) — le journal ne
fait qu'expliquer et discuter, il ne pilote rien.

## 3quinquies. Être prévenu quand ça va mal — alertes

UptimeRobot ne sait qu'une chose : « le service répond ». Or `/tick`
répond volontairement `200` même en cas d'erreur (voir plus bas), donc un
coupe-circuit déclenché à 3h du matin ou un exchange injoignable pendant
six heures passeraient inaperçus jusqu'à ta prochaine visite de la page
de statut. C'est ce que ces alertes couvrent.

**Étape unique** : ajoute la variable d'environnement `ALERT_WEBHOOK_URL`
sur Render (Environment → Add Environment Variable), puis redéploie.
Sans elle, le module est inerte et le bot tourne exactement comme avant.

Selon le service que tu préfères :

| Service  | Ce que tu colles dans `ALERT_WEBHOOK_URL`            | En plus |
|----------|------------------------------------------------------|---------|
| Discord  | Paramètres du salon → Intégrations → Webhooks → Nouveau webhook → Copier l'URL | — |
| Slack    | api.slack.com/messaging/webhooks → URL du webhook entrant | — |
| Telegram | `https://api.telegram.org/bot<TOKEN>/sendMessage` (TOKEN donné par @BotFather) | `ALERT_CHAT_ID` = ton identifiant de discussion |

Le corps envoyé contient à la fois `content` (lu par Discord) et `text`
(lu par Slack) : chacun ignore la clé de l'autre, donc la même URL marche
sans réglage supplémentaire.

**Ce qui déclenche une alerte** — uniquement ce qui demande un œil humain :

- 🛑 **Coupe-circuit de drawdown total** — le bot n'ouvrira plus aucune
  position tant que tu n'as pas revu la stratégie ; il ne se réarme pas
  tout seul, par conception.
- 🛑 **Cycle de trading planté** — une exception non gérée dans `/tick`.
- ⚠️ **Coupe-circuit journalier** — plus d'entrées aujourd'hui, remise à
  zéro automatique demain.
- ⚠️ **Données inaccessibles** — une ou plusieurs paires impossibles à
  récupérer ; le bot continue avec celles qui répondent.
- ⚠️ **Recalibrage mensuel en échec** — sans ça, un job qui ne tourne
  qu'une fois par mois échoue en silence et tu le découvres le mois
  suivant. (Pour l'avoir aussi sur GitHub Actions, ajoute le même secret
  `ALERT_WEBHOOK_URL` au dépôt et passe-le au job, comme `SUPABASE_KEY`.)

**Ce qui n'en déclenche pas** : les trades ordinaires. Ils partent dans
`tradingbot_journal` et sur la page de statut. Une notification par trade
deviendrait un bruit qu'on apprend à ignorer — et le jour où une alerte
compte vraiment, on ne la lit plus.

**Anti-spam** : les coupe-circuits n'alertent qu'à la *transition* (pas à
chaque cycle tant qu'ils restent déclenchés), et les erreurs qu'une fois
par heure (`ERROR_ALERT_COOLDOWN_MINUTES` dans `web_app.py`). En cas de
doute — table d'erreurs illisible, horodatage inexploitable — le bot se
tait plutôt que de risquer une alerte toutes les cinq minutes.

**Garantie** : une alerte ne peut jamais faire échouer un cycle. Tout est
best-effort avec un timeout de 10 s ; si le webhook est lent, cassé ou
mal configuré, le trading continue et l'erreur est simplement avalée.

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
