# Spécification exhaustive — Calcul des prix de rachat Pokéventes (TCGdex + eBay)

## 0) Contexte & objectif

**Site :** Pokéventes.fr / calculateur de rachat  
**Problème :** calculer des **prix de rachat** automatiquement, en se basant sur **les prix de vente eBay**, avec une **mise à jour tous les 3–4 mois**, sur **toutes les cartes** (en excluant les cartes à très faible valeur ~1–3€).  
**Contrainte :** pas assez de volume interne pour déduire la vitesse de vente ; pas d’accès simple aux “sold listings” en API publique → on utilise des proxies robustes (annonces actives + garde-fous).

**Cible business :**
- Aujourd’hui : achat ~15–20% du prix de revente final, marge ~50–60%.
- Test : monter les prix d’achat et viser une marge **25–30%** sur un sous-ensemble / puis généraliser.

## 1) Principes de la solution

### 1.1 Source de vérité “cartes” : TCGdex
TCGdex sert à :
- un identifiant stable par carte (`card.id`), set (`set.id`), numéro (`localId`)
- les **variants** (normal / reverse / holo / first edition)
- un **filet de sécurité pricing** via `pricing.cardmarket` (trend/avg7/avg30) pour détecter les mauvais match eBay et faire un fallback.

### 1.2 Prix “eBay” : ancre sur annonces actives (robuste)
On calcule un **prix ancre eBay** à partir des **annonces actives** (Buy Browse API `item_summary/search`) :
- récupérer un échantillon d’annonces (50–100 items)
- calculer des **percentiles** sur le **prix effectif = prix + port** (normalisé en EUR)
- choisir une ancre robuste : **p20 (ou p25)**, plutôt que moyenne ou p50.

Pourquoi p20/p25 ?
- résiste aux annonces trop chères / jamais vendues
- se rapproche d’un prix “compétitif” et “réalisable” à la vente.

### 1.3 Proxy de “liquidité” sans vitesse de vente
Comme on met à jour tous les 3–4 mois, on n’a pas besoin d’un tracking quotidien complet.
On utilise :
- **Supply** : `active_count` (le total renvoyé par la recherche eBay)
- **Risque/qualité** : dispersion des prix `p80/p20`
- **Confiance** : nb d’items réellement échantillonnés, stabilité, comparaison à Cardmarket.

(Option future : tracker la disparition d’items pour estimer un turnover, mais non requis pour un batch trimestriel.)

---

## 2) Définition du périmètre : quelles cartes calculer ?

### 2.1 Exclusion cartes “faible valeur” (1–3€)
**Règle recommandée :**
- si `pricing.cardmarket.avg30` ou `trend` est disponible :
  - **inclure** la carte si `max(trend, avg30) >= 3.0`
  - sinon **exclure** (ou mettre buy_price=0.00)
- si Cardmarket indisponible : inclure mais tagger en **faible confiance**.

### 2.2 Cartes qui changent de tranche (2€ → 6€)
Pour éviter d’exclure une carte qui remonte :
- à chaque batch trimestriel : **recalcul** du filtre via Cardmarket (cheap)
- option : 1 fois/an faire un “light scan” eBay sur les exclues (échantillon) si besoin.

---

## 3) Génération et maintenance des requêtes eBay (le point critique)

### 3.1 Champ `ebay_query` stocké en base (indispensable)
Pour chaque carte, stocker une requête eBay finalisée :
- générée automatiquement à l’initialisation
- corrigée manuellement sur les cas “difficiles”
- stable d’un batch à l’autre.

### 3.2 Règles de construction automatique

**Inputs TCGdex :**
- `card.name` (FR ou EN selon ton marché)
- `set.name` + éventuellement `set.tcgOnline` (code du set)
- `localId` (numéro)
- `set.cardCount.official` (si dispo)
- `variants.*` (normal/reverse/holo/firstEdition)

**Template de base (exemples)**
- Toujours mettre le nom entre guillemets :
  - `"Dracaufeu"` OR `"Charizard"` selon langue ciblée
- Ajouter le set (nom ou code) :
  - `("Épée et Bouclier – Ténèbres Embrasées" OR "DAA" OR "Darkness Ablaze")`
- Ajouter le numéro :
  - `136` et si possible `136/189`
- Ajouter un mot-clé de type :
  - `pokemon card` / `carte pokemon`
- Ajouter les variantes :
  - reverse: `reverse OR "rev holo" OR "reverse holo"`
  - holo: `holo` (sans reverse)
  - firstEdition: `1st OR "first edition"`

