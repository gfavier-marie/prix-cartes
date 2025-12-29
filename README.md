# Prix-Cartes

Outil de calcul automatique des prix de rachat de cartes Pokemon pour Pokeventes.fr.

## Installation

```bash
# Creer un environnement virtuel
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou: venv\Scripts\activate  # Windows

# Installer les dependances
pip install -r requirements.txt
```

## Configuration

1. Copier et editer la configuration :
```bash
cp config.yaml config.local.yaml
```

2. Remplir les credentials eBay dans `config.yaml` :
```yaml
ebay:
  client_id: "YOUR_EBAY_CLIENT_ID"
  client_secret: "YOUR_EBAY_CLIENT_SECRET"
```

### Obtenir les credentials eBay

1. Creer un compte sur [eBay Developer Program](https://developer.ebay.com/)
2. Creer une application (Production)
3. Recuperer le Client ID et Client Secret
4. L'API utilisee est la **Browse API** (scope: `https://api.ebay.com/oauth/api_scope`)

## Utilisation

### 1. Initialiser la base de donnees

```bash
python cli.py init
```

### 2. Importer les cartes depuis TCGdex

```bash
# Importer tous les sets
python cli.py import-tcgdex

# Importer un set specifique
python cli.py import-tcgdex --set swsh12

# Mettre a jour uniquement les prix Cardmarket
python cli.py import-tcgdex --update-pricing
```

### 3. Generer les requetes eBay

```bash
python cli.py generate-queries
```

### 4. Lancer un batch de pricing

```bash
# Batch complet (eBay + garde-fous)
python cli.py run-batch

# Mode hybride (Cardmarket principalement)
python cli.py run-batch --mode hybrid

# Limiter le nombre de cartes (pour test)
python cli.py run-batch --limit 100

# Traiter des cartes specifiques
python cli.py run-batch --card-id 123 --card-id 456
```

### 5. Exporter les prix en CSV

```bash
# Export standard (cartes OK uniquement)
python cli.py export-csv output.csv

# Export complet avec toutes les colonnes
python cli.py export-csv --full output_full.csv

# Export des anomalies pour review
python cli.py export-csv --anomalies anomalies.csv

# Inclure les cartes a faible confiance
python cli.py export-csv --include-low-conf output.csv
```

### 6. Interface admin

```bash
python cli.py admin
# Ouvre http://127.0.0.1:5000
```

### Commandes utiles

```bash
# Voir les statistiques
python cli.py stats

# Tester une requete eBay
python cli.py test-ebay '"Dracaufeu" pokemon card -lot -graded'

# Tester le pricing d'une carte
python cli.py test-card 123
```

## Structure du projet

```
prix-cartes/
├── src/
│   ├── config.py          # Configuration
│   ├── models.py          # Modeles SQLAlchemy
│   ├── database.py        # Gestion DB
│   ├── tcgdex/            # Client TCGdex
│   ├── ebay/              # Client eBay + worker
│   ├── pricing/           # Calcul des prix
│   ├── batch/             # Orchestration
│   └── export/            # Export CSV
├── admin/                 # Interface web Flask
├── cli.py                 # Point d'entree CLI
├── config.yaml            # Configuration
└── data/                  # Base SQLite
```

## Logique de pricing

### Sources de donnees
- **TCGdex** : Identifiants cartes, variants, prix Cardmarket
- **eBay Browse API** : Annonces actives, prix effectifs

### Calcul du prix de rachat

```
anchor_price = p20 des prix eBay (percentile 20)
risk = f(dispersion, supply, sample_size, source)
buy_price = anchor_price * (1 - fees - margin - risk) - fixed_costs
```

### Garde-fous
- Mismatch si eBay > 2.5x Cardmarket ou < 0.4x
- Mismatch si dispersion > 4.0
- Fallback vers Cardmarket en cas de mismatch

### Etats des cartes
- Neuf : coefficient 1.00
- Bon : coefficient 0.60
- Correct : coefficient 0.30

## Frequence recommandee

Batch trimestriel (tous les 3-4 mois) comme specifie dans les specs.
