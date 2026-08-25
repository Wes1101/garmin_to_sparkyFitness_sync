"""
Verbindet ``garmin_client`` und ``sparky_client``: gruppiert die flache
Satzliste einer Garmin-Aktivität nach Übung und baut daraus den
Payload für ``POST /exercise-preset-entries``.

Die Exercise-ID-Auflösung nutzt zweistufiges Caching:
  1. In-Memory-Dict für den aktuellen Lauf (vermeidet mehrfache Sparky-
     Anfragen innerhalb derselben Aktivität/desselben Batches).
  2. Persistenter ``SyncState``-Cache auf der Platte (vermeidet erneute
     Sparky-Anfragen über mehrere CLI-Läufe hinweg, z.B. im Cronjob).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .garmin_client import StrengthActivity, WorkoutSet
from .gemini_client import GeminiClient
from .sparky_client import SparkyClient, SparkySet
from .state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class MappedWorkout:
    name: str
    entry_date: str
    exercises_payload: list[dict[str, Any]]
    exercise_count: int
    set_count: int


class ExerciseIdResolver:
    """Kapselt Get-or-Create inkl. zweistufigem Cache.

    Entspricht funktional dem ``EXERCISE_CACHE`` aus dem PoC-Skript,
    ergänzt um Persistenz über ``SyncState`` sowie optionale
    Gemini-Anreicherung neu angelegter Übungen (siehe gemini_client.py).
    """

    def __init__(
        self,
        sparky: SparkyClient,
        state: SyncState,
        gemini: GeminiClient | None = None,
    ):
        self._sparky = sparky
        self._state = state
        self._gemini = gemini
        self._run_cache: dict[str, str] = {}

    def is_ignored(self, exercise_name: str) -> bool:
        """Prüft, ob eine Übung auf der Ignore-Liste steht (siehe
        ``cli.py --ignore-add/--ignore-list``)."""
        return self._state.is_ignored(exercise_name.strip().lower())

    def resolve(self, exercise_name: str) -> str:
        key = exercise_name.strip().lower()

        if key in self._run_cache:
            return self._run_cache[key]

        cached_id = self._state.get_exercise_id(key)
        if cached_id:
            self._run_cache[key] = cached_id
            return cached_id

        if self._gemini is not None:
            exercise_id = self._sparky.get_or_create_exercise_id(
                exercise_name, metadata_provider=self._make_metadata_provider(exercise_name)
            )
        else:
            exercise_id = self._sparky.get_or_create_exercise_id(exercise_name)

        self._run_cache[key] = exercise_id
        self._state.set_exercise_id(key, exercise_id)
        return exercise_id

    def _make_metadata_provider(self, exercise_name: str):
        """Baut einen lazy Provider, der Gemini erst beim tatsächlichen
        Neuanlegen einer Übung aufruft und Fehler auf Default-Metadaten
        zurückfallen lässt (Sync soll nicht an einer Gemini-Störung
        scheitern)."""

        def provider():
            try:
                return self._gemini.enrich_exercise(exercise_name)
            except Exception as exc:  # noqa: BLE001 - Gemini darf den Sync nie blockieren
                logger.warning(
                    "Gemini-Anfrage für '%s' fehlgeschlagen (%s), nutze "
                    "Standard-Metadaten.",
                    exercise_name,
                    exc,
                )
                return None

        return provider


def group_sets_by_exercise(sets: list[WorkoutSet]) -> dict[str, list[WorkoutSet]]:
    """Gruppiert Sätze nach Übungsname, Reihenfolge des ersten Auftretens bleibt erhalten."""
    grouped: dict[str, list[WorkoutSet]] = {}
    for workout_set in sets:
        grouped.setdefault(workout_set.exercise_name, []).append(workout_set)
    return grouped


def build_workout_payload(
    activity: StrengthActivity,
    sets: list[WorkoutSet],
    resolver: ExerciseIdResolver,
) -> MappedWorkout | None:
    """Baut den vollständigen Payload für ein Garmin-Krafttraining.

    Gibt None zurück, wenn die Aktivität keine auswertbaren (aktiven)
    Sätze enthält - z.B. weil Garmin für dieses Training keine
    Satz-Details erfasst hat (reine manuelle Aktivität ohne Struktur).
    """
    if not sets:
        logger.warning(
            "Aktivität %s (%s) hat keine aktiven Sätze - wird übersprungen.",
            activity.activity_id,
            activity.start_date,
        )
        return None

    grouped = group_sets_by_exercise(sets)
    exercises_payload: list[dict[str, Any]] = []

    for exercise_name, exercise_sets in grouped.items():
        if resolver.is_ignored(exercise_name):
            logger.info(
                "Übung '%s' steht auf der Ignore-Liste, wird übersprungen.",
                exercise_name,
            )
            continue

        try:
            exercise_id = resolver.resolve(exercise_name)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Konnte Exercise-ID für '%s' nicht auflösen, Übung wird "
                "übersprungen: %s",
                exercise_name,
                exc,
            )
            continue

        sparky_sets = [
            SparkySet(
                set_number=s.set_number,
                reps=s.reps,
                weight_kg=s.weight_kg,
                set_type=s.set_type,
                duration_seconds=s.duration_seconds,
                rest_time_seconds=s.rest_time_seconds,
            )
            for s in exercise_sets
        ]

        # sort_order haelt die Original-Reihenfolge aus Garmin fest (sonst
        # landen bei Sparky alle Übungen mit dem Default sort_order=0 und
        # die Anzeige-Reihenfolge stimmt nicht mehr). len(exercises_payload)
        # ist an dieser Stelle lückenlos, da ignorierte/fehlgeschlagene
        # Übungen oben per `continue` gar nicht erst angehängt werden.
        sort_order = len(exercises_payload)

        # Summe aus Satz- und Pausenzeit (Sekunden) in Minuten, damit die von
        # Sparky gespeicherte/angezeigte Übungs- und Workout-Gesamtdauer der
        # echten Trainingszeit entspricht statt auf 0 zu defaulten.
        duration_minutes = round(
            sum((s.duration_seconds or 0) + (s.rest_time_seconds or 0) for s in exercise_sets)
            / 60.0,
            2,
        )

        exercises_payload.append(
            {
                "exercise_id": exercise_id,
                "sort_order": sort_order,
                "duration_minutes": duration_minutes,
                # Explizit 0 statt das Feld wegzulassen: Sparky würde sonst
                # calories_burned server-seitig aus duration_minutes und dem
                # (teils geschätzten) calories_per_hour der Übung berechnen.
                # Kalorien werden hier bewusst NICHT synchronisiert - der
                # Nutzer trackt sie über eine andere Quelle.
                "calories_burned": 0,
                "sets": [sparky_set.to_payload() for sparky_set in sparky_sets],
            }
        )

    if not exercises_payload:
        logger.warning(
            "Aktivität %s: keine Übung übrig (ignoriert oder ID-Auflösung "
            "fehlgeschlagen), Workout wird nicht importiert.",
            activity.activity_id,
        )
        return None

    return MappedWorkout(
        name=activity.name,
        entry_date=activity.start_date,
        exercises_payload=exercises_payload,
        exercise_count=len(exercises_payload),
        set_count=len(sets),
    )