**Mots-clés négatifs (anti-bruit)**
Toujours ajouter :
- `-lot -lots -bundle -collection -x10 -x20`
- `-psa -cgc -bgs -graded -slab`
- `-proxy -orica -custom`
- `-"code card" -"online code"`

> Remarque : selon la qualité des résultats, on peut ajouter `-japanese -jpn` si tu ne rachètes pas JAP, ou au contraire forcer `japanese` si tu en veux.

### 3.3 Overrides manuels
Prévoir une table/colonne `ebay_query_override` (ou un champ admin).
Logique :
- si override présent → utiliser override
- sinon → utiliser query auto.

### 3.4 Détection automatique des “mauvais match”
Une requête eBay peut ramener des mauvaises cartes (homonymes, promos, etc.).
On détecte via :
- **dispersion énorme** (p80/p20 très haut)
- **anchor eBay incohérent** vs Cardmarket trend/avg30
- **active_count trop haut** pour une carte censée rare (signal bruit).

---

## 4) Collecte eBay : design du batch

### 4.1 Appel eBay (Buy Browse API)
Endpoint logique :
- `/buy/browse/v1/item_summary/search`

Paramètres usuels (à adapter au SDK) :
- `q=` : la requête textuelle `ebay_query`
- `limit=` : 50 (ou 100 si autorisé)
- `offset=` : pagination (facultatif si on veut juste un échantillon)
- filtres : `buyingOptions`, `conditionIds`, `category_ids` si possible
- `fieldgroups=ASPECT_REFINEMENTS` optionnel

**Stratégie d’échantillonnage**
- objectif : 50–100 items **propres**
- si résultats trop nombreux : prendre les 100 premiers
- si résultats trop faibles (<10) : confiance basse, fallback possible.

### 4.2 Normalisation du prix
Pour chaque item :
- `effective_price = item.price.value + shippingOptions.minShippingCost.value (si présent)`
- convertir en EUR si currency != EUR (utiliser un taux de change du jour du batch, stocké dans `fx_rates`)
- ignorer/penaliser les items sans shipping clair (ou mettre shipping=0 si digital, mais Pokémon = physique).

### 4.3 Nettoyage & outliers
Après avoir collecté les effective_price :
- drop des valeurs <= 0
- si > N items : supprimer extrêmes via trimming (ex: retirer top 5% + bottom 5%)
- calculer percentiles (p20/p50/p80)
- calculer dispersion `dispersion = p80/p20` (si p20>0)

### 4.4 Métriques
Pour chaque carte + batch :
- `active_count` = total retourné par eBay
- `sample_size` = nb d’items utilisés pour les stats
- `p20/p50/p80`
- `dispersion`
- `anchor_price = p20`
- `anchor_source = "EBAY_ACTIVE"`

---

## 5) Garde-fous Cardmarket via TCGdex

### 5.1 Utilisation “filet de sécurité” (pas source principale)
On utilise `pricing.cardmarket` (trend/avg30) pour :
- comparer à l’ancre eBay et détecter mismatch
- fallback si eBay est trop bruité.

### 5.2 Règles de mismatch (à calibrer)
Soit :
- `cm = max(trend, avg30)` si dispo
- mismatch si :
  - `anchor_ebay > 2.5 * cm`  **OU**
  - `anchor_ebay < 0.4 * cm`
  - OU `dispersion > 4.0` (exemple)

Action :
- si mismatch : `anchor_price = cm` et `anchor_source="CARDMARKET_FALLBACK"`
- sinon garder eBay.

### 5.3 Cas sans Cardmarket
Si Cardmarket absent :
- pas de mismatch check possible
- confidence baisse
- possibilité : fallback “dernier prix connu” (from last batch).

---

## 6) Calcul du prix de rachat (buy_price)

### 6.1 Paramètres (configurables)
- `fees_rate` : frais eBay + paiement (ex : 0.11)
- `margin_target` : 0.25–0.30 (test)
- `fixed_costs_eur` : coût fixe moyen par carte/transaction (emballage, pertes, etc.)
- `risk_buffer_base` : ex 0.02–0.08
- `min_buy`, `max_buy` (planchers/plafonds)
- `rounding` (ex : arrondi au 0.10€ ou 0.50€)

### 6.2 Buffer de risque (fonction de la qualité)
Exemple de buffer :
- `risk = risk_base`
- + `k1 * clamp(log(dispersion), 0, 2)`
- + `k2 * clamp(log(1 + active_count/1000), 0, 2)` (supply énorme)
- + `k3` si `sample_size < 10`
- + `k4` si `anchor_source == CARDMARKET_FALLBACK`

