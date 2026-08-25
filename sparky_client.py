"""
Client für die SparkyFitness REST-API.

Implementiert exakt die im PoC (`test_sparky_api.py`) verifizierte
Get-or-Create-Pipeline für Übungen sowie den POST an
``/exercise-preset-entries`` für das gebündelte Workout.

Hinweis zur Robustheit:
Die OpenAPI-Spec von SparkyFitness dokumentiert für
``POST /exercise-preset-entries`` aktuell KEIN Request-Body-Schema
("Also note that the API is subject to change without notice due to
heavy development"). Das Payload-Format hier basiert daher bewusst 1:1
auf dem funktionierenden PoC und nicht auf der (unvollständigen) Spec.
Vor produktivem Einsatz unbedingt mit ``--dry-run`` gegenpruefen.

Backend-Quirk "duration" vs. "rest_time" (live über die Sparky-Edit-UI
verifiziert, NICHT das, was der Feldname/die offizielle Doku nahelegt):
  - ``duration`` (pro Satz) wird von Sparky als MINUTEN interpretiert
    (Edit-Dialog zeigt "DURATION (MIN)"). Garmin liefert Satzdauer in
    Sekunden - SparkySet.to_payload() rechnet daher /60 um.
  - ``rest_time`` (pro Satz) ist tatsächlich Sekunden, keine Umrechnung.
Ohne diese (asymmetrische!) Umrechnung zeigte Sparky z.B. für einen
63-Sekunden-Satz "63 Minuten" an - über mehrere Sätze/Übungen hinweg
summierten sich so mehrstündige statt minütige Übungsdauern.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .gemini_client import ExerciseMetadata

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# Backend-Quirk (siehe Aufgabenstellung): primary_muscles/equipment MÜSSEN
# Strings sein, keine Arrays - das Backend ruft intern .split() darauf auf.
DEFAULT_PRIMARY_MUSCLE = "other"
DEFAULT_EQUIPMENT = "None"
DEFAULT_CATEGORY = "strength"

# Fixes, kleingeschriebenes Kategorie-Enum aus dem SparkyFitness-Frontend
# (EXERCISE_CATEGORIES in constants/exercises.ts). Nur diese Werte werden
# vom Frontend erkannt (Icon/Farbe/Übersetzung z.B. "Kraft" statt "strength")
# - alles andere wird zwar gespeichert, aber unübersetzt/unstyled angezeigt.
VALID_SPARKY_CATEGORIES = frozenset(
    {
        "general",
        "strength",
        "cardio",
        "yoga",
        "powerlifting",
        "olympic weightlifting",
        "strongman",
        "plyometrics",
        "stretching",
        "isometric",
    }
)


def _normalize_category(category: str | None) -> str:
    """Erzwingt eine gültige, in Sparky bekannte Kategorie.

    Case-insensitive Abgleich gegen ``VALID_SPARKY_CATEGORIES`` (Gemini
    liefert z.B. teils grossgeschriebene Werte). Unbekannte/fehlende Werte
    fallen auf ``DEFAULT_CATEGORY`` zurück, statt eine Kategorie zu senden,
    die es in Sparky gar nicht gibt.
    """
    normalized = (category or "").strip().lower()
    return normalized if normalized in VALID_SPARKY_CATEGORIES else DEFAULT_CATEGORY

# Backend-Quirk: duration/rest_time pro Satz sind Sekunden, müssen aber als
# Integer übertragen werden - ein Float-rest_time (z.B. "115.656") wird vom
# Backend mit "invalid input syntax for type integer" abgelehnt (live gegen
# die API verifiziert). SparkySet.to_payload() rundet deshalb beide Felder.


class SparkyAPIError(RuntimeError):
    """Fehler bei der Kommunikation mit der SparkyFitness API."""


@dataclass
class SparkySet:
    set_number: int
    reps: int | None
    weight_kg: float | None
    set_type: str = "Working Set"
    duration_seconds: float | None = None
    rest_time_seconds: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "set_number": self.set_number,
            "set_type": self.set_type,
        }
        if self.reps is not None:
            payload["reps"] = self.reps
        if self.weight_kg is not None:
            payload["weight"] = self.weight_kg
        if self.duration_seconds is not None:
            # Backend-Quirk (live über die Sparky-UI verifiziert, siehe
            # Modul-Docstring): "duration" pro Satz wird von Sparky als
            # MINUTEN behandelt (Edit-Dialog zeigt "DURATION (MIN)"),
            # obwohl der Feldname und die Zod-Doku Sekunden nahelegen.
            # "rest_time" ist dagegen tatsächlich Sekunden (keine Umrechnung).
            payload["duration"] = round(self.duration_seconds / 60.0, 2)
        if self.rest_time_seconds is not None:
            # rest_time verlangt einen Integer - Floats lehnt das Backend
            # mit einem DB-Fehler ab ("invalid input syntax for type integer").
            payload["rest_time"] = round(self.rest_time_seconds)
        return payload


class SparkyClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"x-api-key": api_key, "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._session.request(
                    method, url, timeout=self._timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(
                    "Netzwerkfehler bei %s %s (Versuch %d/%d): %s",
                    method, path, attempt, MAX_RETRIES, exc,
                )
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            # 5xx: server-seitiges, evtl. temporäres Problem -> retry
            if response.status_code >= 500:
                logger.warning(
                    "Sparky-Server-Fehler %d bei %s %s (Versuch %d/%d)",
                    response.status_code, method, path, attempt, MAX_RETRIES,
                )
                last_exc = SparkyAPIError(
                    f"{response.status_code} {response.text[:300]}"
                )
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            return response

        raise SparkyAPIError(
            f"{method} {path} nach {MAX_RETRIES} Versuchen fehlgeschlagen: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Get-or-Create Übungen
    # ------------------------------------------------------------------
    def search_exercise_exact(self, exercise_name: str) -> dict[str, Any] | None:
        """Sucht eine Übung per Name, gibt bei exaktem Treffer das Objekt zurück."""
        response = self._request(
            "GET", "/exercises/search", params={"searchTerm": exercise_name}
        )
        if response.status_code != 200:
            raise SparkyAPIError(
                f"Exercise-Suche fehlgeschlagen ({response.status_code}): "
                f"{response.text[:300]}"
            )

        for candidate in response.json():
            if candidate.get("name", "").strip().lower() == exercise_name.strip().lower():
                return candidate
        return None

    def create_exercise(
        self,
        exercise_name: str,
        metadata: ExerciseMetadata | None = None,
    ) -> None:
        """Legt eine neue Übung an.

        ``metadata`` stammt im Regelfall aus einer Gemini-Anreicherung (siehe
        gemini_client.py / mapper.py). Ist keine Metadata verfügbar (Gemini
        nicht konfiguriert oder fehlgeschlagen), wird auf die alten,
        generischen Platzhalter-Werte zurückgefallen.

        WICHTIG: primary_muscles/equipment muessen laut Backend-Quirk Strings
        sein, keine Arrays - siehe Modul-Docstring.
        """
        if metadata is not None:
            category = _normalize_category(metadata.category)
            equipment = metadata.equipment
            primary_muscles = metadata.primary_muscles
            muscle_groups = metadata.primary_muscles
            description = metadata.description or "Automatisch via Garmin-Sync angelegt."
        else:
            category = DEFAULT_CATEGORY
            equipment = DEFAULT_EQUIPMENT
            primary_muscles = DEFAULT_PRIMARY_MUSCLE
            muscle_groups = "Other"
            description = "Automatisch via Garmin-Sync angelegt."

        exercise_payload: dict[str, Any] = {
            "name": exercise_name,
            "category": category,
            "equipment": equipment,
            "muscle_groups": muscle_groups,
            "primary_muscles": primary_muscles,
            "description": description,
            "is_public": False,
        }
        if metadata is not None:
            exercise_payload.update(
                {
                    "secondary_muscles": metadata.secondary_muscles,
                    "force": metadata.force,
                    "level": metadata.level,
                    "mechanic": metadata.mechanic,
                    "instructions": metadata.instructions,
                }
            )

        payload = {"exercises": [exercise_payload]}
        response = self._request("POST", "/exercises/import-json", json=payload)
        if response.status_code not in (200, 201):
            raise SparkyAPIError(
                f"Anlegen der Übung '{exercise_name}' fehlgeschlagen "
                f"({response.status_code}): {response.text[:300]}"
            )
        logger.info("Übung '%s' in SparkyFitness neu angelegt.", exercise_name)

    def get_or_create_exercise_id(
        self,
        exercise_name: str,
        metadata_provider: Callable[[], ExerciseMetadata | None] | None = None,
    ) -> str:
        """Get-or-Create Pipeline (3 Schritte).

        ``metadata_provider`` wird - falls angegeben - nur dann aufgerufen,
        wenn die Übung tatsächlich neu angelegt werden muss (lazy), um
        unnötige Gemini-Anfragen für bereits existierende Übungen zu
        vermeiden.
        """
        existing = self.search_exercise_exact(exercise_name)
        if existing:
            return existing["id"]

        metadata = metadata_provider() if metadata_provider is not None else None

        logger.info("Übung '%s' nicht gefunden, lege sie neu an...", exercise_name)
        self.create_exercise(exercise_name, metadata)

        # Schritt 3: erneut suchen, um die frisch generierte UUID zu holen.
        created = self.search_exercise_exact(exercise_name)
        if not created:
            raise SparkyAPIError(
                f"Übung '{exercise_name}' wurde angelegt, ist danach aber nicht "
                f"per Suche auffindbar - evtl. Indexierungsverzögerung im Backend."
            )
        return created["id"]

    # ------------------------------------------------------------------
    # Workout-Import
    # ------------------------------------------------------------------
    def post_workout(
        self,
        name: str,
        entry_date: str,
        exercises_payload: list[dict[str, Any]],
        source: str = "Garmin Connect",
        dry_run: bool = False,
    ) -> dict[str, Any] | None:
        """POST /exercise-preset-entries - erstellt eine gebündelte Session."""
        payload = {
            "name": name,
            "entry_date": entry_date,
            "source": source,
            "exercises": exercises_payload,
        }

        if dry_run:
            logger.info(
                "[DRY-RUN] Würde folgendes Workout an Sparky senden:\n%s",
                _pretty(payload),
            )
            return None

        response = self._request("POST", "/exercise-preset-entries", json=payload)
        if response.status_code not in (200, 201):
            raise SparkyAPIError(
                f"Workout-Import fehlgeschlagen ({response.status_code}): "
                f"{response.text[:500]}"
            )
        logger.info("Workout '%s' (%s) erfolgreich nach Sparky importiert.", name, entry_date)
        try:
            return response.json()
        except ValueError:
            return None


def _pretty(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, ensure_ascii=False)
