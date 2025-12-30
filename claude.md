# Projet Prix-Cartes - Outil de Pricing Pokeventes

## Contexte

Outil standalone pour calculer automatiquement les **prix de rachat** de cartes Pokemon pour Pokeventes.fr, basé sur les prix eBay avec TCGdex comme source de référence pour les cartes.

**Output** : fichier CSV à importer sur Pokeventes.

---

## Stack technique

- **Python 3.11+**
- **SQLite** (fichier `data/pricing.db`) avec WAL mode
- **SQLAlchemy 2.0+** (ORM)
- **Flask** (interface admin) - Note: htmx mentionné mais **non utilisé** (JavaScript vanilla)
- **httpx** (client HTTP async avec retry/backoff)
- **pandas + numpy** (export CSV, calculs statistiques)
- **click + rich** (CLI formaté)
- **tenacity** (retry logic)

---

## Architecture détaillée

```
prix-cartes/
├── src/                          # Core logic (22 fichiers, ~5200 lignes)
│   ├── config.py                 # Config centralisée (dataclasses + YAML + env)
│   ├── models.py                 # 9 tables SQLAlchemy (Card, Set, MarketSnapshot, BuyPrice, etc.)
│   ├── database.py               # Engine SQLite, sessions, WAL mode
│   ├── tcgdex/
│   │   ├── client.py             # API TCGdex (rate limited 5 req/s)
│   │   └── importer.py           # Import cartes + pricing Cardmarket
│   ├── ebay/
│   │   ├── client.py             # OAuth2 + Browse API v1 (585 lignes)
│   │   ├── query_builder.py      # Génération requêtes optimisées (100 chars max)
│   │   ├── worker.py             # Collecte + stats (p10-p90, dispersion, consensus)
│   │   └── usage_tracker.py      # Suivi API quotidien (5000 calls/jour)
│   ├── pricing/
│   │   ├── calculator.py         # Formule prix rachat + risk factors
│   │   ├── guardrails.py         # Fallback Cardmarket si mismatch/dispersion
│   │   └── confidence.py         # Score 0-100 multi-critères
│   ├── batch/
│   │   ├── runner.py             # Pipeline complet avec anomaly detection
│   │   └── queue.py              # Queue parallèle ThreadPoolExecutor
│   └── export/
│       └── csv_export.py         # Multi-formats (standard, full, anomalies, sales)
│
├── admin/                        # Interface Flask (~4000 lignes total)
│   ├── app.py                    # 40+ routes, 1900 lignes
│   └── templates/                # 14 templates Jinja2
│       ├── base.html             # Layout + thème dark + nav sticky
│       ├── cards.html            # Liste paginée + filtres avancés
│       ├── card_detail.html      # Fiche complète + annonces AJAX
│       ├── batch.html            # Lancement queue + stats par set
│       ├── tcgdex.html           # Sync + visibilité séries/sets
│       ├── import.html           # Import CSV avec doc
│       ├── settings.html         # Config batch + usage API
│       ├── anomalies.html        # Dispersion, confiance, mismatches
│       └── ventes.html           # Sold listings détectés
│
├── scripts/
│   └── run_scheduled_batch.py    # Scheduler hourly (cron)
│
├── cli.py                        # 15 commandes Click (~600 lignes)
├── config.yaml.example           # Template configuration
├── Dockerfile                    # Python 3.11-slim + cron + gunicorn
├── docker-compose.yml            # Orchestration VPS
└── crontab                       # Batch automatique (configurable via Settings)
```

---

## Modèles de données principaux

### Card (table principale)
- **Identifiants** : `tcgdex_id` (unique), `set_id`, `local_id`, `variant` (NORMAL/REVERSE/HOLO/FIRST_ED)
- **TCGdex** : `name`, `set_name`, `rarity`, `card_number_full`
- **Cardmarket** : `cm_trend`, `cm_avg1`, `cm_avg7`, `cm_avg30`
- **eBay** : `ebay_query`, `ebay_query_override`
- **Overrides** : `name_override`, `local_id_override`, `card_number_format`, `card_number_padded`
- **Properties dynamiques** : `effective_ebay_query`, `effective_name`, `cm_max`

