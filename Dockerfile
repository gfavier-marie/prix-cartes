FROM python:3.11-slim

WORKDIR /app

# Installer cron et curl (pour healthcheck)
RUN apt-get update && apt-get install -y cron curl && rm -rf /var/lib/apt/lists/*

# Dependances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Code application
COPY . .

# Crontab
COPY crontab /etc/cron.d/prix-cartes
RUN chmod 0644 /etc/cron.d/prix-cartes
RUN crontab /etc/cron.d/prix-cartes

# Creer dossier logs et data
RUN mkdir -p /var/log /app/data

# Port
EXPOSE 5000

# Variables d'environnement pour Python
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Demarrer cron + gunicorn
CMD ["sh", "-c", "cron && gunicorn -b 0.0.0.0:5000 -w 2 --timeout 120 admin.app:app"]
