"""
Interface admin Flask pour gerer les prix et les overrides.
"""

from datetime import datetime
from pathlib import Path
import sqlite3

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
import csv
import io

from src.models import Card, BuyPrice, MarketSnapshot, BatchRun, BuyPriceStatus, AnchorSource, BatchMode, ApiUsage, SoldListing
from src.database import get_session, init_db
from src.config import get_config
from src.batch import BatchRunner
from src.batch.runner import request_stop as batch_request_stop
from src.batch.queue import get_queue
from src.ebay import EbayQueryBuilder
from src.ebay.usage_tracker import get_ebay_usage_summary
import threading

# Path to TCGdex database for series/set info
TCGDEX_DB_PATH = Path(__file__).parent.parent / "data" / "tcgdex_full.db"


def get_sets_grouped_by_series():
    """Récupère les sets groupés par série, triés par date (ancien -> récent)."""
    if not TCGDEX_DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(TCGDEX_DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Séries à exclure (pas de cartes physiques)
    EXCLUDED_SERIES = ['tcgp']  # Pokémon Pocket

    # Récupérer les séries triées par la plus ancienne date de set
    cursor.execute("""
        SELECT serie_id, serie_name, MIN(releasedate) as min_date
        FROM tcgdex_sets
        WHERE serie_id IS NOT NULL AND serie_name IS NOT NULL
          AND serie_id NOT IN ({})
        GROUP BY serie_id
        ORDER BY min_date
    """.format(','.join('?' * len(EXCLUDED_SERIES))), EXCLUDED_SERIES)
    series = cursor.fetchall()

    result = []
    for serie in series:
        # Récupérer les sets de cette série triés par date
        cursor.execute("""
            SELECT id, name, releasedate
            FROM tcgdex_sets
            WHERE serie_id = ?
            ORDER BY releasedate
        """, (serie['serie_id'],))
        sets = cursor.fetchall()

        result.append({
            'serie_id': serie['serie_id'],
            'serie_name': serie['serie_name'],
            'sets': [{'id': s['id'], 'name': s['name'], 'date': s['releasedate']} for s in sets]
        })

    conn.close()
    return result


def create_app() -> Flask:
    """Cree l'application Flask."""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = "prix-cartes-admin-secret"

    config = get_config()

    @app.route("/")
    def index():
        """Page d'accueil avec stats."""
        with get_session() as session:
            total_cards = session.query(Card).filter(Card.is_active == True).count()
            cards_with_price = session.query(BuyPrice).count()
            low_conf = session.query(BuyPrice).filter(BuyPrice.status == BuyPriceStatus.LOW_CONF).count()

            # Dernier batch
            last_batch = session.query(BatchRun).order_by(BatchRun.started_at.desc()).first()

            # Usage API eBay
            api_usage = get_ebay_usage_summary(session)

            return render_template("index.html",
                total_cards=total_cards,
                cards_with_price=cards_with_price,
                low_conf=low_conf,
                last_batch=last_batch,
                api_usage=api_usage,
            )

    @app.route("/cards")
    def cards_list():
        """Liste des cartes avec filtres."""
        from sqlalchemy import func
        from datetime import date, timedelta
        from dateutil.relativedelta import relativedelta

        page = request.args.get("page", 1, type=int)
        per_page = 50
        search = request.args.get("search", "")
        set_filter = request.args.get("set", "")
        has_data = request.args.get("has_data", "")

        # Filtres de date
        date_filter = request.args.get("date_filter", "")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        months_ago = request.args.get("months_ago", "3")

        # Filtre erreur
        has_error = request.args.get("has_error", "")

        with get_session() as session:
            # Subquery pour le dernier snapshot par carte
            latest_snapshot_id = session.query(
                MarketSnapshot.card_id,
                func.max(MarketSnapshot.id).label('max_id')
            ).group_by(MarketSnapshot.card_id).subquery()

            # Subquery pour le nombre de ventes par carte
            sold_count_subq = session.query(
                SoldListing.card_id,
                func.count(SoldListing.id).label('sold_count')
            ).group_by(SoldListing.card_id).subquery()

            query = session.query(
                Card,
                MarketSnapshot,
                sold_count_subq.c.sold_count
            ).outerjoin(
                latest_snapshot_id, Card.id == latest_snapshot_id.c.card_id
            ).outerjoin(
                MarketSnapshot, MarketSnapshot.id == latest_snapshot_id.c.max_id
            ).outerjoin(
                sold_count_subq, Card.id == sold_count_subq.c.card_id
            ).filter(Card.is_active == True)

            # Filtres
            if search:
                query = query.filter(
                    (Card.name.ilike(f"%{search}%")) |
                    (Card.set_name.ilike(f"%{search}%")) |
                    (Card.tcgdex_id.ilike(f"%{search}%"))
                )

            if set_filter:
                query = query.filter(Card.set_id == set_filter)

            if has_data == "yes":
                query = query.filter(MarketSnapshot.id != None)
            elif has_data == "no":
                query = query.filter(MarketSnapshot.id == None)

            # Filtres de date
            if date_filter == "range" and date_from and date_to:
                try:
                    d_from = date.fromisoformat(date_from)
                    d_to = date.fromisoformat(date_to)
                    query = query.filter(
                        MarketSnapshot.as_of_date >= d_from,
                        MarketSnapshot.as_of_date <= d_to
                    )
                except ValueError:
                    pass
            elif date_filter == "before" and date_to:
                try:
                    d_to = date.fromisoformat(date_to)
                    query = query.filter(MarketSnapshot.as_of_date < d_to)
                except ValueError:
                    pass
            elif date_filter == "months":
                try:
                    months = int(months_ago)
                    cutoff_date = date.today() - relativedelta(months=months)
                    query = query.filter(MarketSnapshot.as_of_date >= cutoff_date)
                except (ValueError, TypeError):
                    pass
            elif date_filter == "never":
                query = query.filter(MarketSnapshot.id == None)

            # Filtre erreur
            if has_error == "yes":
                query = query.filter(Card.last_error != None)
            elif has_error == "no":
                query = query.filter(Card.last_error == None)

            total = query.count()
            results = query.offset((page - 1) * per_page).limit(per_page).all()

            # Récupérer les séries/sets pour le filtre
            series_sets = get_sets_grouped_by_series()

            return render_template("cards.html",
                cards=results,
                page=page,
                per_page=per_page,
                total=total,
                search=search,
                set_filter=set_filter,
                has_data=has_data,
                date_filter=date_filter,
                date_from=date_from,
                date_to=date_to,
                months_ago=months_ago,
                has_error=has_error,
                series_sets=series_sets,
            )

    @app.route("/cards/<int:card_id>")
    def card_detail(card_id: int):
        """Detail d'une carte."""
        # Conserver les filtres de la liste pour le lien retour
        list_params = {
            'page': request.args.get('page', ''),
            'search': request.args.get('search', ''),
            'has_data': request.args.get('has_data', ''),
            'set': request.args.get('set', ''),
            'date_filter': request.args.get('date_filter', ''),
            'date_from': request.args.get('date_from', ''),
            'date_to': request.args.get('date_to', ''),
            'months_ago': request.args.get('months_ago', ''),
            'has_error': request.args.get('has_error', ''),
        }
        # Construire l'URL de retour avec les filtres
        back_params = '&'.join(f'{k}={v}' for k, v in list_params.items() if v)
        back_url = url_for('cards_list') + ('?' + back_params if back_params else '')

        with get_session() as session:
            card = session.query(Card).filter(Card.id == card_id).first()
            if not card:
                flash("Carte non trouvee", "error")
                return redirect(url_for("cards_list"))

            buy_price = session.query(BuyPrice).filter(BuyPrice.card_id == card_id).first()

            # Historique des snapshots
            snapshots = session.query(MarketSnapshot).filter(
                MarketSnapshot.card_id == card_id
            ).order_by(MarketSnapshot.as_of_date.desc()).limit(10).all()

            # Ventes detectees pour cette carte
            sold_listings_raw = session.query(SoldListing).filter(
                SoldListing.card_id == card_id
            ).order_by(SoldListing.detected_sold_at.desc()).limit(50).all()

            # Calculer les stats des ventes et la duree pour chaque vente
            sold_stats = None
            sold_listings = []
            if sold_listings_raw:
                import numpy as np
                from datetime import datetime as dt

                prices = [s.effective_price for s in sold_listings_raw if s.effective_price]

                # Calculer la duree pour chaque vente
                durations = []
                for s in sold_listings_raw:
                    duration_days = None
                    if s.listing_date and s.detected_sold_at:
                        try:
                            # listing_date peut etre string ou datetime
                            if isinstance(s.listing_date, str):
                                listing_dt = dt.fromisoformat(s.listing_date.replace('Z', '+00:00'))
                            else:
                                listing_dt = s.listing_date
                            # Rendre offset-naive si necessaire
                            detected_dt = s.detected_sold_at
                            if listing_dt.tzinfo is not None:
                                listing_dt = listing_dt.replace(tzinfo=None)
                            duration_days = (detected_dt - listing_dt).days
                            if duration_days >= 0:
                                durations.append(duration_days)
                            else:
                                duration_days = None
                        except (ValueError, TypeError):
                            pass
                    # Creer un dict avec les donnees + duration_days
                    sold_listings.append({
                        'id': s.id,
                        'title': s.title,
                        'price': s.price,
                        'effective_price': s.effective_price,
                        'image_url': s.image_url,
                        'url': s.url,
                        'seller': s.seller,
                        'condition': s.condition,
                        'is_reverse': s.is_reverse,
                        'detected_sold_at': s.detected_sold_at,
                        'listing_date': s.listing_date,
                        'duration_days': duration_days,
                    })

                if prices:
                    prices_arr = np.array(prices)
                    sold_stats = {
                        'count': len(prices),
                        'p10': float(np.percentile(prices_arr, 10)),
                        'p20': float(np.percentile(prices_arr, 20)),
                        'p50': float(np.percentile(prices_arr, 50)),
                        'p80': float(np.percentile(prices_arr, 80)),
                        'p90': float(np.percentile(prices_arr, 90)),
                    }

                    # Dispersion et CV
                    if sold_stats['p20'] > 0:
                        sold_stats['dispersion'] = sold_stats['p80'] / sold_stats['p20']
                    mean = float(np.mean(prices_arr))
                    std = float(np.std(prices_arr))
                    if mean > 0:
                        sold_stats['cv'] = std / mean

                    # Duree moyenne de vente
                    if durations:
                        sold_stats['avg_duration_days'] = float(np.mean(durations))
                        sold_stats['median_duration_days'] = float(np.median(durations))

            return render_template("card_detail.html",
                card=card,
                buy_price=buy_price,
                snapshots=snapshots,
                sold_listings=sold_listings,
                sold_stats=sold_stats,
                back_url=back_url,
                list_params=list_params,
            )

    @app.route("/cards/<int:card_id>/edit", methods=["GET", "POST"])
    def card_edit(card_id: int):
        """Editer une carte (override query)."""
        with get_session() as session:
            card = session.query(Card).filter(Card.id == card_id).first()
            if not card:
                flash("Carte non trouvee", "error")
                return redirect(url_for("cards_list"))

            if request.method == "POST":
                # Sauvegarder l'override
                override = request.form.get("ebay_query_override", "").strip()
                if override:
                    card.ebay_query_override = override
                else:
                    card.ebay_query_override = None

                card.updated_at = datetime.utcnow()
                session.commit()

                flash("Query override sauvegardee", "success")
                return redirect(url_for("card_detail", card_id=card_id))

            # Generer une suggestion de query
            builder = EbayQueryBuilder()
            suggested_query = builder.build_query(card)

            return render_template("card_edit.html",
                card=card,
                suggested_query=suggested_query,
            )

    @app.route("/cards/<int:card_id>/reprocess", methods=["POST"])
    def card_reprocess(card_id: int):
        """Retraiter une carte."""
        runner = BatchRunner()
        success = runner.reprocess_card(card_id)

        if success:
            flash("Carte retraitee avec succes", "success")
        else:
            flash("Erreur lors du retraitement", "error")

        return redirect(url_for("card_detail", card_id=card_id))

    @app.route("/anomalies")
    def anomalies():
        """Liste des cartes avec anomalies."""
        dispersion_threshold = request.args.get("dispersion", 4.0, type=float)
        confidence_threshold = request.args.get("confidence", 50, type=int)

        with get_session() as session:
            # High dispersion
            high_dispersion = session.query(Card, BuyPrice, MarketSnapshot).join(
                BuyPrice, Card.id == BuyPrice.card_id
            ).join(
                MarketSnapshot,
                (Card.id == MarketSnapshot.card_id) &
                (MarketSnapshot.as_of_date == BuyPrice.as_of_date)
            ).filter(
                Card.is_active == True,
                MarketSnapshot.dispersion > dispersion_threshold
            ).order_by(MarketSnapshot.dispersion.desc()).limit(50).all()

            # Low confidence
            low_conf = session.query(Card, BuyPrice).join(
                BuyPrice, Card.id == BuyPrice.card_id
            ).filter(
                Card.is_active == True,
                BuyPrice.confidence_score < confidence_threshold
            ).order_by(BuyPrice.confidence_score).limit(50).all()

            # Mismatches (fallback CM)
            mismatches = session.query(Card, BuyPrice).join(
                BuyPrice, Card.id == BuyPrice.card_id
            ).filter(
                Card.is_active == True,
                BuyPrice.anchor_source == AnchorSource.CARDMARKET_FALLBACK
            ).limit(50).all()

            return render_template("anomalies.html",
                high_dispersion=high_dispersion,
                low_conf=low_conf,
                mismatches=mismatches,
                dispersion_threshold=dispersion_threshold,
                confidence_threshold=confidence_threshold,
            )

    @app.route("/batches")
    def batches():
        """Historique des batches."""
        from sqlalchemy import func

        with get_session() as session:
            runs = session.query(BatchRun).order_by(BatchRun.started_at.desc()).limit(20).all()

            # Pour chaque batch, récupérer les sets traités
            batch_details = {}
            for batch in runs:
                if batch.finished_at:
                    # Récupérer les snapshots créés pendant ce batch
                    set_stats = (
                        session.query(
                            Card.set_id,
                            Card.set_name,
                            func.count(MarketSnapshot.id).label("count")
                        )
                        .join(Card, MarketSnapshot.card_id == Card.id)
                        .filter(
                            MarketSnapshot.created_at >= batch.started_at,
                            MarketSnapshot.created_at <= batch.finished_at
                        )
                        .group_by(Card.set_id, Card.set_name)
                        .order_by(func.count(MarketSnapshot.id).desc())
                        .all()
                    )
                    batch_details[batch.id] = [
                        {"set_id": s.set_id, "set_name": s.set_name, "count": s.count}
                        for s in set_stats
                    ]
                else:
                    batch_details[batch.id] = []

            return render_template("batches.html", batches=runs, batch_details=batch_details)

    @app.route("/batch")
    def batch_launcher():
        """Page de lancement de batch par serie."""
        from sqlalchemy import func

        with get_session() as session:
            # Compter les cartes par set_id
            card_counts = dict(
                session.query(Card.set_id, func.count(Card.id))
                .filter(Card.is_active == True)
                .group_by(Card.set_id)
                .all()
            )

            # Compter les cartes avec donnees eBay (MarketSnapshot) par set_id
            price_counts = dict(
                session.query(Card.set_id, func.count(func.distinct(Card.id)))
                .join(MarketSnapshot, Card.id == MarketSnapshot.card_id)
                .filter(Card.is_active == True)
                .group_by(Card.set_id)
                .all()
            )

            # Date du dernier snapshot par set_id
            last_snapshot_dates = dict(
                session.query(Card.set_id, func.max(MarketSnapshot.created_at))
                .join(MarketSnapshot, Card.id == MarketSnapshot.card_id)
                .filter(Card.is_active == True)
                .group_by(Card.set_id)
                .all()
            )

            # Compter les cartes avec erreur par set_id
            error_counts = dict(
                session.query(Card.set_id, func.count(Card.id))
                .filter(Card.is_active == True, Card.last_error != None)
                .group_by(Card.set_id)
                .all()
            )

            # Recuperer les series/sets
            series_sets = get_sets_grouped_by_series()

            # Enrichir avec les stats
            for serie in series_sets:
                for s in serie['sets']:
                    s['card_count'] = card_counts.get(s['id'], 0)
                    s['price_count'] = price_counts.get(s['id'], 0)
                    s['error_count'] = error_counts.get(s['id'], 0)
                    s['last_snapshot'] = last_snapshot_dates.get(s['id'])

            # Verifier si un batch est en cours
            running_batch = session.query(BatchRun).filter(
                BatchRun.finished_at == None
            ).first()

            return render_template("batch.html",
                series_sets=series_sets,
                running_batch=running_batch,
            )

    @app.route("/api/batch/run", methods=["POST"])
    def api_batch_run():
        """API: Ajouter un ou plusieurs sets a la queue."""
        data = request.get_json() or {}

        # Support pour un seul set ou plusieurs
        sets = data.get("sets", [])
        if not sets:
            # Retrocompatibilite avec l'ancien format
            set_id = data.get("set_id")
            set_name = data.get("set_name", set_id)
            if set_id:
                sets = [{"set_id": set_id, "set_name": set_name}]

        if not sets:
            return jsonify({"error": "sets ou set_id requis"}), 400

        # Nombre de workers paralleles (1-10)
        max_workers = data.get("max_workers", 1)
        max_workers = max(1, min(int(max_workers), 10))

        queue = get_queue()
        items = queue.add_multiple(sets, max_workers=max_workers)

        return jsonify({
            "success": True,
            "message": f"{len(items)} set(s) ajoute(s) a la queue ({max_workers} workers)",
            "queue_status": queue.get_status(),
        })

    @app.route("/api/batch/estimate", methods=["POST"])
    def api_batch_estimate():
        """API: Estimer le nombre d'appels API pour une liste de sets."""
        from sqlalchemy import func

        data = request.get_json() or {}
        set_ids = data.get("set_ids", [])

        if not set_ids:
            return jsonify({"error": "set_ids requis"}), 400

        with get_session() as session:
            # Compter les cartes par set
            card_counts = dict(
                session.query(Card.set_id, func.count(Card.id))
                .filter(Card.is_active == True, Card.set_id.in_(set_ids))
                .group_by(Card.set_id)
                .all()
            )

            # Total cartes
            total_cards = sum(card_counts.values())

            # Estimation: ~1 appel API par carte
            estimated_calls = total_cards

            # Recuperer l'usage actuel
            usage = get_ebay_usage_summary(session)
            remaining = usage.get("remaining", 5000)

            return jsonify({
                "set_count": len(set_ids),
                "total_cards": total_cards,
                "estimated_calls": estimated_calls,
                "today_usage": usage.get("today_count", 0),
                "daily_limit": usage.get("daily_limit", 5000),
                "remaining": remaining,
                "will_exceed": estimated_calls > remaining,
            })

    @app.route("/api/batch/status")
    def api_batch_status():
        """API: Statut de la queue de batchs."""
        queue = get_queue()
        return jsonify(queue.get_status())

    @app.route("/api/batch/set-stats")
    def api_batch_set_stats():
        """API: Retourne les compteurs cartes/donnees eBay par set."""
        from sqlalchemy import func

        with get_session() as session:
            # Compter les cartes par set_id
            card_counts = dict(
                session.query(Card.set_id, func.count(Card.id))
                .filter(Card.is_active == True)
                .group_by(Card.set_id)
                .all()
            )

            # Compter les cartes avec donnees eBay (MarketSnapshot) par set_id
            data_counts = dict(
                session.query(Card.set_id, func.count(func.distinct(Card.id)))
                .join(MarketSnapshot, Card.id == MarketSnapshot.card_id)
                .filter(Card.is_active == True)
                .group_by(Card.set_id)
                .all()
            )

            # Date du dernier snapshot par set_id
            last_snapshot_dates = dict(
                session.query(Card.set_id, func.max(MarketSnapshot.created_at))
                .join(MarketSnapshot, Card.id == MarketSnapshot.card_id)
                .filter(Card.is_active == True)
                .group_by(Card.set_id)
                .all()
            )

            # Compter les cartes avec erreur par set_id
            error_counts = dict(
                session.query(Card.set_id, func.count(Card.id))
                .filter(Card.is_active == True, Card.last_error != None)
                .group_by(Card.set_id)
                .all()
            )

            # Construire la reponse
            stats = {}
            for set_id, card_count in card_counts.items():
                data_count = data_counts.get(set_id, 0)
                error_count = error_counts.get(set_id, 0)
                pct = round(data_count / card_count * 100) if card_count > 0 else 0
                error_pct = round(error_count / card_count * 100) if card_count > 0 else 0
                last_date = last_snapshot_dates.get(set_id)
                stats[set_id] = {
                    "card_count": card_count,
                    "price_count": data_count,
                    "error_count": error_count,
                    "pct": pct,
                    "error_pct": error_pct,
                    "last_snapshot": last_date.strftime('%d/%m/%y') if last_date else None,
                }

            return jsonify(stats)

    @app.route("/api/batch/stop", methods=["POST"])
    def api_batch_stop():
        """API: Arreter la queue de batchs."""
        queue = get_queue()
        queue.stop()

        return jsonify({
            "success": True,
            "message": "Arret demande",
            "queue_status": queue.get_status(),
        })

    @app.route("/api/batch/priority-sets")
    def api_batch_priority_sets():
        """API: Retourne les sets prioritaires a lancer (non lances ou les plus anciens)."""
        from sqlalchemy import func

        limit = request.args.get("limit", 10, type=int)

        with get_session() as session:
            # Recuperer la date du dernier snapshot par set_id
            last_snapshot_subq = (
                session.query(
                    Card.set_id,
                    func.max(MarketSnapshot.created_at).label("last_updated")
                )
                .join(MarketSnapshot, Card.id == MarketSnapshot.card_id)
                .group_by(Card.set_id)
                .subquery()
            )

            # Recuperer tous les sets actifs avec leur date de dernier snapshot
            all_sets = (
                session.query(
                    Card.set_id,
                    Card.set_name,
                    func.count(Card.id).label("card_count"),
                    last_snapshot_subq.c.last_updated
                )
                .outerjoin(last_snapshot_subq, Card.set_id == last_snapshot_subq.c.set_id)
                .filter(Card.is_active == True)
                .group_by(Card.set_id, Card.set_name, last_snapshot_subq.c.last_updated)
                .all()
            )

            # Trier: d'abord ceux sans date (jamais lances), puis par date croissante
            priority_sets = sorted(
                all_sets,
                key=lambda x: (x.last_updated is not None, x.last_updated or datetime.min)
            )[:limit]

            return jsonify({
                "sets": [
                    {
                        "set_id": s.set_id,
                        "set_name": s.set_name,
                        "card_count": s.card_count,
                        "last_updated": s.last_updated.isoformat() if s.last_updated else None,
                    }
                    for s in priority_sets
                ]
            })

    @app.route("/api/usage/ebay")
    def api_ebay_usage():
        """API: Usage quotidien de l'API eBay."""
        with get_session() as session:
            summary = get_ebay_usage_summary(session)
            return jsonify(summary)

    @app.route("/api/usage/ebay/refresh", methods=["POST"])
    def api_ebay_usage_refresh():
        """API: Rafraichir les rate limits depuis eBay."""
        from src.ebay.usage_tracker import refresh_rate_limits_from_ebay
        rate_limits = refresh_rate_limits_from_ebay()
        if rate_limits:
            return jsonify({"success": True, **rate_limits})
        return jsonify({"success": False, "error": "Impossible de recuperer les rate limits"}), 500

    @app.route("/api/cards/<int:card_id>/update-info", methods=["POST"])
    def api_card_update_info(card_id: int):
        """API: Mettre a jour les overrides d'informations d'une carte."""
        data = request.get_json() or {}

        with get_session() as session:
            card = session.query(Card).filter(Card.id == card_id).first()
            if not card:
                return jsonify({"success": False, "error": "Carte non trouvee"}), 404

            # Mettre a jour les overrides (null = pas d'override)
            card.name_override = data.get("name_override") or None
            card.local_id_override = data.get("local_id_override") or None
            card.set_name_override = data.get("set_name_override") or None
            card.updated_at = datetime.utcnow()

            # Regenerer la requete eBay avec les nouvelles valeurs
            builder = EbayQueryBuilder()
            new_query = builder.build_query(card)
            card.ebay_query = new_query

            session.commit()

            return jsonify({
                "success": True,
                "message": "Overrides mis a jour et requete eBay regeneree",
                "card": {
                    "id": card.id,
                    "name_override": card.name_override,
                    "local_id_override": card.local_id_override,
                    "set_name_override": card.set_name_override,
                    "effective_name": card.effective_name,
                    "effective_local_id": card.effective_local_id,
                    "effective_set_name": card.effective_set_name,
                    "ebay_query": card.ebay_query,
                }
            })

    @app.route("/api/cards/regenerate-queries", methods=["POST"])
    def api_regenerate_all_queries():
        """API: Regenerer toutes les requetes eBay pour toutes les cartes actives."""
        with get_session() as session:
            builder = EbayQueryBuilder()

            # Recuperer toutes les cartes actives sans override de requete
            cards = session.query(Card).filter(
                Card.is_active == True,
                Card.ebay_query_override == None
            ).all()

            count = 0
            for card in cards:
                new_query = builder.build_query(card)
                if card.ebay_query != new_query:
                    card.ebay_query = new_query
                    count += 1

            session.commit()

            return jsonify({
                "success": True,
                "message": f"{count} requetes eBay regenerees",
                "total_cards": len(cards),
                "updated": count
            })

    @app.route("/api/cards/<int:card_id>")
    def api_card(card_id: int):
        """API: detail carte en JSON."""
        with get_session() as session:
            card = session.query(Card).filter(Card.id == card_id).first()
            if not card:
                return jsonify({"error": "Not found"}), 404

            buy_price = session.query(BuyPrice).filter(BuyPrice.card_id == card_id).first()

            return jsonify({
                "id": card.id,
                "tcgdex_id": card.tcgdex_id,
                "name": card.name,
                "set_name": card.set_name,
                "variant": card.variant.value if card.variant else None,
                "ebay_query": card.effective_ebay_query,
                "has_override": bool(card.ebay_query_override),
                "cm_trend": card.cm_trend,
                "cm_avg30": card.cm_avg30,
                "buy_price": {
                    "neuf": buy_price.buy_neuf,
                    "bon": buy_price.buy_bon,
                    "correct": buy_price.buy_correct,
                    "confidence": buy_price.confidence_score,
                    "status": buy_price.status.value if buy_price.status else None,
                } if buy_price else None,
            })

    @app.route("/api/cards/<int:card_id>/listings")
    def api_card_listings(card_id: int):
        """API: annonces eBay pour une carte."""
        refresh = request.args.get("refresh", "false") == "true"

        with get_session() as session:
            card = session.query(Card).filter(Card.id == card_id).first()
            if not card:
                return jsonify({"error": "Not found"}), 404

            if refresh:
                # Collecter en direct depuis eBay
                from src.ebay import EbayWorker
                worker = EbayWorker()
                result = worker.collect_for_card(card)

                if not result.success:
                    return jsonify({
                        "success": False,
                        "error": result.error,
                        "query": result.query_used,
                        "listings": []
                    })

                listings = [
                    {
                        "title": item.title,
                        "price": item.effective_price,
                        "currency": item.currency,
                        "url": item.item_web_url,
                        "condition": item.condition,
                        "seller": item.seller_username,
                        "image": item.image_url,
                        "listing_date": item.listing_date,
                    }
                    for item in result.items[:50]
                ]

                # Annonces reverse
                reverse_listings = [
                    {
                        "title": item.title,
                        "price": item.price,
                        "shipping": item.shipping_cost,
                        "currency": item.currency,
                        "url": item.item_web_url,
                        "condition": item.condition,
                        "seller": item.seller_username,
                        "image": item.image_url,
                        "listing_date": item.listing_date,
                    }
                    for item in result.reverse_items[:50]
                ] if result.reverse_items else []

                return jsonify({
                    "success": True,
                    "query": result.query_used,
                    "total": result.active_count,
                    "listings": listings,
                    "reverse_listings": reverse_listings,
                    "stats": {
                        "p10": result.stats.p10 if result.stats else None,
                        "p20": result.stats.p20 if result.stats else None,
                        "p50": result.stats.p50 if result.stats else None,
                        "p80": result.stats.p80 if result.stats else None,
                        "p90": result.stats.p90 if result.stats else None,
                        "dispersion": result.stats.dispersion if result.stats else None,
                        "cv": result.stats.cv if result.stats else None,
                        "age_median_days": result.stats.age_median_days if result.stats else None,
                        "pct_recent_7d": result.stats.pct_recent_7d if result.stats else None,
                        "consensus_score": result.stats.consensus_score if result.stats else None,
                    }
                })
            else:
                # Recuperer depuis le dernier snapshot
                snapshot = session.query(MarketSnapshot).filter(
                    MarketSnapshot.card_id == card_id
                ).order_by(MarketSnapshot.as_of_date.desc()).first()

                if not snapshot:
                    return jsonify({
                        "success": False,
                        "error": "Aucun snapshot disponible",
                        "listings": []
                    })

                meta = snapshot.get_raw_meta()
                listings = meta.get("listings", [])
                reverse_listings = meta.get("reverse_listings", [])

                return jsonify({
                    "success": True,
                    "query": meta.get("query"),
                    "snapshot_date": str(snapshot.as_of_date),
                    "total": snapshot.active_count,
                    "listings": listings,
                    "reverse_listings": reverse_listings,
                    "stats": {
                        "p10": snapshot.p10,
                        "p20": snapshot.p20,
                        "p50": snapshot.p50,
                        "p80": snapshot.p80,
                        "p90": snapshot.p90,
                        "dispersion": snapshot.dispersion,
                        "cv": snapshot.cv,
                        "age_median_days": snapshot.age_median_days,
                        "pct_recent_7d": snapshot.pct_recent_7d,
                        "consensus_score": snapshot.consensus_score,
                        "anchor": snapshot.anchor_price,
                    }
                })

    @app.route("/export/csv")
    def export_csv():
        """Export CSV de toutes les cartes avec statistiques eBay et ventes."""
        from sqlalchemy import func
        from collections import defaultdict
        from datetime import timedelta
        import numpy as np

        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)

        # Header - stats eBay + ventes
        # Note: percentiles affiches seulement si >= 10 elements (sinon vide = stats non fiables)
        writer.writerow([
            'ID',
            'TCGdex ID',
            'Nom',
            'Set',
            'Numéro',
            'Variant',
            # Stats annonces en cours (min/max/moy toujours, percentiles si >= 10)
            'Nb Annonces',
            'Min',
            'Max',
            'Moy',
            'p10',
            'p20',
            'p50',
            'p80',
            'p90',
            'Dispersion',
            'CV',
            'Age Median (j)',
            '% Recentes 7j',
            'Consensus %',
            'Date Snapshot',
            # Stats ventes (min/max/moy toujours, percentiles si >= 10)
            'Ventes Nb',
            'Ventes Min',
            'Ventes Max',
            'Ventes Moy',
            'Ventes p10',
            'Ventes p20',
            'Ventes p50',
            'Ventes p80',
            'Ventes p90',
            'Ventes Dispersion',
            'Ventes CV',
            'Ventes % 7j',
            'Derniere Vente',
        ])

        with get_session() as session:
            now = datetime.utcnow()
            seven_days_ago = now - timedelta(days=7)

            # Precharger les stats de ventes par card_id
            sales_by_card = defaultdict(lambda: {
                "prices": [],
                "dates": [],
                "last_date": None
            })

            for sold in session.query(SoldListing).all():
                s = sales_by_card[sold.card_id]
                price = sold.effective_price or 0
                s["prices"].append(price)
                if sold.detected_sold_at:
                    s["dates"].append(sold.detected_sold_at)
                if s["last_date"] is None or (sold.detected_sold_at and sold.detected_sold_at > s["last_date"]):
                    s["last_date"] = sold.detected_sold_at

            # Subquery pour l'ID du snapshot le plus récent par carte
            latest_snapshot_id = session.query(
                MarketSnapshot.card_id,
                func.max(MarketSnapshot.id).label('max_id')
            ).group_by(MarketSnapshot.card_id).subquery()

            # Query avec jointure - utilise l'ID pour éviter les duplications
            results = session.query(Card, MarketSnapshot).outerjoin(
                latest_snapshot_id, Card.id == latest_snapshot_id.c.card_id
            ).outerjoin(
                MarketSnapshot,
                MarketSnapshot.id == latest_snapshot_id.c.max_id
            ).filter(Card.is_active == True).order_by(Card.set_name, Card.local_id).all()

            for card, snapshot in results:
                # Stats ventes pour cette carte
                s = sales_by_card.get(card.id, {"prices": [], "dates": [], "last_date": None})
                prices = s["prices"]
                dates = s["dates"]

                # Calculer les stats des ventes
                v_count = len(prices)
                v_min = v_max = v_moy = ''
                v_p10 = v_p20 = v_p50 = v_p80 = v_p90 = v_disp = v_cv = v_pct_7d = ''
                if prices:
                    prices_arr = np.array(prices)
                    # Min/max/moy toujours affiches
                    v_min = f"{min(prices):.2f}"
                    v_max = f"{max(prices):.2f}"
                    v_moy = f"{np.mean(prices_arr):.2f}"
                    # Percentiles seulement si >= 10 elements (stats fiables)
                    if v_count >= 10:
                        v_p10 = f"{np.percentile(prices_arr, 10):.2f}"
                        v_p20 = f"{np.percentile(prices_arr, 20):.2f}"
                        v_p50 = f"{np.percentile(prices_arr, 50):.2f}"
                        v_p80 = f"{np.percentile(prices_arr, 80):.2f}"
                        v_p90 = f"{np.percentile(prices_arr, 90):.2f}"
                        p20_val = np.percentile(prices_arr, 20)
                        p80_val = np.percentile(prices_arr, 80)
                        if p20_val > 0:
                            v_disp = f"{p80_val / p20_val:.2f}"
                        mean = np.mean(prices_arr)
                        std = np.std(prices_arr)
                        if mean > 0:
                            v_cv = f"{std / mean:.2f}"
                    # % ventes sur 7 derniers jours (toujours affiche)
                    if dates:
                        recent_count = sum(1 for d in dates if d >= seven_days_ago)
                        v_pct_7d = f"{recent_count / len(dates) * 100:.0f}"

                # Stats annonces: min/max/moy depuis le snapshot meta si dispo
                a_count = snapshot.active_count if snapshot else 0
                a_min = a_max = a_moy = ''
                a_p10 = a_p20 = a_p50 = a_p80 = a_p90 = a_disp = a_cv = ''
                if snapshot:
                    meta = snapshot.get_raw_meta() if hasattr(snapshot, 'get_raw_meta') else {}
                    listings = meta.get("listings", [])
                    if listings:
                        listing_prices = [l.get("price", 0) for l in listings if l.get("price")]
                        if listing_prices:
                            a_min = f"{min(listing_prices):.2f}"
                            a_max = f"{max(listing_prices):.2f}"
                            a_moy = f"{sum(listing_prices) / len(listing_prices):.2f}"
                    # Percentiles seulement si >= 10 annonces
                    if a_count and a_count >= 10:
                        if snapshot.p10:
                            a_p10 = f"{snapshot.p10:.2f}"
                        if snapshot.p20:
                            a_p20 = f"{snapshot.p20:.2f}"
                        if snapshot.p50:
                            a_p50 = f"{snapshot.p50:.2f}"
                        if snapshot.p80:
                            a_p80 = f"{snapshot.p80:.2f}"
                        if snapshot.p90:
                            a_p90 = f"{snapshot.p90:.2f}"
                        if snapshot.dispersion:
                            a_disp = f"{snapshot.dispersion:.2f}"
                        if snapshot.cv:
                            a_cv = f"{snapshot.cv:.2f}"

                writer.writerow([
                    card.id,
                    card.tcgdex_id,
                    card.effective_name,
                    card.effective_set_name,
                    card.effective_local_id,
                    card.variant.value if card.variant else 'NORMAL',
                    # Stats annonces
                    a_count if a_count else '',
                    a_min,
                    a_max,
                    a_moy,
                    a_p10,
                    a_p20,
                    a_p50,
                    a_p80,
                    a_p90,
                    a_disp,
                    a_cv,
                    f"{snapshot.age_median_days:.1f}" if snapshot and snapshot.age_median_days else '',
                    f"{snapshot.pct_recent_7d:.0f}" if snapshot and snapshot.pct_recent_7d else '',
                    f"{snapshot.consensus_score:.0f}" if snapshot and snapshot.consensus_score else '',
                    str(snapshot.as_of_date) if snapshot else '',
                    # Stats ventes
                    v_count if v_count > 0 else '',
                    v_min,
                    v_max,
                    v_moy,
                    v_p10,
                    v_p20,
                    v_p50,
                    v_p80,
                    v_p90,
                    v_disp,
                    v_cv,
                    v_pct_7d,
                    s["last_date"].strftime('%Y-%m-%d') if s["last_date"] else '',
                ])

        output.seek(0)
        # UTF-8 avec BOM pour Excel
        csv_content = '\ufeff' + output.getvalue()
        return Response(
            csv_content,
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition': f'attachment; filename=prix_cartes_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            }
        )

    # ===================
    # VENTES DETECTEES
    # ===================

    @app.route("/ventes")
    def sold_listings():
        """Liste des annonces disparues (probablement vendues)."""
        from sqlalchemy import func
        from datetime import datetime, timedelta

        page = request.args.get('page', 1, type=int)
        per_page = 50

        # Filtres de date
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')
        period = request.args.get('period', '')  # 7d, 30d, 90d

        with get_session() as session:
            # Query de base
            query = session.query(SoldListing, Card).join(
                Card, SoldListing.card_id == Card.id
            )

            # Appliquer les filtres de date
            if period:
                days = {'7d': 7, '30d': 30, '90d': 90}.get(period, 0)
                if days:
                    date_limit = datetime.utcnow() - timedelta(days=days)
                    query = query.filter(SoldListing.detected_sold_at >= date_limit)
            else:
                if date_from:
                    try:
                        dt_from = datetime.strptime(date_from, '%Y-%m-%d')
                        query = query.filter(SoldListing.detected_sold_at >= dt_from)
                    except ValueError:
                        pass
                if date_to:
                    try:
                        dt_to = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                        query = query.filter(SoldListing.detected_sold_at < dt_to)
                    except ValueError:
                        pass

            # Total count (avec filtres)
            total = query.count()

            # Get paginated results
            listings = query.order_by(
                SoldListing.detected_sold_at.desc()
            ).offset((page - 1) * per_page).limit(per_page).all()

            # Stats (sur les resultats filtres)
            stats_query = session.query(SoldListing)
            if period:
                days = {'7d': 7, '30d': 30, '90d': 90}.get(period, 0)
                if days:
                    date_limit = datetime.utcnow() - timedelta(days=days)
                    stats_query = stats_query.filter(SoldListing.detected_sold_at >= date_limit)
            else:
                if date_from:
                    try:
                        dt_from = datetime.strptime(date_from, '%Y-%m-%d')
                        stats_query = stats_query.filter(SoldListing.detected_sold_at >= dt_from)
                    except ValueError:
                        pass
                if date_to:
                    try:
                        dt_to = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                        stats_query = stats_query.filter(SoldListing.detected_sold_at < dt_to)
                    except ValueError:
                        pass

            stats = {
                'total': total,
                'total_value': stats_query.with_entities(
                    func.sum(SoldListing.effective_price)
                ).scalar() or 0
            }

            return render_template(
                "ventes.html",
                listings=listings,
                stats=stats,
                page=page,
                per_page=per_page,
                total=total,
                total_pages=(total + per_page - 1) // per_page,
                date_from=date_from,
                date_to=date_to,
                period=period,
            )

    return app


def run_admin():
    """Lance le serveur admin."""
    config = get_config()
    app = create_app()
    app.run(host=config.admin_host, port=config.admin_port, debug=True)


if __name__ == "__main__":
    run_admin()