### MarketSnapshot (historique collecte)
- Stats : `p10`, `p20`, `p50`, `p80`, `p90`, `dispersion`, `cv`, `iqr`
- Temporalité : `age_median_days`, `pct_recent_7d`, `pct_old_30d`
- Qualité : `consensus_score`, `confidence_score`
- Ancre : `anchor_price`, `anchor_source` (EBAY_ACTIVE/CARDMARKET_FALLBACK/LAST_KNOWN)

### BuyPrice (résultat final)
- `buy_neuf`, `buy_bon`, `buy_correct`
- `status` (OK/LOW_CONF/DISABLED)
- Pas d'historique complet (overwrite chaque batch)

---

## Logique métier clé

### Sources de données
1. **TCGdex** : identifiants cartes, variants, pricing Cardmarket (trend/avg7/avg30)
2. **eBay Browse API** : annonces actives, prix effectif = prix + port

### Calcul du prix de rachat
```
anchor_price = p20 des prix eBay (percentile 20)

risk = base (0.02)
     + k1 * clamp(log(dispersion), 0, 2)
     + k2 * clamp(log(1 + active/1000), 0, 2)
     + k3 * (sample < 10 ? 0.05 : 0)
     + k4 * (fallback ? 0.03-0.045 : 0)
     + consensus_adjustment (-0.02 à +0.05)
     + age_adjustment (0 à +0.05)

buy_price = anchor * (1 - fees - margin - risk) - fixed_costs
buy_etat = buy_price * coef_etat
```

### Garde-fous (guardrails)
- Si `anchor > 2.5 * cm_max` → fallback Cardmarket (trop cher eBay)
- Si `anchor < 0.4 * cm_max` → fallback Cardmarket (trop bon marché)
- Si `dispersion > 4.0` → fallback Cardmarket (marché volatil)
- Ordre fallback : Cardmarket → Dernier prix connu → None

### Score de confiance (0-100)
- Sample size : 30 pts (0→0, 30+→30)
- Dispersion : 25 pts (≤1.5→25, >4→5)
- Cardmarket présent : 15 pts
- Source : 20 pts (eBay=20, CM=12, Last=6)
- Stabilité vs batch précédent : 10 pts

### Etats des cartes
- Neuf : coef 1.00
- Bon : coef 0.60
- Correct : coef 0.30

---

## Paramètres principaux (config.yaml)

```yaml
pricing:
  min_card_value_eur: 3.0     # Exclure cartes < 3€
  margin_target: 0.27         # Marge cible 27%
  fees_rate: 0.11             # Frais eBay + paiement 11%
  fixed_costs_eur: 0.5        # Coûts fixes
  risk_base: 0.02             # Buffer risque de base
  rounding_step: 0.1          # Arrondi 10 centimes

guardrails:
  mismatch_upper: 2.5         # Anchor > 2.5x CM = fallback
  mismatch_lower: 0.4         # Anchor < 0.4x CM = fallback
  dispersion_bad: 4.0         # Dispersion > 4 = fallback

ebay:
  sample_limit: 200           # Max items par requête
  daily_limit: 5000           # Appels API/jour
  marketplace_id: EBAY_FR

tcgdex:
  language: fr
  excluded_series: [tcgp, mc, tk, misc]
```

---

## Commandes CLI

```bash
# Initialisation
python cli.py init [--force]              # Créer/reset la base de données

# Import données
python cli.py import-tcgdex               # Import complet TCGdex
python cli.py import-tcgdex --set base1   # Import un set spécifique
python cli.py import-tcgdex --update-pricing  # MAJ prix Cardmarket seulement
python cli.py generate-queries [--force]  # Générer requêtes eBay

# Batch pricing
python cli.py run-batch                   # Batch complet
python cli.py run-batch --mode hybrid     # Mode mixte eBay+CM
python cli.py run-batch --limit 100       # Limiter à N cartes
python cli.py run-batch --card-id X       # Cartes spécifiques

# Export
python cli.py export-csv output.csv       # Export standard Pokeventes
python cli.py export-csv out.csv --full   # Export avec toutes colonnes
python cli.py export-csv out.csv --anomalies  # Export anomalies
python cli.py export-sales sales.csv      # Export ventes détectées
python cli.py export-sales sales.csv --summary  # Résumé par carte

# Debug
python cli.py test-ebay "pikachu 25/102"  # Tester requête eBay
python cli.py test-card CARD_ID           # Tester pricing complet
python cli.py listings CARD_ID [--refresh]  # Voir annonces

# Admin
python cli.py admin [--port 5000]         # Lancer interface web
python cli.py stats                       # Statistiques DB
python cli.py migrate-sets                # Migration sets existants
python cli.py create-ed1-variants --all-old-sets  # Créer variantes Ed1
```

