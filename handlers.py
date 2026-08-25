"""
Erweiterungspunkt für zukünftige Trainingsarten (Laufen, Radfahren, ...).

Aktuell ist nur Krafttraining (``StrengthTrainingHandler``) aktiv registriert
und wird von ``cli.py`` verarbeitet. Um eine weitere Trainingsart zu
unterstützen:

  1. Eine neue Klasse analog zu ``StrengthTrainingHandler`` implementieren,
     die von ``ActivityTypeHandler`` erbt (``garmin_type_keys`` setzen,
     ``build_workout``/``post_workout`` implementieren).
  2. Eine Instanz in ``ACTIVE_HANDLERS`` eintragen.

``cli.py`` iteriert generisch über ``ACTIVE_HANDLERS`` - dort muss für eine
neue Trainingsart nichts angepasst werden. ``garmin_client.GarminClient``
bietet dafür bereits eine generische ``fetch_activities(..., type_keys=...)``
Methode statt der Krafttraining-spezifischen ``fetch_strength_activities``.

Hinweis: Andere Trainingsarten benötigen im Regelfall einen eigenen
Mapper/Payload-Aufbau (z.B. Distanz/Pace statt Sätzen/Gewicht) sowie ggf.
einen anderen Sparky-Endpoint als ``/exercise-preset-entries`` - das ist
bewusst noch nicht implementiert, nur die Struktur dafür ist vorbereitet.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .garmin_client import GarminClient, StrengthActivity
from .mapper import ExerciseIdResolver, MappedWorkout, build_workout_payload
from .sparky_client import SparkyClient


class ActivityTypeHandler(ABC):
    """Kapselt alles Trainingsart-Spezifische: welche Garmin-Aktivitäten
    zählen, wie Detaildaten geholt werden, und wie daraus ein Sparky-Payload
    gebaut und übertragen wird."""

    #: Menschenlesbarer Schlüssel, z.B. "strength_training".
    key: str

    #: Garmin `activityType.typeKey`-Werte, die dieser Handler verarbeitet.
    garmin_type_keys: frozenset[str]

    @abstractmethod
    def build_workout(
        self,
        garmin: GarminClient,
        activity: StrengthActivity,
        resolver: ExerciseIdResolver,
    ) -> MappedWorkout | None:
        """Holt Detaildaten für ``activity`` und baut daraus den Sparky-Payload.

        Gibt ``None`` zurück, wenn die Aktivität nichts Importierbares
        enthält (Aktivität wird dann als "übersprungen" gezählt).
        """

    @abstractmethod
    def post_workout(
        self,
        sparky: SparkyClient,
        mapped: MappedWorkout,
        dry_run: bool,
    ) -> Any:
        """Überträgt den gemappten Payload nach SparkyFitness."""


class StrengthTrainingHandler(ActivityTypeHandler):
    """Aktuell einziger aktiver Handler - Krafttraining über
    ``/exercise-preset-entries``, wie im Rest des Projekts beschrieben."""

    key = "strength_training"
    garmin_type_keys = frozenset({"strength_training", "gym"})

    def build_workout(
        self,
        garmin: GarminClient,
        activity: StrengthActivity,
        resolver: ExerciseIdResolver,
    ) -> MappedWorkout | None:
        sets = garmin.fetch_exercise_sets(activity.activity_id)
        return build_workout_payload(activity, sets, resolver)

    def post_workout(
        self,
        sparky: SparkyClient,
        mapped: MappedWorkout,
        dry_run: bool,
    ) -> Any:
        return sparky.post_workout(
            name=mapped.name,
            entry_date=mapped.entry_date,
            exercises_payload=mapped.exercises_payload,
            dry_run=dry_run,
        )


#: Aktive Handler, in der Reihenfolge, in der Aktivitäten abgefragt/verarbeitet
#: werden. Neue Trainingsarten hier ergänzen, sobald implementiert.
ACTIVE_HANDLERS: list[ActivityTypeHandler] = [StrengthTrainingHandler()]
