# Deploiement prix-cartes sur VPS

## Objectifs

- Deployer l'app sur `https://prix-cartes.lescartesauxtresors.fr`
- Batch automatique quotidien a 3h du matin
- Interface admin pour configurer limite API et activer/desactiver le batch
- Securisation des credentials

---

## Architecture cible

```
+-------------------------------------------------------------+
|                     VPS (72.62.25.103)                      |
+-------------------------------------------------------------+
|  Caddy (HTTPS automatique + Basic Auth)                     |
|    - prix-cartes.lescartesauxtresors.fr -> localhost:5002   |
+-------------------------------------------------------------+
|  Docker Container "prix-cartes"                             |
|    - Flask Admin UI (gunicorn) : port 5002                  |
|    - SQLite volume : /opt/apps/prix-cartes/data/            |
|    - Cron interne : batch quotidien                         |
+-------------------------------------------------------------+
```

---

## Phase 1 : Modifications du code (local)

### 1.1 Securiser les credentials

**Fichier : `src/config.py`**

Modifier pour lire les variables d'environnement :
- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`
- `FLASK_SECRET_KEY`

Fallback sur `config.yaml` pour les autres parametres (pricing, guardrails, etc.)

**Fichier : `config.yaml.example`** (nouveau)

Template sans les secrets, a commiter dans git.

### 1.2 Ajouter table `settings` en base

**Fichier : `src/models.py`**

```python
class Settings(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

Parametres a stocker :

| Cle | Valeur par defaut | Description |
|-----|-------------------|-------------|
| `batch_enabled` | `true` | Activer le batch auto |
| `batch_hour` | `3` | Heure d'execution (0-23) |
| `daily_api_limit` | `5000` | Limite appels eBay/jour |

### 1.3 Page Settings dans l'admin

**Fichier : `admin/app.py`**

Ajouter :
- Route `GET /settings` : afficher formulaire
- Route `POST /settings` : sauvegarder les parametres
- Afficher l'usage API du jour (depuis table `api_usage`)

**Fichier : `admin/templates/settings.html`** (nouveau)

- Formulaire avec les 3 champs
- Toggle on/off pour `batch_enabled`
- Input number pour limite et heure
- Affichage usage API actuel

### 1.4 Modifier le batch runner

**Fichier : `src/batch/runner.py`**

- Lire `daily_api_limit` depuis la table `settings`
- Avant chaque appel eBay : verifier si limite atteinte
- Si oui : log + arret propre du batch

### 1.5 Creer les fichiers Docker

**Fichier : `Dockerfile`**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Installer cron
RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

# Dependances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Code application
COPY . .

# Crontab
COPY crontab /etc/cron.d/prix-cartes
RUN chmod 0644 /etc/cron.d/prix-cartes
RUN crontab /etc/cron.d/prix-cartes

# Creer dossier logs
RUN mkdir -p /var/log

# Port
EXPOSE 5000

# Demarrer cron + gunicorn
CMD ["sh", "-c", "cron && gunicorn -b 0.0.0.0:5000 -w 2 admin.app:app"]
```

**Fichier : `docker-compose.yml`**

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    ports:
      - "127.0.0.1:5002:5000"
    volumes:
      - ./data:/app/data
    environment:
      - EBAY_CLIENT_ID=${EBAY_CLIENT_ID}
      - EBAY_CLIENT_SECRET=${EBAY_CLIENT_SECRET}
      - FLASK_SECRET_KEY=${FLASK_SECRET_KEY}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

**Fichier : `crontab`**

```
# Verifie toutes les heures si c'est l'heure du batch
0 * * * * cd /app && python scripts/run_scheduled_batch.py >> /var/log/batch.log 2>&1
```

**Fichier : `scripts/run_scheduled_batch.py`** (nouveau)

```python
#!/usr/bin/env python3
"""
Script execute par cron toutes les heures.
Verifie si le batch est active et si c'est l'heure configuree.
"""
import sys
from datetime import datetime
sys.path.insert(0, '/app')

from src.database import get_session
from src.models import Settings

def get_setting(session, key, default):
    setting = session.query(Settings).filter_by(key=key).first()
    return setting.value if setting else default

def main():
    session = get_session()
    try:
        # Verifier si batch active
        enabled = get_setting(session, 'batch_enabled', 'true')
        if enabled.lower() != 'true':
            print(f"[{datetime.now()}] Batch desactive, skip")
            return

        # Verifier l'heure
        batch_hour = int(get_setting(session, 'batch_hour', '3'))
        current_hour = datetime.now().hour

        if current_hour != batch_hour:
            # Pas l'heure, exit silencieux
            return

        print(f"[{datetime.now()}] Lancement du batch...")

        # Lancer le batch
        from src.batch.runner import BatchRunner
        runner = BatchRunner()
        runner.run()

        print(f"[{datetime.now()}] Batch termine")

    finally:
        session.close()

if __name__ == '__main__':
    main()
```

---

## Phase 2 : Configuration VPS

### 2.1 DNS (Hostinger)

Ajouter enregistrement A dans la zone DNS :

```
Type: A
Nom: prix-cartes
Valeur: 72.62.25.103
TTL: 3600
```

### 2.2 Caddy

Editer `/etc/caddy/Caddyfile` :

```
prix-cartes.lescartesauxtresors.fr {
    basicauth {
        admin <mot_de_passe_hashe>
    }
    reverse_proxy localhost:5002
}
```

Generer le hash du mot de passe :

```bash
caddy hash-password --plaintext "ton_mot_de_passe_secret"
```

Recharger Caddy :

```bash
systemctl reload caddy
```

### 2.3 Creer le dossier application

```bash
mkdir -p /opt/apps/prix-cartes/data
```

---

## Phase 3 : Deploiement

### 3.1 Depuis le Mac

```bash
# Envoyer les fichiers (exclure data, venv, cache)
rsync -avz \
  --exclude='data/' \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='.git/' \
  --exclude='*.pyc' \
  ~/Dev/prix-cartes/ \
  root@72.62.25.103:/opt/apps/prix-cartes/
```

### 3.2 Sur le VPS

```bash
cd /opt/apps/prix-cartes

# Creer le fichier .env avec les credentials
cat > .env << 'EOF'
EBAY_CLIENT_ID=votre_client_id_ebay
EBAY_CLIENT_SECRET=votre_client_secret_ebay
FLASK_SECRET_KEY=une_cle_secrete_aleatoire_tres_longue_minimum_32_caracteres
EOF

# Securiser le fichier
chmod 600 .env

# Build et lancer
docker compose up -d --build

# Verifier
docker compose ps
docker compose logs -f app
```

### 3.3 Verification

```bash
# Test local sur le VPS
curl -I http://localhost:5002

# Test externe (apres propagation DNS)
curl -u admin:mot_de_passe -I https://prix-cartes.lescartesauxtresors.fr
```

---

## Commandes utiles

### Docker

```bash
# Voir les logs
docker compose logs -f app

# Voir les logs du batch
docker compose exec app cat /var/log/batch.log

# Relancer apres modification
docker compose up -d --build

# Arreter
docker compose down

# Redemarrer
docker compose restart app
```

### Batch manuel

```bash
# Lancer un batch manuellement
docker compose exec app python cli.py run-batch

# Voir les stats
docker compose exec app python cli.py stats
```

### Base de donnees

```bash
# Acceder a SQLite
docker compose exec app sqlite3 data/pricing.db

# Commandes SQL utiles
.tables
SELECT * FROM settings;
SELECT * FROM api_usage WHERE usage_date = date('now');
SELECT * FROM batch_runs ORDER BY started_at DESC LIMIT 5;
```

### Mise a jour

```bash
# Depuis le Mac
rsync -avz \
  --exclude='data/' \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='.git/' \
  ~/Dev/prix-cartes/ \
  root@72.62.25.103:/opt/apps/prix-cartes/

# Sur le VPS
cd /opt/apps/prix-cartes
docker compose up -d --build
```

---

## Ports utilises (mise a jour VPS)

| Port | Application |
|------|-------------|
| 80 | Caddy (HTTP -> HTTPS) |
| 443 | Caddy (HTTPS) |
| 3000 | cards-manager backend |
| 3001 | pokeventes |
| 5002 | **prix-cartes** |
| 5678 | n8n |
| 8080 | cards-manager frontend |
| 8081 | cashechange |

---

## Checklist deploiement

### Code (local)

- [ ] Modifier `src/config.py` pour lire env vars
- [ ] Creer `config.yaml.example`
- [ ] Ajouter modele `Settings` dans `src/models.py`
- [ ] Creer page `/settings` dans `admin/app.py`
- [ ] Creer template `admin/templates/settings.html`
- [ ] Modifier `src/batch/runner.py` pour limite API
- [ ] Creer `Dockerfile`
- [ ] Creer `docker-compose.yml`
- [ ] Creer `crontab`
- [ ] Creer `scripts/run_scheduled_batch.py`
- [ ] Tester en local avec Docker

### VPS

- [ ] Ajouter enregistrement DNS A
- [ ] Attendre propagation DNS
- [ ] Configurer Caddy + Basic Auth
- [ ] Creer dossier `/opt/apps/prix-cartes/data`
- [ ] rsync des fichiers
- [ ] Creer `.env` avec credentials
- [ ] `docker compose up -d --build`
- [ ] Verifier les logs
- [ ] Tester l'acces HTTPS
- [ ] Verifier la page Settings

---

## Troubleshooting

### Le batch ne se lance pas

1. Verifier les logs : `docker compose exec app cat /var/log/batch.log`
2. Verifier que cron tourne : `docker compose exec app ps aux | grep cron`
3. Verifier les settings en base : `SELECT * FROM settings;`

### Erreur 502 Bad Gateway

1. Verifier que le container tourne : `docker compose ps`
2. Verifier les logs : `docker compose logs app`
3. Verifier le port : `curl http://localhost:5002`

### Erreur d'authentification eBay

1. Verifier les credentials dans `.env`
2. Verifier que les variables sont chargees : `docker compose exec app env | grep EBAY`

### Base de donnees corrompue

1. Arreter l'app : `docker compose down`
2. Sauvegarder : `cp data/pricing.db data/pricing.db.bak`
3. Verifier l'integrite : `sqlite3 data/pricing.db "PRAGMA integrity_check;"`
