"""
Unit-Tests für mapper.py.

Laufen komplett offline: Garmin-, Sparky- und Gemini-Aufrufe werden gemockt.
Ausführen aus dem übergeordneten Verzeichnis mit:
    python -m unittest garmin_sparky_sync.test_mapper -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_sparky_sync.garmin_client import StrengthActivity, WorkoutSet
from garmin_sparky_sync.gemini_client import ExerciseMetadata, GeminiError
from garmin_sparky_sync.mapper import ExerciseIdResolver, build_workout_payload
from garmin_sparky_sync.sparky_client import _normalize_category
from garmin_sparky_sync.state import SyncState


class TestGroupingAndMapping(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state = SyncState(Path(self._tmpdir.name))
        self.sparky = MagicMock()
        self.resolver = ExerciseIdResolver(self.sparky, self.state)

        self.activity = StrengthActivity(
            activity_id="123",
            name="Pull Day (Oberkörper)",
            start_date="2026-07-29",
            activity_type_key="strength_training",
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_groups_sets_by_exercise_and_resolves_ids(self):
        self.sparky.get_or_create_exercise_id.side_effect = lambda name: {
            "Klimmzug": "uuid-klimmzug",
            "Rudern": "uuid-rudern",
        }[name]

        sets = [
            WorkoutSet("Klimmzug", 1, "Working Set", 10, 0.0, 30, 60),
            WorkoutSet("Klimmzug", 2, "Working Set", 8, 0.0, 28, 60),
            WorkoutSet("Rudern", 1, "Working Set", 12, 50.0, 35, 90),
        ]

        mapped = build_workout_payload(self.activity, sets, self.resolver)

        self.assertIsNotNone(mapped)
        self.assertEqual(mapped.exercise_count, 2)
        self.assertEqual(mapped.set_count, 3)
        self.assertEqual(mapped.name, "Pull Day (Oberkörper)")
        self.assertEqual(mapped.entry_date, "2026-07-29")

        klimmzug = next(
            e for e in mapped.exercises_payload if e["exercise_id"] == "uuid-klimmzug"
        )
        rudern = next(
            e for e in mapped.exercises_payload if e["exercise_id"] == "uuid-rudern"
        )
        self.assertEqual(len(klimmzug["sets"]), 2)
        self.assertEqual(klimmzug["sets"][0]["set_number"], 1)
        self.assertEqual(klimmzug["sets"][0]["reps"], 10)
        self.assertEqual(klimmzug["sets"][0]["set_type"], "Working Set")
        # Backend-Quirk: Sparky behandelt "duration" pro Satz als Minuten
        # (30s Garmin-Dauer -> 0.5), "rest_time" bleibt Sekunden (gerundet).
        self.assertAlmostEqual(klimmzug["sets"][0]["duration"], 0.5)
        self.assertEqual(klimmzug["sets"][0]["rest_time"], 60)

        # sort_order muss die Original-Reihenfolge aus Garmin widerspiegeln
        # (Klimmzug kommt vor Rudern in der Eingabe-Liste).
        self.assertEqual(klimmzug["sort_order"], 0)
        self.assertEqual(rudern["sort_order"], 1)

        # duration_minutes = Summe aus duration+rest_time (Sekunden) / 60,
        # auf 2 Nachkommastellen gerundet.
        self.assertAlmostEqual(klimmzug["duration_minutes"], (30 + 60 + 28 + 60) / 60.0, places=2)
        self.assertAlmostEqual(rudern["duration_minutes"], (35 + 90) / 60.0, places=2)

        # Kalorien werden bewusst nicht synchronisiert (User trackt sie
        # anderweitig) - explizit 0 statt Sparkys Auto-Schätzung zuzulassen.
        self.assertEqual(klimmzug["calories_burned"], 0)
        self.assertEqual(rudern["calories_burned"], 0)

        # Get-or-Create darf pro Übung nur einmal pro Lauf aufgerufen werden
        # (In-Memory-Cache im Resolver).
        self.assertEqual(self.sparky.get_or_create_exercise_id.call_count, 2)

    def test_exercise_id_cache_persists_across_resolver_calls(self):
        self.sparky.get_or_create_exercise_id.return_value = "uuid-klimmzug"

        self.resolver.resolve("Klimmzug")
        self.resolver.resolve("Klimmzug")
        self.resolver.resolve("klimmzug")  # Case-insensitive

        self.sparky.get_or_create_exercise_id.assert_called_once_with("Klimmzug")

    def test_empty_sets_returns_none(self):
        mapped = build_workout_payload(self.activity, [], self.resolver)
        self.assertIsNone(mapped)

    def test_failed_id_resolution_skips_only_that_exercise(self):
        def side_effect(name):
            if name == "Kaputte Übung":
                raise RuntimeError("Sparky down")
            return "uuid-ok"

        self.sparky.get_or_create_exercise_id.side_effect = side_effect

        sets = [
            WorkoutSet("Kaputte Übung", 1, "Working Set", 10, 20.0, 30, 60),
            WorkoutSet("Rudern", 1, "Working Set", 12, 50.0, 35, 90),
        ]

        mapped = build_workout_payload(self.activity, sets, self.resolver)

        self.assertIsNotNone(mapped)
        self.assertEqual(mapped.exercise_count, 1)
        self.assertEqual(mapped.exercises_payload[0]["exercise_id"], "uuid-ok")

    def test_ignored_exercise_is_skipped_without_sparky_call(self):
        self.state.add_ignored("arm circles")
        self.sparky.get_or_create_exercise_id.return_value = "uuid-rudern"

        sets = [
            WorkoutSet("Arm Circles", 1, "Warmup", 15, None, 20, 10),
            WorkoutSet("Rudern", 1, "Working Set", 12, 50.0, 35, 90),
        ]

        mapped = build_workout_payload(self.activity, sets, self.resolver)

        self.assertIsNotNone(mapped)
        self.assertEqual(mapped.exercise_count, 1)
        self.assertEqual(mapped.exercises_payload[0]["exercise_id"], "uuid-rudern")
        # Für die ignorierte Übung darf gar keine Sparky-Anfrage erfolgen.
        self.sparky.get_or_create_exercise_id.assert_called_once_with("Rudern")

    def test_all_exercises_ignored_returns_none(self):
        self.state.add_ignored("arm circles")
        sets = [WorkoutSet("Arm Circles", 1, "Warmup", 15, None, 20, 10)]

        mapped = build_workout_payload(self.activity, sets, self.resolver)

        self.assertIsNone(mapped)
        self.sparky.get_or_create_exercise_id.assert_not_called()


class TestGeminiFallback(unittest.TestCase):
    """Neu angelegte Übungen sollen bei Gemini-Fehlern auf die alten
    Default-Metadaten zurückfallen statt den Sync abzubrechen."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state = SyncState(Path(self._tmpdir.name))
        self.sparky = MagicMock()
        self.gemini = MagicMock()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_metadata_provider_falls_back_to_none_on_gemini_error(self):
        self.gemini.enrich_exercise.side_effect = GeminiError("Kein API-Key")
        self.sparky.get_or_create_exercise_id.return_value = "uuid-neu"
        resolver = ExerciseIdResolver(self.sparky, self.state, gemini=self.gemini)

        exercise_id = resolver.resolve("Dumbbell Hammer Curl")

        self.assertEqual(exercise_id, "uuid-neu")
        _, kwargs = self.sparky.get_or_create_exercise_id.call_args
        provider = kwargs["metadata_provider"]
        self.assertIsNone(provider())  # Fehler wird abgefangen, kein Crash.

    def test_metadata_provider_returns_gemini_metadata_on_success(self):
        metadata = ExerciseMetadata(
            category="Strength", equipment="Dumbbell", primary_muscles="Biceps",
            secondary_muscles="Forearms", force="pull", level="beginner",
            mechanic="isolation", instructions="1. ...", description="...",
        )
        self.gemini.enrich_exercise.return_value = metadata
        self.sparky.get_or_create_exercise_id.return_value = "uuid-neu"
        resolver = ExerciseIdResolver(self.sparky, self.state, gemini=self.gemini)

        resolver.resolve("Dumbbell Hammer Curl")

        _, kwargs = self.sparky.get_or_create_exercise_id.call_args
        provider = kwargs["metadata_provider"]
        self.assertEqual(provider(), metadata)


class TestCategoryNormalization(unittest.TestCase):
    """Sparky kennt nur ein festes, kleingeschriebenes Kategorie-Enum
    (EXERCISE_CATEGORIES im Frontend) - alles andere wird zwar gespeichert,
    aber im UI nicht übersetzt/gestylt angezeigt."""

    def test_known_category_is_lowercased(self):
        self.assertEqual(_normalize_category("Strength"), "strength")
        self.assertEqual(_normalize_category("olympic weightlifting"), "olympic weightlifting")

    def test_unknown_category_falls_back_to_default(self):
        self.assertEqual(_normalize_category("Bodybuilding"), "strength")
        self.assertEqual(_normalize_category(None), "strength")
        self.assertEqual(_normalize_category(""), "strength")


if __name__ == "__main__":
    unittest.main()
