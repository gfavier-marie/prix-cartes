"""
Interface admin Flask pour gerer les prix et les overrides.
"""

from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
import csv
import io

from src.models import Card, BuyPrice, MarketSnapshot, BatchRun, BuyPriceStatus, AnchorSource, BatchMode, ApiUsage, SoldListing, Set, CardNumberFormat, Settings
from src.database import get_session, init_db
from src.config import get_config
from src.batch import BatchRunner
from src.tcgdex.client import TCGdexClient
from src.tcgdex.importer import TCGdexImporter
from src.batch.runner import request_stop as batch_request_stop
from src.batch.queue import get_queue
from src.ebay import EbayQueryBuilder
from src.ebay.usage_tracker import get_ebay_usage_summary
import threading

def get_sets_grouped_by_series():
    """Récupère les sets groupés par série, triés par date (ancien -> récent).

    Utilise la table sets de pricing.db et applique le filtre excluded_series et excluded_sets.
    """
    config = get_config()
    excluded_series = config.tcgdex.excluded_series
    excluded_sets = config.tcgdex.excluded_sets

    with get_session() as session:
        # Récupérer tous les sets, filtrer les séries et sets exclus
        query = session.query(Set)
        if excluded_series:
            query = query.filter(~Set.serie_id.in_(excluded_series))
        if excluded_sets:
            query = query.filter(~Set.id.in_(excluded_sets))
        sets = query.order_by(Set.release_date).all()

        if not sets:
            return []

        # Grouper par série
        series_dict = {}
        for s in sets:
            if s.serie_id not in series_dict:
                series_dict[s.serie_id] = {
                    'serie_id': s.serie_id,
                    'serie_name': s.serie_name,
                    'sets': [],
                    'min_date': s.release_date
                }
            series_dict[s.serie_id]['sets'].append({
                'id': s.id,
                'name': s.name,
                'date': str(s.release_date) if s.release_date else None
            })

        # Trier les séries par date la plus ancienne
        result = sorted(series_dict.values(), key=lambda x: x['min_date'] or '')

        # Retirer min_date du résultat final
        for serie in result:
            del serie['min_date']

        return result


