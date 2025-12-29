#!/usr/bin/env python3
"""
CLI pour l'outil de pricing Pokeventes.

Commandes:
    init            Initialise la base de donnees
    import-tcgdex   Importe les cartes depuis TCGdex
    generate-queries Genere les requetes eBay pour les cartes
    run-batch       Execute le batch de pricing
    export-csv      Exporte les prix en CSV
    admin           Lance l'interface admin web
"""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

# Ajouter src au path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import get_config, AppConfig
from src.database import init_db, reset_db, get_session
from src.models import Card, BuyPrice, BatchMode, Variant

console = Console()


@click.group()
@click.option("--config", "-c", type=click.Path(exists=True), help="Chemin vers config.yaml")
@click.pass_context
def cli(ctx, config):
    """Outil de pricing Pokeventes - Calcul automatique des prix de rachat."""
    ctx.ensure_object(dict)
    if config:
        from src.config import reload_config
        ctx.obj["config"] = reload_config(Path(config))
    else:
        ctx.obj["config"] = get_config()


@cli.command()
@click.option("--force", is_flag=True, help="Force la reinitialisation (supprime les donnees)")
def init(force):
    """Initialise la base de donnees."""
    if force:
        if click.confirm("Cela va supprimer toutes les donnees. Continuer?"):
            console.print("[yellow]Reinitialisation de la base...[/yellow]")
            reset_db()
            console.print("[green]Base reinitialisee.[/green]")
    else:
        console.print("[cyan]Initialisation de la base...[/cyan]")
        init_db()
        console.print("[green]Base initialisee.[/green]")


