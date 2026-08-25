"""
CLI-Einstiegspunkt.

Beispiele:
    # Heutige Krafttraining-Aktivitäten importieren
    python -m garmin_sparky_sync --timeframe today

    # Letzte 7 Tage, nur anzeigen was passieren würde (kein POST)
    python -m garmin_sparky_sync -t 7days --dry-run

    # Manueller Datumsbereich
    python -m garmin_sparky_sync --start-date 2026-07-01 --end-date 2026-07-31

    # Einzelne Aktivität (z.B. für Webhook-/Event-Trigger)
    python -m garmin_sparky_sync --activity-id 123456789

    # Rohes Garmin-JSON einer Aktivität zur Kalibrierung des Parsings ausgeben
    python -m garmin_sparky_sync --activity-id 123456789 --debug-dump-raw

    # Übung von zukünftigen Syncs ausschliessen (z.B. Aufwärmübungen)
    python -m garmin_sparky_sync --ignore-add "Arm Circles"
    python -m garmin_sparky_sync --ignore-list
    python -m garmin_sparky_sync --ignore-remove "Arm Circles"

    # Lokale Caches einsehen bzw. zurücksetzen
    python -m garmin_sparky_sync --cache-show
    python -m garmin_sparky_sync --cache-clear-exercises
    python -m garmin_sparky_sync --cache-clear-synced
    python -m garmin_sparky_sync --cache-clear-all

    # Gemini-API-Key/Erreichbarkeit testen
    python -m garmin_sparky_sync --test-gemini
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .config import ConfigError, load_config
from .garmin_client import GarminAuthError, GarminClient
from .gemini_client import GeminiClient, GeminiError
from .handlers import ACTIVE_HANDLERS, ActivityTypeHandler
from .mapper import ExerciseIdResolver
from .sparky_client import SparkyAPIError, SparkyClient
from .state import SyncState

logger = logging.getLogger("garmin_sparky_sync")

_TIMEFRAME_ALIASES = {
    "today": "today",
    "7days": "7days",
    "week": "7days",
    "30days": "30days",
    "month": "30days",
}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin_sparky_sync",
        description=(
            "Synchronisiert Krafttraining-Aktivitäten von Garmin Connect als "
            "gebündelte Workout-Sessions in SparkyFitness."
        ),
    )

    timeframe_group = parser.add_mutually_exclusive_group()
    timeframe_group.add_argument(
        "--timeframe",
        "-t",
        choices=sorted(_TIMEFRAME_ALIASES.keys()),
        default=None,
        help="Vordefinierte Zeitspanne für den Sync (Default: 'today', falls "
        "keine anderen Zeit-/ID-Optionen angegeben werden).",
    )
    timeframe_group.add_argument(
        "--activity-id",
        type=str,
        default=None,
        help="Synchronisiert eine einzelne, spezifische Garmin-Aktivität "
        "(z.B. für Event-/Webhook-basierte Trigger).",
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start des manuellen Datumsbereichs (YYYY-MM-DD). Erfordert --end-date.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Ende des manuellen Datumsbereichs (YYYY-MM-DD). Erfordert --start-date.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zeigt an, was importiert würde, ohne tatsächlich an Sparky zu senden.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Importiert auch Aktivitäten erneut, die laut lokalem State bereits "
        "synchronisiert wurden.",
    )
    parser.add_argument(
        "--debug-dump-raw",
        action="store_true",
        help="Gibt für die gefundenen Aktivitäten nur das rohe Garmin-JSON der "
        "Satz-Details aus (zur Kalibrierung des Parsings) und beendet danach, "
        "ohne irgendetwas an Sparky zu senden.",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Expliziter Pfad zu einer .env-Datei (Default: Standard-Suche).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Ausführlicheres Logging (DEBUG-Level)."
    )

    mgmt_group = parser.add_argument_group(
        "Verwaltung (Ignore-Liste & Caches)",
        "Diese Optionen führen nur die jeweilige Verwaltungsaktion aus und "
        "beenden danach, ohne Garmin-Login oder Sync (kombinierbar).",
    )
    mgmt_group.add_argument(
        "--ignore-add",
        action="append",
        metavar="NAME",
        default=None,
        help="Fügt eine Übung (Name, case-insensitive) zur Ignore-Liste hinzu. "
        "Mehrfach verwendbar. Ignorierte Übungen werden beim Sync komplett "
        "übersprungen (keine Sparky-Get-or-Create-Anfrage).",
    )
    mgmt_group.add_argument(
        "--ignore-remove",
        action="append",
        metavar="NAME",
        default=None,
        help="Entfernt eine Übung von der Ignore-Liste. Mehrfach verwendbar.",
    )
    mgmt_group.add_argument(
        "--ignore-list",
        action="store_true",
        help="Zeigt die aktuelle Ignore-Liste an.",
    )
    mgmt_group.add_argument(
        "--cache-show",
        action="store_true",
        help="Zeigt Inhalt der lokalen Caches an (bereits importierte "
        "Garmin-Aktivitäten + Exercise-ID-Cache).",
    )
    mgmt_group.add_argument(
        "--cache-clear-synced",
        action="store_true",
        help="Löscht den Cache bereits importierter Garmin-Aktivitäten "
        "(Idempotenz-State). Aktivitäten im gewählten Zeitraum würden beim "
        "nächsten Sync erneut importiert.",
    )
    mgmt_group.add_argument(
        "--cache-clear-exercises",
        action="store_true",
        help="Löscht den Exercise-ID-Cache. Übungen werden beim nächsten "
        "Sync erneut in Sparky gesucht/angelegt.",
    )
    mgmt_group.add_argument(
        "--cache-clear-all",
        action="store_true",
        help="Entspricht --cache-clear-synced + --cache-clear-exercises.",
    )
    mgmt_group.add_argument(
        "--test-gemini",
        action="store_true",
        help="Prüft GEMINI_API_KEY/GEMINI_MODEL, indem eine echte Test-Anfrage "
        "(exakt der Codepfad aus dem Anlegen neuer Übungen) an Gemini "
        "geschickt wird. Exit-Code 0 bei Erfolg, 2 bei Fehler.",
    )

    return parser


def _resolve_date_range(args: argparse.Namespace) -> tuple[dt.date, dt.date]:
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise ValueError("--start-date und --end-date müssen zusammen angegeben werden.")
        try:
            start = dt.date.fromisoformat(args.start_date)
            end = dt.date.fromisoformat(args.end_date)
        except ValueError as exc:
            raise ValueError(f"Ungültiges Datumsformat, erwartet YYYY-MM-DD: {exc}") from exc
        if start > end:
            raise ValueError("--start-date darf nicht nach --end-date liegen.")
        return start, end

    timeframe = _TIMEFRAME_ALIASES[args.timeframe or "today"]
    today = dt.date.today()
    if timeframe == "today":
        return today, today
    if timeframe == "7days":
        return today - dt.timedelta(days=6), today
    if timeframe == "30days":
        return today - dt.timedelta(days=29), today
    raise AssertionError(f"Unbekannte Timeframe: {timeframe}")  # pragma: no cover


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _find_handler(activity_type_key: str) -> ActivityTypeHandler | None:
    for handler in ACTIVE_HANDLERS:
        if activity_type_key in handler.garmin_type_keys:
            return handler
    return None


def _has_management_flags(args: argparse.Namespace) -> bool:
    return bool(
        args.ignore_add
        or args.ignore_remove
        or args.ignore_list
        or args.cache_show
        or args.cache_clear_synced
        or args.cache_clear_exercises
        or args.cache_clear_all
        or args.test_gemini
    )


def _test_gemini(cfg) -> bool:
    """Prüft Gemini-Konfiguration/Erreichbarkeit über eine echte Anfrage -
    exakt derselbe Codepfad wie beim Anlegen neuer Übungen im Sync. Gibt
    True bei Erfolg zurück, False bei jedem Fehler (kein Key, Netzwerk,
    ungültige Antwort)."""
    if not cfg.gemini_api_key:
        print(
            "Gemini-Test fehlgeschlagen: GEMINI_API_KEY ist nicht gesetzt "
            "(siehe .env)."
        )
        return False

    print(f"Teste Gemini-API (Modell: {cfg.gemini_model})...")
    gemini = GeminiClient(cfg.gemini_api_key, cfg.gemini_model)
    try:
        metadata = gemini.enrich_exercise("Push-Up")
    except GeminiError as exc:
        print(f"Gemini-Test fehlgeschlagen: {exc}")
        return False

    print("Gemini-Test erfolgreich! Beispiel-Metadaten für 'Push-Up':")
    print(f"  category:        {metadata.category}")
    print(f"  equipment:       {metadata.equipment}")
    print(f"  primary_muscles: {metadata.primary_muscles}")
    print(f"  force/level/mechanic: {metadata.force} / {metadata.level} / {metadata.mechanic}")
    return True


def _run_management_actions(args: argparse.Namespace, cfg) -> bool:
    """Führt Ignore-Liste-, Cache- und Gemini-Testaktionen aus. Läuft
    komplett lokal (kein Garmin-Login, kein Sparky-Request). Gibt False
    zurück, falls eine der Aktionen fehlgeschlagen ist (aktuell nur
    --test-gemini kann fehlschlagen)."""
    success = True

    with SyncState(cfg.state_dir) as state:
        for name in args.ignore_add or []:
            key = name.strip().lower()
            if state.add_ignored(key):
                print(f"Ignoriert: '{name}' zur Ignore-Liste hinzugefügt.")
            else:
                print(f"Ignoriert: '{name}' war bereits auf der Ignore-Liste.")

        for name in args.ignore_remove or []:
            key = name.strip().lower()
            if state.remove_ignored(key):
                print(f"Ignoriert: '{name}' von der Ignore-Liste entfernt.")
            else:
                print(f"Ignoriert: '{name}' stand nicht auf der Ignore-Liste.")

        if args.ignore_list:
            ignored = state.list_ignored()
            print(f"\nIgnorierte Übungen ({len(ignored)}):")
            for name in ignored:
                print(f"  - {name}")
            if not ignored:
                print("  (keine)")

        if args.cache_clear_all or args.cache_clear_synced:
            count = state.clear_synced()
            print(f"Cache 'bereits importierte Aktivitäten' geleert ({count} Eintrag/Einträge).")

        if args.cache_clear_all or args.cache_clear_exercises:
            count = state.clear_exercise_cache()
            print(f"Exercise-ID-Cache geleert ({count} Eintrag/Einträge).")

        if args.cache_show:
            synced = state.list_synced()
            print(f"\nBereits synchronisierte Garmin-Aktivitäten ({len(synced)}):")
            for activity_id, name, entry_date in synced:
                date_display = entry_date or "unbekanntes Datum"
                name_display = name or "unbekannter Name"
                print(f"  - {date_display} | {activity_id} | {name_display}")
            if not synced:
                print("  (keine)")

            exercise_cache = state.list_exercise_cache()
            print(f"\nExercise-ID-Cache ({len(exercise_cache)}):")
            for name, exercise_id in sorted(exercise_cache.items()):
                print(f"  - {name} -> {exercise_id}")
            if not exercise_cache:
                print("  (keine)")

    if args.test_gemini:
        success = _test_gemini(cfg) and success

    return success


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = load_config(env_file=args.env_file)
    except ConfigError:
        return 2

    if _has_management_flags(args):
        success = _run_management_actions(args, cfg)
        return 0 if success else 2

    garmin = GarminClient(cfg.garmin_email, cfg.garmin_password, cfg.state_dir)
    try:
        garmin.login()
    except GarminAuthError as exc:
        logger.error("%s", exc)
        return 2

    sparky = SparkyClient(cfg.sparky_base_url, cfg.sparky_api_key)
    gemini = GeminiClient(cfg.gemini_api_key, cfg.gemini_model) if cfg.gemini_api_key else None
    if gemini is None:
        logger.info(
            "GEMINI_API_KEY nicht gesetzt - neue Übungen werden mit generischen "
            "Platzhalter-Metadaten angelegt."
        )

    all_type_keys: frozenset[str] = frozenset().union(
        *(handler.garmin_type_keys for handler in ACTIVE_HANDLERS)
    )

    imported, skipped, failed = 0, 0, 0

    with SyncState(cfg.state_dir) as state:
        resolver = ExerciseIdResolver(sparky, state, gemini=gemini)

        if args.activity_id:
            activity = garmin.fetch_single_activity(args.activity_id, all_type_keys)
            activity_handler_pairs = (
                [(activity, _find_handler(activity.activity_type_key))] if activity else []
            )
        else:
            try:
                start_date, end_date = _resolve_date_range(args)
            except ValueError as exc:
                logger.error("%s", exc)
                return 2

            activity_handler_pairs = []
            for handler in ACTIVE_HANDLERS:
                activities = garmin.fetch_activities(
                    start_date,
                    end_date,
                    handler.garmin_type_keys,
                    max_activities=cfg.max_activities_per_run,
                )
                activity_handler_pairs.extend((activity, handler) for activity in activities)

        if not activity_handler_pairs:
            logger.info("Keine passenden Aktivitäten im gewählten Zeitraum gefunden.")
            return 0

        for activity, handler in activity_handler_pairs:
            if args.debug_dump_raw:
                raw = garmin.fetch_exercise_sets_raw(activity.activity_id)
                print(f"\n===== Rohdaten Aktivität {activity.activity_id} ({activity.start_date}) =====")
                import json

                print(json.dumps(raw, indent=2, ensure_ascii=False))
                continue

            if handler is None:
                logger.warning(
                    "Aktivität %s hat Typ '%s', für den kein Handler registriert "
                    "ist - wird übersprungen.",
                    activity.activity_id,
                    activity.activity_type_key,
                )
                skipped += 1
                continue

            if state.is_synced(activity.activity_id) and not args.force:
                logger.info(
                    "Aktivität %s (%s) bereits synchronisiert, überspringe "
                    "(--force zum erneuten Import).",
                    activity.activity_id,
                    activity.start_date,
                )
                skipped += 1
                continue

            try:
                mapped = handler.build_workout(garmin, activity, resolver)
                if mapped is None:
                    skipped += 1
                    continue

                handler.post_workout(sparky, mapped, dry_run=args.dry_run)
                logger.info(
                    "Aktivität %s (%s): %d Übung(en), %d Satz/Sätze importiert.",
                    activity.activity_id,
                    activity.start_date,
                    mapped.exercise_count,
                    mapped.set_count,
                )

                if not args.dry_run:
                    state.mark_synced(activity.activity_id, activity.name, activity.start_date)
                imported += 1

            except SparkyAPIError as exc:
                logger.error(
                    "Import von Aktivität %s fehlgeschlagen: %s", activity.activity_id, exc
                )
                failed += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Unerwarteter Fehler bei Aktivität %s: %s", activity.activity_id, exc
                )
                failed += 1

    if args.debug_dump_raw:
        return 0

    logger.info(
        "Fertig. Importiert: %d, übersprungen: %d, fehlgeschlagen: %d.",
        imported, skipped, failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