def create_app() -> Flask:
    """Cree l'application Flask."""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    config = get_config()
    app.secret_key = config.flask_secret_key

    @app.route("/")
    def index():
        """Page d'accueil avec stats."""
        from src.ebay.usage_tracker import refresh_rate_limits_from_ebay
        from datetime import datetime

        with get_session() as session:
            total_cards = session.query(Card).filter(Card.is_active == True).count()
            cards_with_price = session.query(BuyPrice).count()
            low_conf = session.query(BuyPrice).filter(BuyPrice.status == BuyPriceStatus.LOW_CONF).count()

            # Dernier batch
            last_batch = session.query(BatchRun).order_by(BatchRun.started_at.desc()).first()

            # Usage API eBay - appel reel
            ebay_usage = refresh_rate_limits_from_ebay()
            if ebay_usage:
                # Formatter le reset
                if ebay_usage.get("reset"):
                    try:
                        reset_dt = datetime.fromisoformat(ebay_usage["reset"].replace("Z", "+00:00"))
                        ebay_usage["reset_formatted"] = reset_dt.strftime("%d/%m %H:%M")
                    except Exception:
                        ebay_usage["reset_formatted"] = ebay_usage["reset"]
                # Calculer le pourcentage
                if ebay_usage.get("limit") and ebay_usage["limit"] > 0:
                    ebay_usage["percent"] = int((ebay_usage.get("count", 0) / ebay_usage["limit"]) * 100)
                else:
                    ebay_usage["percent"] = 0

            return render_template("index.html",
                total_cards=total_cards,
                cards_with_price=cards_with_price,
                low_conf=low_conf,
                last_batch=last_batch,
                ebay_usage=ebay_usage,
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
        error_hours = request.args.get("error_hours", "")

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
                # Filtre sur la date de l'erreur
                if error_hours:
                    try:
                        hours = int(error_hours)
                        from datetime import datetime, timedelta
                        error_since = datetime.utcnow() - timedelta(hours=hours)
                        query = query.filter(Card.last_error_at >= error_since)
                    except ValueError:
                        pass
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
                error_hours=error_hours,
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
            'error_hours': request.args.get('error_hours', ''),
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
                back_params=back_params,
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
        # Conserver les filtres de la liste
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
            'error_hours': request.args.get('error_hours', ''),
        }

        runner = BatchRunner()
        success = runner.reprocess_card(card_id)

        if success:
            flash("Carte retraitee avec succes", "success")
        else:
            flash("Erreur lors du retraitement", "error")

        # Rediriger avec les filtres preserves
        return redirect(url_for("card_detail", card_id=card_id, **{k: v for k, v in list_params.items() if v}))

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
            card.card_count_official_override = data.get("card_count_official_override") or None

            # card_number_format: convertir string en enum ou None
            card_number_format_str = data.get("card_number_format")
            if card_number_format_str:
                try:
                    card.card_number_format = CardNumberFormat(card_number_format_str)
                except ValueError:
                    card.card_number_format = None
            else:
                card.card_number_format = None

            # card_number_padded: convertir en booleen ou None
            card_number_padded = data.get("card_number_padded")
            if card_number_padded is True or card_number_padded == "true":
                card.card_number_padded = True
            elif card_number_padded is False or card_number_padded == "false" or card_number_padded == "":
                card.card_number_padded = None
            else:
                card.card_number_padded = None

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
                    "card_count_official_override": card.card_count_official_override,
                    "card_number_format": card.card_number_format.value if card.card_number_format else None,
                    "card_number_padded": card.card_number_padded,
                    "effective_name": card.effective_name,
                    "effective_local_id": card.effective_local_id,
                    "effective_set_name": card.effective_set_name,
                    "effective_card_number_full": card.effective_card_number_full,
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

                # Annonces graded (PSA, CGC, PCA, etc.)
                graded_listings = [
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
                    for item in result.graded_items[:50]
                ] if result.graded_items else []

                return jsonify({
                    "success": True,
                    "query": result.query_used,
                    "total": result.active_count,
                    "listings": listings,
                    "reverse_listings": reverse_listings,
                    "graded_listings": graded_listings,
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
                graded_listings = meta.get("graded_listings", [])

                return jsonify({
                    "success": True,
                    "query": meta.get("query"),
                    "snapshot_date": snapshot.created_at.strftime('%d/%m/%y %H:%M') if snapshot.created_at else str(snapshot.as_of_date),
                    "total": snapshot.active_count,
                    "listings": listings,
                    "reverse_listings": reverse_listings,
                    "graded_listings": graded_listings,
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

    @app.route("/export/sets-reference")
    def export_sets_reference():
        """Export CSV de reference des sets (pour creer des cartes)."""
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)

        # Header
        writer.writerow(['serie_id', 'serie_name', 'set_id', 'set_name', 'release_date'])

        # Recuperer les sets groupes par serie
        series_sets = get_sets_grouped_by_series()

        for serie in series_sets:
            for s in serie['sets']:
                writer.writerow([
                    serie['serie_id'],
                    serie['serie_name'],
                    s['id'],
                    s['name'],
                    s['date'] or '',
                ])

        output.seek(0)
        csv_content = '\ufeff' + output.getvalue()
        return Response(
            csv_content,
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition': 'attachment; filename=sets_reference.csv'
            }
        )

    # ===================
    # IMPORT CSV
    # ===================

    @app.route("/import")
    def import_page():
        """Page d'import CSV."""
        return render_template("import.html")

    @app.route("/export/import-template")
    def export_import_template():
        """Telecharge un modele CSV vide pour l'import."""
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)

        # Header avec toutes les colonnes
        writer.writerow(['id', 'name', 'local_id', 'set_name', 'set_id', 'set_cardcount_official', 'variant', 'card_number_format', 'card_number_padded', 'ebay_query'])

        # Exemples commentes
        writer.writerow(['# Modification: remplir id, les autres colonnes sont optionnelles', '', '', '', '', '', '', '', '', ''])
        writer.writerow(['# Exemple modification:', '', '', '', '', '', '', '', '', ''])
        writer.writerow(['123', 'Pikachu', '25', '', '', '', '', '', '', ''])
        writer.writerow(['ecard2-H01-HOLO', '', 'H01', '', '', 'H32', '', 'LOCAL_ONLY', '', ''])
        writer.writerow(['svp-001-NORMAL', '', '', '', '', '', '', 'PROMO', 'true', ''])
        writer.writerow(['# Creation: laisser id vide, remplir name, local_id, set_id obligatoires', '', '', '', '', '', '', '', '', ''])
        writer.writerow(['# Exemple creation:', '', '', '', '', '', '', '', '', ''])
        writer.writerow(['', 'Ma Nouvelle Carte', '001', '', 'sv08', '', 'NORMAL', 'LOCAL_TOTAL', '', ''])
        writer.writerow(['', 'Carte Promo', '002', '', 'svp', '', 'NORMAL', 'PROMO', 'true', ''])
        writer.writerow(['', 'Carte Reverse', '003', '', 'sv08', '', 'REVERSE', '', '', 'Carte Reverse 003 pokemon'])

        output.seek(0)
        csv_content = '\ufeff' + output.getvalue()
        return Response(
            csv_content,
            mimetype='text/csv; charset=utf-8',
            headers={
                'Content-Disposition': 'attachment; filename=modele_import.csv'
            }
        )

    @app.route("/api/cards/import-csv", methods=["POST"])
    def api_import_csv():
        """API: Importer des cartes depuis un fichier CSV."""
        from src.models import Variant, CardNumberFormat

        if 'file' not in request.files:
            return jsonify({"success": False, "error": "Aucun fichier envoye"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "Aucun fichier selectionne"}), 400

        try:
            # Lire le contenu du fichier
            content = file.read().decode('utf-8-sig')  # utf-8-sig gere le BOM
            reader = csv.DictReader(io.StringIO(content), delimiter=';')

            results = {
                "updated": 0,
                "created": 0,
                "errors": [],
                "details": []
            }

            with get_session() as session:
                builder = EbayQueryBuilder()

                for row_num, row in enumerate(reader, start=2):  # start=2 car ligne 1 = header
                    try:
                        card_id = row.get('id', '').strip()

                        if card_id:
                            # Mode mise a jour
                            # Supporter ID numerique ou tcgdex_id
                            if card_id.isdigit():
                                card = session.query(Card).filter(Card.id == int(card_id)).first()
                            else:
                                # Chercher par tcgdex_id
                                card = session.query(Card).filter(Card.tcgdex_id == card_id).first()
                            if not card:
                                results["errors"].append(f"Ligne {row_num}: Carte {card_id} non trouvee")
                                continue

                            # Mettre a jour les overrides si les colonnes sont presentes
                            updated_fields = []
                            if 'name' in row and row['name'].strip():
                                new_name = row['name'].strip()
                                if new_name != card.name:
                                    card.name_override = new_name
                                    updated_fields.append('name')

                            if 'local_id' in row and row['local_id'].strip():
                                new_local_id = row['local_id'].strip()
                                if new_local_id != card.local_id:
                                    card.local_id_override = new_local_id
                                    updated_fields.append('local_id')

                            if 'set_name' in row and row['set_name'].strip():
                                new_set_name = row['set_name'].strip()
                                if new_set_name != card.set_name:
                                    card.set_name_override = new_set_name
                                    updated_fields.append('set_name')

                            # Gestion de l'override de la requete eBay
                            if 'ebay_query' in row and row['ebay_query'].strip():
                                new_ebay_query = row['ebay_query'].strip()
                                card.ebay_query_override = new_ebay_query
                                updated_fields.append('ebay_query')

                            # Gestion du set_cardcount_official ou set_id (construit card_number_full_override)
                            # En mode modification, set_id peut etre utilise pour le total du set
                            set_cardcount = None
                            if 'set_cardcount_official' in row and row['set_cardcount_official'].strip():
                                set_cardcount = row['set_cardcount_official'].strip()
                            elif 'set_id' in row and row['set_id'].strip():
                                # En mode modification, set_id = total du set (ex: H32)
                                set_cardcount = row['set_id'].strip()

                            if set_cardcount:
                                # Stocker le total officiel du set dans card_count_official_override
                                # La propriete effective_card_number_full construira automatiquement X/Y
                                card.card_count_official_override = set_cardcount
                                updated_fields.append('card_count_official')

                            # Gestion de card_number_format
                            if 'card_number_format' in row and row['card_number_format'].strip():
                                format_str = row['card_number_format'].strip().upper()
                                try:
                                    card.card_number_format = CardNumberFormat[format_str]
                                    updated_fields.append('card_number_format')
                                except KeyError:
                                    results["errors"].append(f"Ligne {row_num}: card_number_format invalide: {format_str}")

                            # Gestion de card_number_padded (padding avec zeros)
                            if 'card_number_padded' in row:
                                padded_str = row['card_number_padded'].strip().lower()
                                if padded_str in ('true', '1', 'oui', 'yes'):
                                    card.card_number_padded = True
                                    updated_fields.append('card_number_padded')
                                elif padded_str in ('false', '0', 'non', 'no', ''):
                                    card.card_number_padded = None
                                    updated_fields.append('card_number_padded')

                            if updated_fields:
                                # Regenerer la requete eBay seulement si pas d'override
                                if 'ebay_query' not in updated_fields:
                                    card.ebay_query = builder.build_query(card)
                                card.updated_at = datetime.utcnow()
                                results["updated"] += 1
                                results["details"].append(f"Carte {card_id} mise a jour: {', '.join(updated_fields)}")

                        else:
                            # Mode creation
                            set_id = row.get('set_id', '').strip()
                            name = row.get('name', '').strip()
                            local_id = row.get('local_id', '').strip()

                            if not set_id or not name or not local_id:
                                results["errors"].append(f"Ligne {row_num}: set_id, name et local_id requis pour creer une carte")
                                continue

                            # Determiner le variant
                            variant_str = row.get('variant', 'NORMAL').strip().upper()
                            try:
                                variant = Variant[variant_str] if variant_str else Variant.NORMAL
                            except KeyError:
                                variant = Variant.NORMAL

                            # Verifier si la carte existe deja
                            tcgdex_id = f"{set_id}-{local_id}"
                            if variant != Variant.NORMAL:
                                tcgdex_id = f"{tcgdex_id}-{variant.value}"

                            existing = session.query(Card).filter(Card.tcgdex_id == tcgdex_id).first()
                            if existing:
                                results["errors"].append(f"Ligne {row_num}: Carte {tcgdex_id} existe deja (ID: {existing.id})")
                                continue

                            # Recuperer le nom du set depuis la table sets
                            set_name = row.get('set_name', '').strip()
                            if not set_name:
                                # Chercher dans la table sets
                                set_obj = session.query(Set).filter(Set.id == set_id).first()
                                if set_obj:
                                    set_name = set_obj.name

                            if not set_name:
                                set_name = set_id  # Fallback

                            # Creer la carte
                            new_card = Card(
                                tcgdex_id=tcgdex_id,
                                set_id=set_id,
                                local_id=local_id,
                                name=name,
                                set_name=set_name,
                                variant=variant,
                                is_active=True,
                                created_at=datetime.utcnow(),
                            )

                            # Gestion de card_number_format pour creation
                            if 'card_number_format' in row and row['card_number_format'].strip():
                                format_str = row['card_number_format'].strip().upper()
                                try:
                                    new_card.card_number_format = CardNumberFormat[format_str]
                                except KeyError:
                                    new_card.card_number_format = CardNumberFormat.LOCAL_TOTAL
                                    results["errors"].append(f"Ligne {row_num}: card_number_format invalide '{format_str}', utilise LOCAL_TOTAL")
                            else:
                                new_card.card_number_format = CardNumberFormat.LOCAL_TOTAL

                            # Gestion de la requete eBay (override ou generee)
                            if 'ebay_query' in row and row['ebay_query'].strip():
                                new_card.ebay_query_override = row['ebay_query'].strip()
                            else:
                                new_card.ebay_query = builder.build_query(new_card)

                            session.add(new_card)
                            session.flush()  # Pour obtenir l'ID
                            results["created"] += 1
                            results["details"].append(f"Carte creee: {tcgdex_id} (ID: {new_card.id})")

                    except Exception as e:
                        results["errors"].append(f"Ligne {row_num}: {str(e)}")

                session.commit()

            return jsonify({
                "success": True,
                "message": f"{results['updated']} carte(s) mise(s) a jour, {results['created']} carte(s) creee(s)",
                **results
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ===================
    # TCGDEX SYNC
    # ===================

    @app.route("/tcgdex")
    def tcgdex_sync():
        """Page de synchronisation TCGdex."""
        return render_template("tcgdex.html")

    @app.route("/api/tcgdex/check-new-sets")
    def api_tcgdex_check_new_sets():
        """API: Verifier les nouveaux sets sur TCGdex."""
        try:
            client = TCGdexClient()
            tcgdex_sets = client.get_sets()

            with get_session() as session:
                # Recuperer les set_id existants depuis la table sets
                existing_set_ids = set(
                    row[0] for row in session.query(Set.id).all()
                )

                # Recuperer les set_id qui ont des cartes (pour info)
                imported_set_ids = set(
                    row[0] for row in session.query(Card.set_id).distinct().all()
                )

            # Trouver les nouveaux sets (pas encore dans la table sets)
            new_sets = []
            for s in tcgdex_sets:
                if s.id not in existing_set_ids:
                    # Recuperer les details du set pour avoir le nombre de cartes
                    set_details = client.get_set(s.id)
                    new_sets.append({
                        "id": s.id,
                        "name": s.name,
                        "card_count": set_details.card_count_total if set_details else None,
                        "release_date": set_details.release_date if set_details else None,
                        "already_imported": s.id in imported_set_ids,
                    })

            return jsonify({
                "success": True,
                "total_tcgdex_sets": len(tcgdex_sets),
                "existing_sets": len(existing_set_ids),
                "imported_sets": len(imported_set_ids),
                "new_sets": new_sets,
                "new_count": len(new_sets),
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/tcgdex/import-set/<set_id>", methods=["POST"])
    def api_tcgdex_import_set(set_id: str):
        """API: Importer un set depuis TCGdex."""
        try:
            with get_session() as session:
                importer = TCGdexImporter(session)
                stats = importer.import_set(set_id)
                session.commit()

                # Generer les requetes eBay pour les nouvelles cartes
                builder = EbayQueryBuilder()
                new_cards = session.query(Card).filter(
                    Card.set_id == set_id,
                    Card.ebay_query == None
                ).all()

                for card in new_cards:
                    card.ebay_query = builder.build_query(card)
                session.commit()

                return jsonify({
                    "success": True,
                    "set_id": set_id,
                    "cards_created": stats["created"],
                    "cards_updated": stats["updated"],
                    "queries_generated": len(new_cards),
                })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/tcgdex/series")
    def api_tcgdex_series():
        """API: Liste toutes les series avec leur statut (visible/masque)."""
        from sqlalchemy import func

        config = get_config()
        excluded_series = set(config.tcgdex.excluded_series)

        with get_session() as session:
            # Recuperer toutes les series depuis la table sets
            series_data = (
                session.query(
                    Set.serie_id,
                    Set.serie_name,
                    func.count(Set.id).label("set_count"),
                    func.min(Set.release_date).label("first_date")
                )
                .group_by(Set.serie_id, Set.serie_name)
                .order_by(func.min(Set.release_date))
                .all()
            )

            # Compter les cartes par serie
            cards_by_serie = dict(
                session.query(
                    Set.serie_id,
                    func.count(Card.id)
                )
                .join(Card, Card.set_id == Set.id)
                .group_by(Set.serie_id)
                .all()
            )

            series = []
            for s in series_data:
                series.append({
                    "serie_id": s.serie_id,
                    "serie_name": s.serie_name,
                    "set_count": s.set_count,
                    "card_count": cards_by_serie.get(s.serie_id, 0),
                    "is_visible": s.serie_id not in excluded_series,
                    "first_date": str(s.first_date) if s.first_date else None,
                })

            return jsonify({
                "success": True,
                "series": series,
                "excluded_count": len(excluded_series),
            })

    @app.route("/api/tcgdex/series/<serie_id>/toggle", methods=["POST"])
    def api_tcgdex_toggle_serie(serie_id: str):
        """API: Basculer la visibilite d'une serie."""
        from src.config import reload_config
        from pathlib import Path

        config = get_config()
        excluded = list(config.tcgdex.excluded_series)

        if serie_id in excluded:
            # Rendre visible
            excluded.remove(serie_id)
            is_visible = True
        else:
            # Masquer
            excluded.append(serie_id)
            is_visible = False

        # Mettre a jour la config
        config.tcgdex.excluded_series = excluded

        # Sauvegarder dans config.yaml
        config.save(Path("config.yaml"))

        # Recharger la config
        reload_config()

        return jsonify({
            "success": True,
            "serie_id": serie_id,
            "is_visible": is_visible,
            "excluded_series": excluded,
        })

    @app.route("/api/tcgdex/sets")
    def api_tcgdex_sets():
        """API: Liste tous les sets avec leur statut (visible/masque)."""
        from sqlalchemy import func

        config = get_config()
        excluded_series = set(config.tcgdex.excluded_series)
        excluded_sets = set(config.tcgdex.excluded_sets)

        with get_session() as session:
            # Recuperer tous les sets avec le nombre de cartes
            sets_data = (
                session.query(
                    Set.id,
                    Set.name,
                    Set.serie_id,
                    Set.serie_name,
                    Set.release_date,
                    func.count(Card.id).label("card_count")
                )
                .outerjoin(Card, Card.set_id == Set.id)
                .group_by(Set.id, Set.name, Set.serie_id, Set.serie_name, Set.release_date)
                .order_by(Set.release_date)
                .all()
            )

            sets = []
            for s in sets_data:
                # Un set est visible si:
                # - Sa serie n'est pas exclue
                # - Il n'est pas directement exclu
                serie_hidden = s.serie_id in excluded_series
                set_hidden = s.id in excluded_sets
                is_visible = not serie_hidden and not set_hidden

                sets.append({
                    "set_id": s.id,
                    "name": s.name,
                    "serie_id": s.serie_id,
                    "serie_name": s.serie_name,
                    "card_count": s.card_count,
                    "release_date": str(s.release_date) if s.release_date else None,
                    "is_visible": is_visible,
                    "hidden_by_serie": serie_hidden,
                    "hidden_directly": set_hidden,
                })

            return jsonify({
                "success": True,
                "sets": sets,
                "excluded_sets_count": len(excluded_sets),
                "excluded_series_count": len(excluded_series),
            })

    @app.route("/api/tcgdex/sets/<set_id>/toggle", methods=["POST"])
    def api_tcgdex_toggle_set(set_id: str):
        """API: Basculer la visibilite d'un set individuel."""
        from src.config import reload_config
        from pathlib import Path

        config = get_config()
        excluded = list(config.tcgdex.excluded_sets)

        if set_id in excluded:
            # Rendre visible
            excluded.remove(set_id)
            is_visible = True
        else:
            # Masquer
            excluded.append(set_id)
            is_visible = False

        # Mettre a jour la config
        config.tcgdex.excluded_sets = excluded

        # Sauvegarder dans config.yaml
        config.save(Path("config.yaml"))

        # Recharger la config
        reload_config()

        return jsonify({
            "success": True,
            "set_id": set_id,
            "is_visible": is_visible,
            "excluded_sets": excluded,
        })

    @app.route("/api/tcgdex/import-sets", methods=["POST"])
    def api_tcgdex_import_sets():
        """API: Importer plusieurs sets depuis TCGdex."""
        data = request.get_json() or {}
        set_ids = data.get("set_ids", [])

        if not set_ids:
            return jsonify({"success": False, "error": "set_ids requis"}), 400

        results = {
            "success": True,
            "imported": [],
            "errors": [],
            "total_cards_created": 0,
            "total_cards_updated": 0,
        }

        with get_session() as session:
            importer = TCGdexImporter(session)
            builder = EbayQueryBuilder()

            for set_id in set_ids:
                try:
                    stats = importer.import_set(set_id)
                    session.commit()

                    # Generer les requetes eBay
                    new_cards = session.query(Card).filter(
                        Card.set_id == set_id,
                        Card.ebay_query == None
                    ).all()

                    for card in new_cards:
                        card.ebay_query = builder.build_query(card)
                    session.commit()

                    results["imported"].append({
                        "set_id": set_id,
                        "cards_created": stats["created"],
                        "cards_updated": stats["updated"],
                    })
                    results["total_cards_created"] += stats["created"]
                    results["total_cards_updated"] += stats["updated"]

                except Exception as e:
                    results["errors"].append({
                        "set_id": set_id,
                        "error": str(e),
                    })
                    session.rollback()

        return jsonify(results)

    # ===================
    # SETTINGS
    # ===================

    @app.route("/settings")
    def settings_page():
        """Page de configuration des parametres."""
        from src.ebay.usage_tracker import refresh_rate_limits_from_ebay
        from datetime import datetime

        with get_session() as session:
            # Recuperer tous les settings
            settings = Settings.get_all(session)

            # Usage API eBay - appel reel
            ebay_usage = refresh_rate_limits_from_ebay()
            if ebay_usage:
                # Formatter le reset
                if ebay_usage.get("reset"):
                    try:
                        reset_dt = datetime.fromisoformat(ebay_usage["reset"].replace("Z", "+00:00"))
                        ebay_usage["reset_formatted"] = reset_dt.strftime("%d/%m %H:%M")
                    except Exception:
                        ebay_usage["reset_formatted"] = ebay_usage["reset"]
                # Calculer le pourcentage
                if ebay_usage.get("limit") and ebay_usage["limit"] > 0:
                    ebay_usage["percent"] = int((ebay_usage.get("count", 0) / ebay_usage["limit"]) * 100)
                else:
                    ebay_usage["percent"] = 0

            return render_template("settings.html",
                settings=settings,
                ebay_usage=ebay_usage,
            )

    @app.route("/settings", methods=["POST"])
    def settings_save():
        """Sauvegarder les parametres."""
        with get_session() as session:
            # batch_enabled
            batch_enabled = request.form.get("batch_enabled", "false")
            Settings.set_value(session, "batch_enabled", batch_enabled)

            # batch_hour
            batch_hour = request.form.get("batch_hour", "3")
            try:
                batch_hour_int = int(batch_hour)
                if batch_hour_int < 0 or batch_hour_int > 23:
                    batch_hour = "3"
                else:
                    batch_hour = str(batch_hour_int)
            except ValueError:
                batch_hour = "3"
            Settings.set_value(session, "batch_hour", batch_hour)

            # batch_minute
            batch_minute = request.form.get("batch_minute", "0")
            try:
                batch_minute_int = int(batch_minute)
                if batch_minute_int < 0 or batch_minute_int > 59:
                    batch_minute = "0"
                else:
                    batch_minute = str(batch_minute_int)
            except ValueError:
                batch_minute = "0"
            Settings.set_value(session, "batch_minute", batch_minute)

            # daily_api_limit
            daily_api_limit = request.form.get("daily_api_limit", "5000")
            try:
                daily_limit_int = int(daily_api_limit)
                if daily_limit_int < 0:
                    daily_api_limit = "5000"
                else:
                    daily_api_limit = str(daily_limit_int)
            except ValueError:
                daily_api_limit = "5000"
            Settings.set_value(session, "daily_api_limit", daily_api_limit)

            # low_value_threshold (seuil basse valeur en euros)
            low_value_threshold = request.form.get("low_value_threshold", "10")
            try:
                threshold_float = float(low_value_threshold)
                if threshold_float < 0:
                    low_value_threshold = "10"
                else:
                    low_value_threshold = str(threshold_float)
            except ValueError:
                low_value_threshold = "10"
            Settings.set_value(session, "low_value_threshold", low_value_threshold)

            # low_value_refresh_days (frequence rafraichissement basse valeur)
            low_value_refresh_days = request.form.get("low_value_refresh_days", "60")
            try:
                refresh_int = int(low_value_refresh_days)
                if refresh_int < 1:
                    low_value_refresh_days = "60"
                else:
                    low_value_refresh_days = str(refresh_int)
            except ValueError:
                low_value_refresh_days = "60"
            Settings.set_value(session, "low_value_refresh_days", low_value_refresh_days)

            # max_error_retries (nb erreurs avant basse priorite)
            max_error_retries = request.form.get("max_error_retries", "3")
            try:
                retries_int = int(max_error_retries)
                if retries_int < 1:
                    max_error_retries = "3"
                else:
                    max_error_retries = str(retries_int)
            except ValueError:
                max_error_retries = "3"
            Settings.set_value(session, "max_error_retries", max_error_retries)

            session.commit()

        flash("Parametres sauvegardes avec succes", "success")
        return redirect(url_for("settings_page"))

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


# Instance globale pour gunicorn
app = create_app()


def run_admin():
    """Lance le serveur admin."""
    config = get_config()
    app = create_app()
    app.run(host=config.admin_host, port=config.admin_port, debug=True)


if __name__ == "__main__":
    run_admin()