### 6.3 Formule buy (base)
`buy_base = anchor_price * (1 - fees_rate - margin_target - risk) - fixed_costs_eur`

Puis :
- clamp : `buy = min(max(buy_base, min_buy), max_buy)`
- arrondi : appliquer `rounding_rule(buy)`

### 6.4 Déclinaison par état (neuf/bon/correct)
Définir des multiplicateurs :
- `coef_neuf = 1.00`
- `coef_bon = 0.85` (exemple)
- `coef_correct = 0.70` (exemple)

Prix final :
- `buy_neuf = buy * coef_neuf`
- `buy_bon = buy * coef_bon`
- `buy_correct = buy * coef_correct`

> Calibrer ces coefficients avec l’expérience (marge réelle + taux de revente/retours).

### 6.5 Confiance / statut
Produire un `confidence_score` (0–100) basé sur :
- sample_size
- dispersion
- présence Cardmarket
- mismatch ou non
- stabilité vs batch précédent (variation %).

Si confiance faible :
- afficher le prix mais tag “estimation”
- ou plafonner le prix de rachat.

---

## 7) Schéma de base de données (suggestion)

### 7.1 `cards`
- `card_id` (PK) — ex `tcgdex_id`
- `name`
- `name_en` (option)
- `set_id`
- `set_name`
- `set_code` (tcgOnline)
- `local_id` (string)
- `card_number_full` (ex "136/189" option)
- `variant` ENUM('NORMAL','REVERSE','HOLO','FIRST_ED')
- `language_scope` (FR/EN/JPN/ANY)
- `rarity` (option)
- `ebay_query` (text)
- `ebay_query_override` (text nullable)
- `cm_trend`, `cm_avg7`, `cm_avg30` (numeric nullable)
- `is_active` boolean
- indexes : `(set_id, local_id, variant)`, `cm_avg30`, `is_active`

### 7.2 `market_snapshots`
- `id` (PK)
- `card_id` (FK)
- `as_of_date` (date)
- `active_count` (int)
- `sample_size` (int)
- `p20`, `p50`, `p80` (numeric)
- `dispersion` (numeric)
- `anchor_price` (numeric)
- `anchor_source` ENUM('EBAY_ACTIVE','CARDMARKET_FALLBACK','LAST_KNOWN')
- `confidence_score` (int)
- `raw_meta` (jsonb : debug: currencies, removed outliers, query used, etc.)
- indexes: `(as_of_date)`, `(card_id, as_of_date DESC)`

### 7.3 `buy_prices`
- `card_id` (PK/FK)
- `updated_at` timestamp
- `buy_neuf`, `buy_bon`, `buy_correct`
- `anchor_price`, `confidence_score`, `anchor_source`, `as_of_date`
- `status` ENUM('OK','LOW_CONF','DISABLED')
- index `(updated_at DESC)`

### 7.4 `batch_runs`
- `batch_id` (PK)
- `started_at`, `finished_at`
- `mode` ENUM('FULL_EBAY','HYBRID')
- `cards_targeted`, `cards_succeeded`, `cards_failed`
- `notes`

### 7.5 `fx_rates` (si conversion devises)
- `date`
- `base` ('EUR')
- `rates` jsonb

---

## 8) Pipeline batch (trimestriel)

### 8.1 Étapes
1. **Import TCGdex** (sets + cards + pricing)
2. **Filtrage** cartes à traiter (>3€ via Cardmarket)
3. **Génération** `ebay_query` (si vide) + apply overrides
4. **Job queue** : pour chaque carte -> fetch eBay -> compute stats -> snapshot
5. **Guardrail** mismatch vs Cardmarket -> choisir anchor final
6. **Compute buy prices** + écrire `buy_prices`
7. **Publication** : le calculateur lit `buy_prices`
8. **Rapport batch** : anomalies, top variations, cartes à corriger (queries)

### 8.2 Scheduling
Fréquence : tous les 3–4 mois.  
Durée : acceptable sur plusieurs jours si quotas eBay.

---

## 9) Observabilité & maintenance

### 9.1 Logs par carte
Stocker dans `market_snapshots.raw_meta` :
- query utilisée
- nb items bruts vs nettoyés
- p5/p95 si calculés
- erreurs eBay / timeouts
- flags mismatch

### 9.2 Alertes
- variation > X% vs batch précédent (ex 60%) → review
- dispersion > seuil
- active_count très haut → query trop large
- sample_size < 10 → revoir query