---

## Routes admin principales

| Route | Description |
|-------|-------------|
| `/` | Dashboard avec stats clés |
| `/cards` | Liste paginée avec filtres (set, date, erreur, données) |
| `/cards/<id>` | Fiche complète + annonces AJAX + overrides |
| `/batch` | Lancement queue par sets + auto-select prioritaire |
| `/batches` | Historique des batches |
| `/tcgdex` | Sync + gestion visibilité séries/sets |
| `/import` | Import CSV avec documentation |
| `/export/csv` | Télécharger CSV complet |
| `/settings` | Config batch automatique + usage API |
| `/anomalies` | Cartes problématiques (dispersion, confiance, mismatch) |
| `/ventes` | Sold listings détectés |

---

## Credentials requis

Variables d'environnement (NE PAS stocker dans config.yaml) :
- `EBAY_CLIENT_ID` : Client ID eBay Developer
- `EBAY_CLIENT_SECRET` : Client Secret eBay
- `FLASK_SECRET_KEY` : Clé secrète Flask (sessions)

eBay OAuth2 :
- Scope : `https://api.ebay.com/oauth/api_scope`
- Endpoint : Buy Browse API v1
- Quota : 5000 appels/jour (production)

---

## Déploiement

### Local (dev)
```bash
./launch.sh       # Toggle start/stop sur port 5001
./stop.sh         # Force stop
```

### Docker (production)
```bash
docker compose up -d --build
# Port exposé : 127.0.0.1:5002 (reverse proxy Caddy)
# Batch auto : configurable via /settings (défaut: 3h)
# Logs : /var/log/batch.log
```

---

## Batch automatique (cron)

### Fonctionnement
Le script `scripts/run_scheduled_batch.py` est exécuté **toutes les heures** par cron.

```
Cron (0 * * * *)
    ↓
batch_enabled == "true" ? (Settings)
    ↓
Heure == batch_hour ? (Settings, défaut: 3h)
    ↓
Usage API < daily_api_limit ? (Settings, défaut: 5000)
    ↓
BatchRunner.run(prioritize_oldest=True)
```

### Priorisation des cartes
1. **Exclut** les cartes en erreur depuis moins de 24h (évite de bloquer sur un set problématique)
2. **Cartes jamais traitées** (pas de MarketSnapshot) → en premier
3. **Cartes les plus anciennes** (snapshot le plus vieux) → ensuite
4. Continue jusqu'à épuisement de la limite API

### Gestion des erreurs
- Si une carte échoue → `last_error_at` = maintenant
- Cette carte est **exclue pendant 24h** des prochains batchs
- Après 10 échecs sur un set → **skip ce set**, continue avec les autres
- Les sets skippés sont loggés dans le rapport
- Le batch ne s'arrête **jamais** pour cause d'échecs (seulement pour limite API ou arrêt manuel)

### Limite API stricte
- Vérifie **AVANT** chaque carte si la limite est atteinte
- Si `daily_api_limit=4000` et usage actuel=3999 → **1 seule requête** puis arrêt
- Le compteur est incrémenté après chaque appel eBay

### Paramètres (via /settings ou Settings table)
| Clé | Défaut | Description |
|-----|--------|-------------|
| `batch_enabled` | "true" | Active/désactive le batch |
| `batch_hour` | "3" | Heure d'exécution (0-23) |
| `batch_minute` | "0" | Minute d'exécution (0-59) |
| `daily_api_limit` | "5000" | Limite d'appels API/jour |

