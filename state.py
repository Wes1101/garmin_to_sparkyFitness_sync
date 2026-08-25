"""
Persistenter lokaler Zustand.

Zwei Dinge werden über Programmläufe hinweg auf der Platte gehalten:

1. ``synced_activities.json``
   Mapping von bereits erfolgreich nach SparkyFitness übertragener
   Garmin-``activityId`` -> ``{"name": ..., "date": ...}``. Ohne das würde
   z.B. ein taeglich laufender ``--timeframe 7days``-Cronjob dieselben
   Workouts jedes Mal erneut anlegen, da SparkyFitness selbst keine
   Duplikatspruefung ueber ``/exercise-preset-entries`` anbietet. Name/Datum
   sind nur fuer die menschliche Anzeige (``--cache-show``) da, nicht fuer
   die Idempotenz-Logik (die läuft ausschliesslich über die activityId).
   Aeltere State-Dateien im alten Format (reine Liste von IDs ohne
   Name/Datum) werden beim Laden automatisch migriert.

2. ``exercise_id_cache.json``
   Mapping von (normalisiertem) Übungsnamen -> Sparky exercise UUID.
   Ergänzt den In-Memory-Cache aus mapper.py/sparky_client.py, damit
   nicht bei jedem Lauf erneut nach bereits bekannten Übungen gesucht
   werden muss.

3. ``ignored_exercises.json``
   Menge (normalisierter) Übungsnamen, die beim Sync komplett übersprungen
   werden sollen (z.B. von der Garmin-Watch automatisch erkannte
   Aufwärm-/Pausenübungen wie "Arm Circles"). Wird über
   ``cli.py --ignore-add/--ignore-remove/--ignore-list`` verwaltet.

Das Format ist bewusst simpel gehalten (JSON-Dateien, keine DB), da das
Tool als CLI-Cronjob und nicht als Dauer-Service laeuft.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class SyncState:
    def __init__(self, state_dir: Path):
        self._state_dir = state_dir
        self._synced_path = state_dir / "synced_activities.json"
        self._exercise_cache_path = state_dir / "exercise_id_cache.json"
        self._ignored_path = state_dir / "ignored_exercises.json"
        self._lock = Lock()

        self._synced_activities: dict[str, dict[str, str]] = self._load_synced(
            self._synced_path
        )
        self._exercise_id_cache: dict[str, str] = self._load_json_dict(
            self._exercise_cache_path
        )
        self._ignored_exercises: set[str] = self._load_json_set(self._ignored_path)

    # ------------------------------------------------------------------
    # Laden/Speichern
    # ------------------------------------------------------------------
    @staticmethod
    def _load_json_set(path: Path) -> set[str]:
        if not path.exists():
            return set()
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Konnte %s nicht laden (%s), starte mit leerem State.", path, exc)
            return set()

    @staticmethod
    def _load_json_dict(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Konnte %s nicht laden (%s), starte mit leerem Cache.", path, exc)
            return {}

    @staticmethod
    def _load_synced(path: Path) -> dict[str, dict[str, str]]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Konnte %s nicht laden (%s), starte mit leerem State.", path, exc)
            return {}

        if isinstance(data, list):
            # Altes Format (reine Liste von activityIds ohne Name/Datum) -
            # automatische Migration, Name/Datum bleiben unbekannt.
            return {str(activity_id): {"name": "", "date": ""} for activity_id in data}

        if isinstance(data, dict):
            return {
                str(activity_id): {
                    "name": info.get("name", ""),
                    "date": info.get("date", ""),
                }
                for activity_id, info in data.items()
            }

        logger.warning("Unerwartetes Format in %s, starte mit leerem State.", path)
        return {}

    def _atomic_write(self, path: Path, data) -> None:
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)

    def flush(self) -> None:
        with self._lock:
            self._atomic_write(self._synced_path, dict(sorted(self._synced_activities.items())))
            self._atomic_write(self._exercise_cache_path, self._exercise_id_cache)
            self._atomic_write(self._ignored_path, sorted(self._ignored_exercises))

    # ------------------------------------------------------------------
    # Synced-Activities
    # ------------------------------------------------------------------
    def is_synced(self, garmin_activity_id: str) -> bool:
        return str(garmin_activity_id) in self._synced_activities

    def mark_synced(self, garmin_activity_id: str, name: str = "", entry_date: str = "") -> None:
        with self._lock:
            self._synced_activities[str(garmin_activity_id)] = {
                "name": name,
                "date": entry_date,
            }

    # ------------------------------------------------------------------
    # Exercise-ID-Cache
    # ------------------------------------------------------------------
    def get_exercise_id(self, exercise_name_key: str) -> str | None:
        return self._exercise_id_cache.get(exercise_name_key)

    def set_exercise_id(self, exercise_name_key: str, exercise_id: str) -> None:
        with self._lock:
            self._exercise_id_cache[exercise_name_key] = exercise_id

    def list_exercise_cache(self) -> dict[str, str]:
        return dict(self._exercise_id_cache)

    def clear_exercise_cache(self) -> int:
        with self._lock:
            count = len(self._exercise_id_cache)
            self._exercise_id_cache.clear()
            return count

    # ------------------------------------------------------------------
    # Synced-Activities: Anzeige/Löschen
    # ------------------------------------------------------------------
    def list_synced(self) -> list[tuple[str, str, str]]:
        """Gibt ``(activity_id, name, date)``-Tupel zurück, sortiert nach
        Datum (dann activity_id). Name/Datum sind leer, falls aus einer
        alten State-Datei ohne diese Angaben migriert."""
        items = [
            (activity_id, info.get("name", ""), info.get("date", ""))
            for activity_id, info in self._synced_activities.items()
        ]
        items.sort(key=lambda item: (item[2], item[0]))
        return items

    def clear_synced(self) -> int:
        with self._lock:
            count = len(self._synced_activities)
            self._synced_activities.clear()
            return count

    # ------------------------------------------------------------------
    # Ignore-Liste
    # ------------------------------------------------------------------
    def is_ignored(self, exercise_name_key: str) -> bool:
        return exercise_name_key in self._ignored_exercises

    def add_ignored(self, exercise_name_key: str) -> bool:
        """Fügt eine Übung zur Ignore-Liste hinzu. Gibt False zurück, falls
        sie bereits enthalten war (dann keine Änderung)."""
        with self._lock:
            if exercise_name_key in self._ignored_exercises:
                return False
            self._ignored_exercises.add(exercise_name_key)
            return True

    def remove_ignored(self, exercise_name_key: str) -> bool:
        """Entfernt eine Übung von der Ignore-Liste. Gibt False zurück, falls
        sie nicht enthalten war."""
        with self._lock:
            if exercise_name_key not in self._ignored_exercises:
                return False
            self._ignored_exercises.discard(exercise_name_key)
            return True

    def list_ignored(self) -> list[str]:
        return sorted(self._ignored_exercises)

    def __enter__(self) -> "SyncState":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # State auch bei Fehlern sichern, damit bereits erfolgreich
        # importierte Workouts nicht erneut verarbeitet werden.
        self.flush()