### 9.3 Interface admin minimale
- liste des cartes en anomalie
- champ pour éditer `ebay_query_override`
- bouton “recompute this card” (job on-demand)
- affichage comparaison : eBay anchor vs Cardmarket vs dernier batch.

---

## 10) Stratégie “FULL eBay” vs “HYBRID”

### 10.1 FULL eBay (référence)
- pour toutes les cartes >3€ :
  - fetch eBay + anchor p20 + guardrail CM
- + fiable “eBay-based” mais coûte plus en appels.

### 10.2 HYBRID (si quotas/temps limités)
- pour toutes : Cardmarket (via TCGdex)
- pour un subset (ex top 2000 ou cartes les plus rachetées) : anchor eBay
- apprendre un coefficient `k(segment) = ebay_anchor / cardmarket_price`
- appliquer au reste : `anchor_est = k * cardmarket`
Segments possibles :
- par set / ère / rareté / variant
- par tranche de prix.

---

## 11) Edge cases (Pokémon)

- Cartes “promo” sans numérotation standard
- Variants confus (holo vs reverse vs cosmos holo)
- Langue : FR/EN/JAP mélangées sur eBay (nécessite mots-clés)
- Lots : “4 cards”, “bundle”
- Graded : PSA/CGC (à exclure)
- “Reprint” / mêmes noms dans plusieurs sets
- “Shadowless”, “1st edition” (Wizards) : nécessite règles spéciales.

---

## 12) Checklist d’implémentation (ordre recommandé)

1) **Importer TCGdex** et figer l’identifiant interne `(set_id, local_id, variant)`
2) Ajouter le champ `ebay_query` + génération auto
3) Implémenter le worker eBay :
   - fetch -> normalize -> percentiles -> snapshot
4) Implémenter guardrail Cardmarket + fallback
5) Implémenter buy price formula + état (neuf/bon/correct)
6) Construire l’admin “anomalies + override query”
7) Lancer batch test sur 200–500 cartes, vérifier output
8) Déployer batch complet trimestriel + monitoring.

---

## 13) Pseudocode (résumé)

```pseudo
for card in cards_to_update:
  query = card.ebay_query_override ?? card.ebay_query
  ebay = fetch_ebay_active(query, limit=100)
  prices = normalize_prices(ebay.items)  # price+shipping, to EUR
  prices = trim_outliers(prices)
  stats = percentiles(prices, [20, 50, 80])
  dispersion = stats.p80 / stats.p20
  anchor = stats.p20
  source = "EBAY_ACTIVE"
  cm = max(card.cm_trend, card.cm_avg30) if available

  confidence = compute_confidence(stats.sample_size, dispersion, ebay.total, cm exists)
  if cm exists and (anchor > 2.5*cm or anchor < 0.4*cm or dispersion > 4.0):
      anchor = cm
      source = "CARDMARKET_FALLBACK"
      confidence -= penalty

  risk = compute_risk(dispersion, ebay.total, stats.sample_size, source)
  buy = anchor * (1 - fees_rate - margin_target - risk) - fixed_costs
  buy = clamp_and_round(buy)

  write market_snapshot(card_id, stats, dispersion, anchor, source, confidence)
  write buy_prices(card_id, buy*coef_neuf, buy*coef_bon, buy*coef_correct, confidence, source)
```

---

## 14) Paramètres à définir (à mettre dans un fichier config)

- `MIN_CARD_VALUE_EUR = 3.00`
- `EBAY_SAMPLE_LIMIT = 100`
- `EBAY_MIN_SAMPLE = 10`
- `MARGIN_TARGET = 0.27` (par défaut, test A/B 0.25–0.30)
- `FEES_RATE = 0.11` (à confirmer)
- `FIXED_COSTS_EUR = 0.30..3.00` (à estimer)
- `RISK_BASE = 0.02`
- `MISMATCH_UPPER = 2.5`
- `MISMATCH_LOWER = 0.4`
- `DISPERSION_BAD = 4.0`
- `COEF_BON = 0.85`
- `COEF_CORRECT = 0.70`
- `ROUNDING_STEP = 0.10` (ou 0.50)

---

## 15) Livrables attendus (pour Claude Code)

1) Scripts d’import TCGdex → `cards`
2) Générateur `ebay_query`
3) Worker eBay Browse API + percentiles + snapshots
4) Module guardrail Cardmarket + fallback
5) Moteur buy_price (marge/buffer/états)
6) Batch runner trimestriel + rapport + anomalies
7) Admin minimal override query (optionnel mais recommandé)

---

Fin.