### Logs
- Docker : `/var/log/batch.log`
- Format : `[YYYY-MM-DD HH:MM:SS] message`

---

## Faiblesses et incohérences identifiées

### CRITIQUE - Sécurité

| Issue | Impact | Statut |
|-------|--------|--------|
| **Pas d'authentification admin** | Accès public à toutes les routes | ✅ Résolu via auth Caddy (reverse proxy) |
| **Pas de CSRF protection** | Vulnérable aux attaques CSRF | ⚠️ Acceptable pour usage interne mono-utilisateur |
| **Debug mode hardcodé** | RCE potentiel en production | ⚠️ Mitigé par auth Caddy, mais à corriger |
| **Credentials eBay** | Exposition des secrets | ✅ Via env vars uniquement |

### HAUTE - Architecture

| Issue | Impact | Solution |
|-------|--------|----------|
| **Pas de tests** (0% couverture) | Régressions non détectées | Ajouter pytest + tests pricing |
| **BuyPrice sans historique** | Impossible de tracer l'évolution | Ajouter `batch_run_id` FK |
| **Double stockage TCGdex** | `tcgdex_db.py` vs `models.py` | Supprimer tcgdex_db.py |
| ~~**Rate limit eBay naïf**~~ | ~~5000/jour = ~100 cartes max~~ | ✅ Priorisation + limite stricte |

### MOYENNE - Code

| Issue | Impact | Solution |
|-------|--------|----------|
| **Ports incohérents** (5000/5001/5002) | Confusion config | Normaliser |
| ~~**Chemins hardcodés crontab**~~ | ~~Incompatible local~~ | ✅ Chemins relatifs |
| **JS inline dans templates** | Maintenance difficile | Extraire en fichiers .js |
| **Pas de validation config** | Crash si params invalides | Ajouter pydantic |
| **Seuils arbitraires** (2.5x, 0.4x, 4.0) | Non documentés | Justifier ou rendre configurables |

### BASSE - UX

| Issue | Impact | Solution |
|-------|--------|----------|
| **Tables non responsive** | Mobile inutilisable | Wrapper scrollable |
| **Statut batch invisible** | Ne sait pas si en cours | Badge dans nav |
| **Import CSV sans preview** | Perte de données | Client-side preview |

---

## Recommandations prioritaires

### Phase 1 - Urgent (1-2 jours)
1. ~~Révoquer et renouveler credentials eBay si exposées~~ ✅ (env vars)
2. ~~Ajouter authentification basique admin~~ ✅ (auth Caddy)
3. Conditionner `debug=True` sur variable env (optionnel, mitigé par Caddy)

### Phase 2 - Court terme (1-2 semaines)
4. Ajouter tests unitaires pour `pricing/calculator.py`
5. ~~Ajouter CSRF protection~~ (acceptable pour usage interne)
6. Historiser BuyPrice (FK vers BatchRun)
7. Normaliser les ports

### Phase 3 - Moyen terme (1 mois)
8. ~~Rate limiting adaptatif~~ ✅ (priorisation par ancienneté)
9. Cache requêtes TCGdex/eBay (24h)
10. Traçabilité des overrides (audit log)
11. Export Excel (pandas xlsxwriter)

### Phase 4 - Long terme
12. CI/CD avec GitHub Actions
13. Tests d'intégration
14. Monitoring (logs structurés)
15. Notifications batch (email/webhook)

---

## Notes importantes

- Mise à jour trimestrielle recommandée (tous les 3-4 mois)
- Exclure : lots, graded (PSA/CGC), proxy, codes online, japanese
- Mots-clés négatifs toujours ajoutés aux requêtes eBay
- Override manuel possible via `ebay_query_override` ou interface admin
- Consensus eBay : % d'annonces à ±20% du p50 (fiabilité marché)
- Dispersion = p80/p20 (volatilité des prix)

---

## Métriques projet

| Métrique | Valeur |
|----------|--------|
| Fichiers Python | 22 |
| Lignes de code | ~5200 (src) + ~4000 (admin) |
| Tables SQLAlchemy | 9 |
| Routes Flask | 40+ |
| Commandes CLI | 15 |
| Couverture tests | 0% |
