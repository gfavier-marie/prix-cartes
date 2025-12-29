# Projet Prix-Cartes - Outil de Pricing Pokeventes

## Contexte

Outil standalone pour calculer automatiquement les **prix de rachat** de cartes Pokemon pour Pokeventes.fr, basé sur les prix eBay avec TCGdex comme source de référence pour les cartes.

**Output** : fichier CSV à importer sur Pokeventes.

## Stack technique

- **Python 3.11+**
- **SQLite** (fichier `data/pricing.db`)
- **SQLAlchemy** (ORM)
- **Flask + htmx** (interface admin)
- **httpx** (client HTTP async)
- **pandas** (export CSV)
- **click** (CLI)

## Architecture

```
src/
├── config.py              # Paramètres configurables (margins, fees, seuils)
├── models.py              # Modèles SQLAlchemy
├── database.py            # Init DB, sessions
├── tcgdex/                # Import cartes depuis TCGdex API
├── ebay/                  # Client eBay Browse API + collecte prix
├── pricing/               # Calcul prix rachat + guardrails
├── batch/                 # Orchestration batch trimestriel
└── export/                # Export CSV

admin/                     # Interface web Flask pour overrides
cli.py                     # Point d'entrée CLI
```

## Logique métier clé

### Sources de données
1. **TCGdex** : identifiants cartes, variants, pricing Cardmarket (trend/avg7/avg30)
2. **eBay Browse API** : annonces actives, prix effectif = prix + port

### Calcul du prix de rachat
```
anchor_price = p20 des prix eBay (percentile 20)
risk = f(dispersion, supply, sample_size, source)
buy_price = anchor_price * (1 - fees - margin - risk) - fixed_costs
```

### Garde-fous
- Si mismatch eBay vs Cardmarket (>2.5x ou <0.4x) → fallback Cardmarket
- Si dispersion > 4.0 → fallback Cardmarket
- Score de confiance basé sur sample_size, dispersion, source

### Etats des cartes
- Neuf : coef 1.00
- Bon : coef 0.6
- Correct : coef 0.3

## Paramètres principaux (config.yaml)

- `MIN_CARD_VALUE_EUR`: 3.00 (exclure cartes < 3€)
- `MARGIN_TARGET`: 0.27 (marge cible 25-30%)
- `FEES_RATE`: 0.11 (frais eBay + paiement)
- `FIXED_COSTS_EUR`: 0.30-3.00
- `MISMATCH_UPPER`: 2.5
- `MISMATCH_LOWER`: 0.4
- `DISPERSION_BAD`: 4.0

## Commandes CLI

```bash
# Import des cartes depuis TCGdex
python cli.py import-tcgdex

# Lancer un batch de pricing
python cli.py run-batch [--full|--hybrid]

# Exporter le CSV
python cli.py export-csv output.csv

# Lancer l'interface admin
python cli.py admin
```

## Tables principales

- `cards` : cartes avec ebay_query, cm_trend, cm_avg30, etc.
- `market_snapshots` : historique des collectes (p20/p50/p80, dispersion, anchor)
- `buy_prices` : prix de rachat calculés (neuf/bon/correct)
- `batch_runs` : logs des batches

## Credentials requis

- **eBay Developer** : Client ID + Client Secret (OAuth2 Client Credentials)
  - Scope : `https://api.ebay.com/oauth/api_scope`
  - Endpoint : Buy Browse API v1

## Notes importantes

- Mise à jour trimestrielle (tous les 3-4 mois)
- Exclure : lots, graded (PSA/CGC), proxy, codes online
- Mots-clés négatifs toujours ajoutés aux requêtes eBay
- Override manuel possible via `ebay_query_override`