@cli.command("import-tcgdex")
@click.option("--set", "set_id", help="Importer uniquement ce set")
@click.option("--update-pricing", is_flag=True, help="Mettre a jour uniquement les prix Cardmarket")
def import_tcgdex(set_id, update_pricing):
    """Importe les cartes depuis TCGdex."""
    from src.tcgdex import TCGdexImporter

    init_db()  # S'assurer que la DB existe

    importer = TCGdexImporter()

    if update_pricing:
        console.print("[cyan]Mise a jour des prix Cardmarket...[/cyan]")
        stats = importer.update_pricing_only()
        console.print(f"[green]Termine: {stats['updated']} cartes mises a jour, {stats['errors']} erreurs[/green]")
    elif set_id:
        console.print(f"[cyan]Import du set {set_id}...[/cyan]")
        stats = importer.import_set(set_id)
        console.print(f"[green]Termine: {stats['created']} creees, {stats['updated']} mises a jour[/green]")
    else:
        console.print("[cyan]Import de tous les sets TCGdex...[/cyan]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            stats = importer.import_all_sets(progress)

        console.print(f"[green]Termine:[/green]")
        console.print(f"  Sets: {stats['sets']}")
        console.print(f"  Cartes creees: {stats['cards_created']}")
        console.print(f"  Cartes mises a jour: {stats['cards_updated']}")
        console.print(f"  Erreurs: {stats['errors']}")


@cli.command("generate-queries")
@click.option("--force", is_flag=True, help="Regenerer meme si deja presente")
def generate_queries(force):
    """Genere les requetes eBay pour toutes les cartes."""
    from src.ebay import EbayQueryBuilder

    builder = EbayQueryBuilder()

    with get_session() as session:
        if force:
            cards = session.query(Card).filter(Card.is_active == True).all()
        else:
            cards = session.query(Card).filter(
                Card.is_active == True,
                Card.ebay_query == None,
                Card.ebay_query_override == None
            ).all()

        console.print(f"[cyan]Generation des requetes pour {len(cards)} cartes...[/cyan]")

        count = 0
        for card in cards:
            if force or not card.ebay_query:
                builder.generate_for_card(card)
                count += 1

        session.commit()
        console.print(f"[green]Termine: {count} requetes generees[/green]")


@cli.command("run-batch")
@click.option("--mode", type=click.Choice(["full", "hybrid"]), default="full", help="Mode de batch")
@click.option("--limit", type=int, help="Limite le nombre de cartes")
@click.option("--card-id", type=int, multiple=True, help="Traiter uniquement ces card_ids")
def run_batch(mode, limit, card_id):
    """Execute le batch de pricing."""
    from src.batch import BatchRunner

    batch_mode = BatchMode.FULL_EBAY if mode == "full" else BatchMode.HYBRID
    card_ids = list(card_id) if card_id else None

    runner = BatchRunner()

    console.print(f"[cyan]Lancement du batch (mode: {batch_mode.value})...[/cyan]")

    stats, anomalies = runner.run_with_progress(
        mode=batch_mode,
        card_ids=card_ids,
        limit=limit,
    )

    console.print("\n[green]Batch termine:[/green]")
    console.print(f"  Total: {stats.total_cards}")
    console.print(f"  Succes: {stats.succeeded}")
    console.print(f"  Echecs: {stats.failed}")
    console.print(f"  Exclus (faible valeur): {stats.skipped}")

    if anomalies.high_dispersions:
        console.print(f"\n[yellow]Attention: {len(anomalies.high_dispersions)} cartes avec haute dispersion[/yellow]")

    if anomalies.mismatches:
        console.print(f"[yellow]Attention: {len(anomalies.mismatches)} fallbacks Cardmarket[/yellow]")

    if anomalies.query_issues:
        console.print(f"[red]Erreurs de requete: {len(anomalies.query_issues)}[/red]")
        for issue in anomalies.query_issues[:5]:
            console.print(f"  - {issue['name']}: {issue['error']}")


@cli.command("export-csv")
@click.argument("output", type=click.Path())
@click.option("--full", is_flag=True, help="Export complet avec toutes les colonnes")
@click.option("--anomalies", is_flag=True, help="Export uniquement les anomalies")
@click.option("--min-confidence", type=int, help="Score minimum de confiance")
@click.option("--include-low-conf", is_flag=True, help="Inclure les cartes LOW_CONF")
def export_csv(output, full, anomalies, min_confidence, include_low_conf):
    """Exporte les prix en CSV."""
    from src.export import CSVExporter

    exporter = CSVExporter()
    output_path = Path(output)

    if anomalies:
        console.print("[cyan]Export des anomalies...[/cyan]")
        stats = exporter.export_anomalies(output_path)
    elif full:
        console.print("[cyan]Export complet...[/cyan]")
        stats = exporter.export_full(output_path)
    else:
        console.print("[cyan]Export des prix...[/cyan]")
        stats = exporter.export(
            output_path,
            only_ok=not include_low_conf,
            min_confidence=min_confidence,
        )

    console.print(f"[green]Termine: {stats['exported']} lignes exportees vers {output}[/green]")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host du serveur")
@click.option("--port", default=5000, type=int, help="Port du serveur")
def admin(host, port):
    """Lance l'interface admin web."""
    from admin.app import create_app

    init_db()  # S'assurer que la DB existe

    app = create_app()
    console.print(f"[cyan]Lancement de l'admin sur http://{host}:{port}[/cyan]")
    app.run(host=host, port=port, debug=True)


@cli.command()
def stats():
    """Affiche les statistiques de la base."""
    from src.models import MarketSnapshot, BatchRun

    with get_session() as session:
        total_cards = session.query(Card).count()
        active_cards = session.query(Card).filter(Card.is_active == True).count()
        cards_with_query = session.query(Card).filter(Card.ebay_query != None).count()
        cards_with_price = session.query(BuyPrice).count()
        snapshots = session.query(MarketSnapshot).count()
        batches = session.query(BatchRun).count()

        console.print("\n[cyan]Statistiques:[/cyan]")
        console.print(f"  Cartes totales: {total_cards}")
        console.print(f"  Cartes actives: {active_cards}")
        console.print(f"  Avec requete eBay: {cards_with_query}")
        console.print(f"  Avec prix calcule: {cards_with_price}")
        console.print(f"  Snapshots: {snapshots}")
        console.print(f"  Batches executes: {batches}")


@cli.command("test-ebay")
@click.argument("query")
@click.option("--limit", default=10, type=int, help="Nombre de resultats")
def test_ebay(query, limit):
    """Teste une requete eBay."""
    from src.ebay import EbayClient

    client = EbayClient()

    console.print(f"[cyan]Test de la requete: {query}[/cyan]")

    try:
        result = client.search(query, limit=limit)

        console.print(f"\n[green]Total trouve: {result.total}[/green]")
        console.print(f"Items retournes: {len(result.items)}\n")

        for item in result.items:
            price_str = f"{item.effective_price:.2f} {item.currency}"
            console.print(f"  [{price_str}] {item.title[:60]}...")

        if result.warnings:
            console.print(f"\n[yellow]Warnings: {result.warnings}[/yellow]")

    except Exception as e:
        console.print(f"[red]Erreur: {e}[/red]")


@cli.command("test-card")
@click.argument("card_id", type=int)
def test_card(card_id):
    """Teste le pricing d'une carte specifique."""
    from src.ebay import EbayWorker
    from src.pricing import PriceGuardrails, PriceCalculator, ConfidenceScorer

    with get_session() as session:
        card = session.query(Card).filter(Card.id == card_id).first()
        if not card:
            console.print(f"[red]Carte {card_id} non trouvee[/red]")
            return

        console.print(f"\n[cyan]Test de la carte: {card.name}[/cyan]")
        console.print(f"  Set: {card.set_name}")
        console.print(f"  Variant: {card.variant.value if card.variant else 'NORMAL'}")
        console.print(f"  CM trend: {card.cm_trend}")
        console.print(f"  CM avg30: {card.cm_avg30}")
        console.print(f"  Query: {card.effective_ebay_query}")

        # Collecter eBay
        worker = EbayWorker()
        console.print("\n[cyan]Collecte eBay...[/cyan]")
        result = worker.collect_for_card(card)

        if result.success:
            console.print(f"  [green]Succes[/green]")
            console.print(f"  Active count: {result.active_count}")
            if result.stats:
                console.print(f"  Sample size: {result.stats.sample_size}")
                console.print(f"  p20: {result.stats.p20:.2f}" if result.stats.p20 else "  p20: -")
                console.print(f"  p50: {result.stats.p50:.2f}" if result.stats.p50 else "  p50: -")
                console.print(f"  p80: {result.stats.p80:.2f}" if result.stats.p80 else "  p80: -")
                console.print(f"  Dispersion: {result.stats.dispersion:.2f}" if result.stats.dispersion else "  Dispersion: -")
        else:
            console.print(f"  [red]Echec: {result.error}[/red]")

        # Garde-fous
        guardrails = PriceGuardrails()
        gr_result = guardrails.check(card, result.anchor_price, result.stats.dispersion if result.stats else None)

        console.print(f"\n[cyan]Garde-fous:[/cyan]")
        console.print(f"  Mismatch: {gr_result.is_mismatch}")
        if gr_result.mismatch_reason:
            console.print(f"  Raison: {gr_result.mismatch_reason}")
        console.print(f"  Ancre finale: {gr_result.final_anchor:.2f}" if gr_result.final_anchor else "  Ancre finale: -")
        console.print(f"  Source: {gr_result.final_source.value}")

        # Calcul prix
        if gr_result.final_anchor:
            calculator = PriceCalculator()
            calc = calculator.calculate(
                anchor_price=gr_result.final_anchor,
                dispersion=result.stats.dispersion if result.stats else None,
                active_count=result.active_count,
                sample_size=result.stats.sample_size if result.stats else None,
                anchor_source=gr_result.final_source,
            )

            console.print(f"\n[cyan]Prix de rachat:[/cyan]")
            console.print(f"  [green]Neuf: {calc.buy_neuf:.2f} EUR[/green]")
            console.print(f"  Bon: {calc.buy_bon:.2f} EUR")
            console.print(f"  Correct: {calc.buy_correct:.2f} EUR")
            console.print(f"  Risk total: {calc.risk_total:.2%}")


@cli.command("create-ed1-variants")
@click.option("--set", "set_ids", multiple=True, help="IDs des sets (ex: base1, base2)")
@click.option("--all-old-sets", is_flag=True, help="Creer pour tous les anciens sets (Base, Jungle, Fossil, etc.)")
def create_ed1_variants(set_ids, all_old_sets):
    """Cree des variantes Edition 1 pour les anciens sets."""
    from src.ebay import EbayQueryBuilder

    # Sets avec Edition 1
    OLD_SETS_WITH_ED1 = [
        "base1",  # Set de Base
        "base2",  # Jungle
        "base3",  # Fossile
        "base4",  # Team Rocket
        "base5",  # Gym Heroes
        "base6",  # Gym Challenge
        "neo1",   # Neo Genesis
        "neo2",   # Neo Discovery
        "neo3",   # Neo Revelation
        "neo4",   # Neo Destiny
    ]

    if all_old_sets:
        target_sets = OLD_SETS_WITH_ED1
    elif set_ids:
        target_sets = list(set_ids)
    else:
        console.print("[yellow]Specifiez --set ou --all-old-sets[/yellow]")
        console.print(f"Sets disponibles: {', '.join(OLD_SETS_WITH_ED1)}")
        return

    builder = EbayQueryBuilder()
    created = 0

    with get_session() as session:
        for set_id in target_sets:
            # Trouver les cartes de ce set qui n'ont pas de variante ED1
            cards = session.query(Card).filter(
                Card.set_id == set_id,
                Card.variant.in_([Variant.NORMAL, Variant.HOLO]),
                Card.is_active == True
            ).all()

            console.print(f"[cyan]Set {set_id}: {len(cards)} cartes trouvees[/cyan]")

            for card in cards:
                # Verifier si une version ED1 existe deja
                existing = session.query(Card).filter(
                    Card.set_id == set_id,
                    Card.local_id == card.local_id,
                    Card.variant == Variant.FIRST_ED
                ).first()

                if existing:
                    continue

                # Creer la variante Edition 1
                ed1_card = Card(
                    tcgdex_id=f"{card.tcgdex_id}-ed1",
                    set_id=card.set_id,
                    local_id=card.local_id,
                    name=card.name,
                    name_en=card.name_en,
                    set_name=card.set_name,
                    set_code=card.set_code,
                    card_number_full=card.card_number_full,
                    variant=Variant.FIRST_ED,
                    rarity=card.rarity,
                    language_scope=card.language_scope,
                    is_active=True,
                )

                # Generer la requete eBay
                builder.generate_for_card(ed1_card)

                session.add(ed1_card)
                created += 1

        session.commit()

    console.print(f"[green]Termine: {created} variantes Edition 1 creees[/green]")


@cli.command("listings")
@click.argument("card_id", type=int)
@click.option("--refresh", is_flag=True, help="Relancer la collecte eBay")
def listings(card_id, refresh):
    """Affiche les annonces eBay pour une carte."""
    from src.models import MarketSnapshot
    from src.ebay import EbayWorker

    with get_session() as session:
        card = session.query(Card).filter(Card.id == card_id).first()
        if not card:
            console.print(f"[red]Carte {card_id} non trouvee[/red]")
            return

        console.print(f"\n[cyan]{card.name}[/cyan] ({card.set_name})")
        console.print(f"Query: {card.effective_ebay_query}\n")

        if refresh:
            # Collecter en direct
            console.print("[cyan]Collecte eBay en cours...[/cyan]\n")
            worker = EbayWorker()
            result = worker.collect_for_card(card)

            if not result.success:
                console.print(f"[red]Erreur: {result.error}[/red]")
                return

            items = result.items
            console.print(f"[green]{len(items)} annonces trouvees[/green]\n")

            for i, item in enumerate(items[:30], 1):
                price_str = f"{item.effective_price:.2f} EUR"
                console.print(f"{i:2}. [{price_str:>10}] {item.title[:55]}...")
                if item.item_web_url:
                    console.print(f"    [link]{item.item_web_url}[/link]")
        else:
            # Afficher depuis le dernier snapshot
            snapshot = session.query(MarketSnapshot).filter(
                MarketSnapshot.card_id == card_id
            ).order_by(MarketSnapshot.as_of_date.desc()).first()

            if not snapshot:
                console.print("[yellow]Aucun snapshot trouve. Utilisez --refresh pour collecter.[/yellow]")
                return

            meta = snapshot.get_raw_meta()
            listings_data = meta.get("listings", [])

            if not listings_data:
                console.print("[yellow]Aucune annonce stockee. Utilisez --refresh pour collecter.[/yellow]")
                return

            console.print(f"[dim]Snapshot du {snapshot.as_of_date}[/dim]")
            console.print(f"Ancre: {snapshot.anchor_price:.2f} EUR (p20)")
            console.print(f"Dispersion: {snapshot.dispersion:.2f}" if snapshot.dispersion else "")
            console.print(f"\n[green]{len(listings_data)} annonces:[/green]\n")

            for i, listing in enumerate(listings_data, 1):
                price = listing.get("effective_price", listing.get("price", 0))
                title = listing.get("title", "")[:55]
                url = listing.get("url", "")
                console.print(f"{i:2}. [{price:>8.2f} EUR] {title}...")
                if url:
                    console.print(f"    {url}")


if __name__ == "__main__":
    cli()
