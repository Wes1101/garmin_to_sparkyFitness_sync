"""
Client für die Google Gemini API zur automatischen Anreicherung neu
angelegter SparkyFitness-Übungen mit Metadaten (Muskelgruppe, Equipment,
Kategorie, ...).

Wird von ``mapper.py`` ausschliesslich dann aufgerufen, wenn eine Übung in
SparkyFitness tatsächlich neu angelegt werden muss (nicht bei bereits
existierenden Übungen) - siehe ``ExerciseIdResolver.resolve``.

Schlägt die Anfrage fehl (kein API-Key, Netzwerkfehler, HTTP-Fehler,
unerwartete/unvollständige Antwort), wirft diese Klasse ``GeminiError``.
Der Aufrufer (mapper.py) fängt das ab und fällt auf die alten,
generischen Platzhalter-Werte zurück - der Sync läuft dann trotzdem weiter.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5"
API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
REQUEST_TIMEOUT_SECONDS = 20

# Feste, kleingeschriebene Enums - 1:1 aus dem SparkyFitness-Frontend
# übernommen (EXERCISE_CATEGORIES bzw. dropdownOptions in
# constants/exercises.ts). Nur diese Werte werden von Sparkys UI erkannt
# (Icon/Farbe/Übersetzung, z.B. "strength" -> "Kraft" im deutschen UI) -
# alles andere wird zwar gespeichert, aber unübersetzt/unstyled angezeigt.
# equipment/primary_muscles/secondary_muscles sind dagegen freier Text ohne
# Enum auf Sparky-Seite.
_CATEGORY_ENUM = [
    "general", "strength", "cardio", "yoga", "powerlifting",
    "olympic weightlifting", "strongman", "plyometrics", "stretching",
    "isometric",
]
_FORCE_ENUM = ["push", "pull", "static"]
_LEVEL_ENUM = ["beginner", "intermediate", "expert"]
_MECHANIC_ENUM = ["compound", "isolation"]

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING", "enum": _CATEGORY_ENUM},
        "equipment": {"type": "STRING"},
        "primary_muscles": {"type": "STRING"},
        "secondary_muscles": {"type": "STRING"},
        "force": {"type": "STRING", "enum": _FORCE_ENUM},
        "level": {"type": "STRING", "enum": _LEVEL_ENUM},
        "mechanic": {"type": "STRING", "enum": _MECHANIC_ENUM},
        "instructions": {"type": "STRING"},
        "description": {"type": "STRING"},
    },
    "required": [
        "category", "equipment", "primary_muscles", "secondary_muscles",
        "force", "level", "mechanic", "instructions", "description",
    ],
}

_PROMPT_TEMPLATE = """Du bist ein Fitness-Experte. Fülle die Metadaten für die \
Kraftsport-Übung "{exercise_name}" aus, wie sie typischerweise in einer \
Übungsdatenbank (im Stil von free-exercise-db) erfasst würde.

Regeln:
- category/force/level/mechanic: MÜSSEN exakt einer der vorgegebenen \
Enum-Werte sein (siehe Schema) - diese sind feste, in SparkyFitness \
hinterlegte Werte und dürfen NICHT übersetzt oder abgewandelt werden.
- equipment: das hauptsächlich benötigte Trainingsgerät, ein einzelner \
kurzer Begriff auf Deutsch (z.B. "Kurzhantel", "Langhantel", "Kabelzug", \
"Körpergewicht", "Maschine").
- primary_muscles / secondary_muscles: Muskelgruppe(n) auf Deutsch, bei \
mehreren kommagetrennt in einem einzigen String (z.B. "Bizeps, Unterarme"). \
secondary_muscles kann ein leerer String sein, falls nicht zutreffend.
- instructions: eine kurze, nummerierte Schritt-für-Schritt-Anleitung auf \
Deutsch als einzelner String.
- description: ein bis zwei Sätze Beschreibung der Übung auf Deutsch.

Antworte ausschliesslich mit den angeforderten Feldern."""


class GeminiError(RuntimeError):
    """Fehler bei der Kommunikation mit der Gemini API oder beim Parsen der Antwort."""


@dataclass(frozen=True)
class ExerciseMetadata:
    category: str
    equipment: str
    primary_muscles: str
    secondary_muscles: str
    force: str
    level: str
    mechanic: str
    instructions: str
    description: str


class GeminiClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self._api_key = api_key
        self._model = model

    def enrich_exercise(self, exercise_name: str) -> ExerciseMetadata:
        """Fragt Gemini nach vollständigen Metadaten für eine neue Übung.

        Wirft ``GeminiError`` bei jedem Problem (Netzwerk, HTTP-Fehler,
        unerwartete/unvollständige Antwort) - der Aufrufer entscheidet, wie
        damit umgegangen wird (siehe mapper.py: Fallback auf Defaults).
        """
        url = f"{API_BASE_URL}/{self._model}:generateContent"
        payload = {
            "contents": [
                {"parts": [{"text": _PROMPT_TEMPLATE.format(exercise_name=exercise_name)}]}
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }

        try:
            response = requests.post(
                url,
                params={"key": self._api_key},
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise GeminiError(f"Netzwerkfehler bei Gemini-Anfrage: {exc}") from exc

        if response.status_code != 200:
            raise GeminiError(
                f"Gemini-API-Fehler ({response.status_code}): {response.text[:300]}"
            )

        try:
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            fields = json.loads(text)
            metadata = ExerciseMetadata(
                category=fields["category"],
                equipment=fields["equipment"],
                primary_muscles=fields["primary_muscles"],
                secondary_muscles=fields.get("secondary_muscles", ""),
                force=fields["force"],
                level=fields["level"],
                mechanic=fields["mechanic"],
                instructions=fields.get("instructions", ""),
                description=fields.get("description", ""),
            )
        except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GeminiError(f"Unerwartete Gemini-Antwort: {exc}") from exc

        logger.info(
            "Gemini-Metadaten für '%s' erhalten (equipment=%s, primary_muscles=%s).",
            exercise_name, metadata.equipment, metadata.primary_muscles,
        )
        return metadata
