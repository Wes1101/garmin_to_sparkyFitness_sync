"""
Dünner Wrapper um die ``garminconnect``-Bibliothek.

Orientiert sich an den Authentifizierungs-/Abruf-Mustern der offiziellen
``demo.py`` von cyberjunky/python-garminconnect:
  https://github.com/cyberjunky/python-garminconnect

Wichtig:
- Sessions werden über den in ``garminconnect`` (>=0.3) eingebauten
  Tokenstore auf der Platte zwischengespeichert (``state_dir/garmin_tokens``),
  damit nicht bei jedem Cronjob-Lauf ein komplett neuer Login (inkl.
  möglicher MFA-Abfrage) nötig ist.
- Die genaue JSON-Struktur von ``get_activity_exercise_sets`` ist von
  Garmin nicht öffentlich dokumentiert und kann sich ändern. Das Parsing
  in ``parse_exercise_sets`` ist daher defensiv (überall ``.get()`` mit
  Fallbacks) und über ``--debug-dump-raw`` in der CLI überprüfbar,
  BEVOR man sich auf die Werte verlässt.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Aktivitätstypen, die als "Krafttraining" gelten. Garmin nutzt hier den
# `typeKey` aus `activityType`. Passt bei Bedarf an eure Watch-Konfiguration
# an (z.B. falls ihr in Garmin Connect andere Kategorien verwendet).
STRENGTH_ACTIVITY_TYPE_KEYS = {"strength_training", "gym"}

# Garmins "Satz"-Einträge kommen als flache, chronologische Liste mit
# alternierenden setType-Werten: ACTIVE (tatsächlich ausgeführter Satz) und
# REST (Pause danach). Die Pausenzeit steht NICHT am ACTIVE-Satz selbst
# (dessen "restTime"/"restDuration"-Felder sind bei uns durchgängig null),
# sondern ist die "duration" des direkt nachfolgenden REST-Satzes.
ACTIVE_SET_TYPE = "ACTIVE"
REST_SET_TYPE = "REST"

# Garmin markiert per Watch/App erkannte Aufwärmübungen (z.B. "Arm Circles")
# über exercises[0]["category"] == "WARM_UP". Dropsets/Failure-Sätze sind in
# den Garmin-Rohdaten nicht als eigenes Feld erkennbar - dafür gibt es daher
# aktuell keine automatische Erkennung.
GARMIN_WARMUP_CATEGORY = "WARM_UP"

# SparkyFitness-Dropdown-Werte für set_type (siehe Frontend-Konstante
# excerciseWorkoutSetTypes.ts). "Warm-up" MIT Bindestrich - ohne Bindestrich
# wird der Satz zwar trotzdem gespeichert, aber vom Frontend nicht als
# bekannter Satztyp erkannt (kein Farb-Badge, keine Übersetzung).
SPARKY_SET_TYPE_WORKING = "Working Set"
SPARKY_SET_TYPE_WARMUP = "Warm-up"

# Garmin liefert das Gewicht pro Satz in Gramm.
GRAMS_PER_KG = 1000.0


class GarminAuthError(RuntimeError):
    """Login bei Garmin Connect fehlgeschlagen."""


@dataclass
class WorkoutSet:
    exercise_name: str
    set_number: int
    set_type: str
    reps: int | None
    weight_kg: float | None
    duration_seconds: float | None
    rest_time_seconds: float | None


@dataclass
class StrengthActivity:
    activity_id: str
    name: str
    start_date: str  # YYYY-MM-DD
    activity_type_key: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class GarminClient:
    def __init__(self, email: str, password: str, state_dir: Path):
        self._email = email
        self._password = password
        self._token_dir = state_dir / "garmin_tokens"
        self._token_dir.mkdir(parents=True, exist_ok=True)
        self._api = None  # lazy init, siehe login()

    # ------------------------------------------------------------------
    def login(self) -> None:
        """Meldet sich bei Garmin Connect an.

        Seit garminconnect >=0.3 gibt es kein separates ``garth``-Tokenstore-
        Objekt mehr: ``Garmin.login(tokenstore)`` versucht intern zuerst eine
        zwischengespeicherte Session aus ``tokenstore`` zu laden, führt bei
        Bedarf automatisch einen frischen E-Mail/Passwort-Login durch und
        speichert die Tokens danach selbst wieder in ``tokenstore``.
        """
        # Lazy Import, damit das Modul auch ohne installierte
        # garminconnect-Bibliothek importierbar bleibt (z.B. für Unit-Tests
        # von mapper.py etc. ohne die echte Abhängigkeit).
        try:
            import garminconnect
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Das Paket 'garminconnect' ist nicht installiert. "
                "Bitte 'pip install garminconnect' ausführen."
            ) from exc

        api = garminconnect.Garmin(email=self._email, password=self._password)
        try:
            api.login(str(self._token_dir))
            self._api = api
            logger.info("Garmin-Login erfolgreich (Session-Cache in %s aktualisiert).", self._token_dir)
        except Exception as exc:  # noqa: BLE001
            raise GarminAuthError(
                f"Garmin-Login fehlgeschlagen: {exc}. "
                f"Falls MFA aktiv ist, muss der Login evtl. einmalig interaktiv "
                f"durchgeführt werden."
            ) from exc

    @property
    def api(self):
        if self._api is None:
            raise RuntimeError("login() muss vor der Nutzung des Clients aufgerufen werden.")
        return self._api

    # ------------------------------------------------------------------
    def fetch_activities(
        self,
        start_date: dt.date,
        end_date: dt.date,
        type_keys: Iterable[str],
        max_activities: int = 50,
    ) -> list[StrengthActivity]:
        """Holt alle Aktivitäten eines bestimmten Typs (``type_keys``) im
        gegebenen Zeitraum.

        Generischer Nachfolger von ``fetch_strength_activities`` - jede
        zukünftige Trainingsart (Laufen, Radfahren, ...) kann diese Methode
        mit ihren eigenen Garmin-``typeKey``-Werten wiederverwenden, siehe
        ``handlers.py``.
        """
        type_keys = frozenset(type_keys)
        logger.info("Rufe Aktivitäten von %s bis %s ab...", start_date, end_date)
        raw_activities: Iterable[dict[str, Any]] = self.api.get_activities_by_date(
            start_date.isoformat(), end_date.isoformat()
        )

        result: list[StrengthActivity] = []
        for raw in raw_activities:
            type_key = (raw.get("activityType") or {}).get("typeKey", "")
            if type_key not in type_keys:
                continue

            activity_id = str(raw.get("activityId"))
            start_local = raw.get("startTimeLocal", "")
            entry_date = start_local.split(" ")[0] if start_local else start_date.isoformat()

            result.append(
                StrengthActivity(
                    activity_id=activity_id,
                    name=raw.get("activityName") or "Krafttraining",
                    start_date=entry_date,
                    activity_type_key=type_key,
                    raw=raw,
                )
            )

        if len(result) > max_activities:
            logger.warning(
                "Es wurden %d Aktivität(en) gefunden, aber nur die neuesten "
                "%d werden verarbeitet (max_activities_per_run). Erhöht das Limit via "
                "GARMIN_SPARKY_MAX_ACTIVITIES, falls gewünscht.",
                len(result),
                max_activities,
            )
            result = result[:max_activities]

        logger.info("%d passende Aktivität(en) gefunden.", len(result))
        return result

    def fetch_strength_activities(
        self, start_date: dt.date, end_date: dt.date, max_activities: int = 50
    ) -> list[StrengthActivity]:
        """Holt alle Krafttraining-Aktivitäten im gegebenen Zeitraum.

        Dünner, abwärtskompatibler Wrapper um ``fetch_activities`` mit den
        fest verdrahteten Krafttraining-Typen.
        """
        return self.fetch_activities(
            start_date, end_date, STRENGTH_ACTIVITY_TYPE_KEYS, max_activities
        )

    def fetch_single_activity(
        self, activity_id: str, allowed_type_keys: Iterable[str] | None = None
    ) -> StrengthActivity | None:
        """Holt eine einzelne Aktivität per ID (z.B. für Webhook-Trigger).

        ``allowed_type_keys`` grenzt ein, welche Garmin-Aktivitätstypen
        akzeptiert werden (Default: nur Krafttraining, siehe
        ``STRENGTH_ACTIVITY_TYPE_KEYS``). Der CLI-Aufruf übergibt hier die
        Vereinigung aller in ``handlers.ACTIVE_HANDLERS`` registrierten Typen.
        """
        allowed_type_keys = frozenset(allowed_type_keys or STRENGTH_ACTIVITY_TYPE_KEYS)
        raw = self.api.get_activity(activity_id)
        type_key = (raw.get("activityTypeDTO") or raw.get("activityType") or {}).get(
            "typeKey", ""
        )
        if type_key not in allowed_type_keys:
            logger.warning(
                "Aktivität %s hat Typ '%s', für den kein Handler registriert ist - "
                "wird übersprungen.",
                activity_id,
                type_key,
            )
            return None

        start_local = raw.get("summaryDTO", {}).get("startTimeLocal") or raw.get(
            "startTimeLocal", ""
        )
        entry_date = start_local.split(" ")[0] if start_local else dt.date.today().isoformat()

        return StrengthActivity(
            activity_id=str(activity_id),
            name=raw.get("activityName") or "Krafttraining",
            start_date=entry_date,
            activity_type_key=type_key,
            raw=raw,
        )

    # ------------------------------------------------------------------
    def fetch_exercise_sets_raw(self, activity_id: str) -> dict[str, Any]:
        """Roher API-Aufruf, ungefiltert - hilfreich für --debug-dump-raw."""
        return self.api.get_activity_exercise_sets(activity_id)

    def fetch_exercise_sets(self, activity_id: str) -> list[WorkoutSet]:
        """Holt und parsed die Satz-Details eines Krafttrainings.

        Garmin liefert pro Aktivität eine flache Liste von "exerciseSets"
        in chronologischer Reihenfolge (abwechselnd ACTIVE/REST). Wir
        filtern auf ACTIVE-Sätze und zählen pro Übung fortlaufend hoch.
        Die Pausenzeit eines ACTIVE-Satzes steht bei Garmin nicht am Satz
        selbst (dort beobachtet: "restTime"/"restDuration" durchgängig
        null), sondern ist die "duration" des direkt darauffolgenden
        REST-Satzes - siehe Modul-Konstanten oben.
        """
        raw = self.fetch_exercise_sets_raw(activity_id)
        raw_sets = raw.get("exerciseSets", []) if isinstance(raw, dict) else []

        parsed: list[WorkoutSet] = []
        set_counter: dict[str, int] = {}

        for index, raw_set in enumerate(raw_sets):
            set_type = raw_set.get("setType", "")
            if set_type != ACTIVE_SET_TYPE:
                continue

            exercise_name = self._extract_exercise_name(raw_set)
            set_counter[exercise_name] = set_counter.get(exercise_name, 0) + 1

            weight_grams = raw_set.get("weight")
            weight_kg = (
                round(weight_grams / GRAMS_PER_KG, 2)
                if isinstance(weight_grams, (int, float))
                else None
            )

            rest_time_seconds = raw_set.get("restTime") or raw_set.get("restDuration")
            if rest_time_seconds is None:
                next_raw_set = raw_sets[index + 1] if index + 1 < len(raw_sets) else None
                if next_raw_set is not None and next_raw_set.get("setType") == REST_SET_TYPE:
                    rest_time_seconds = next_raw_set.get("duration")

            parsed.append(
                WorkoutSet(
                    exercise_name=exercise_name,
                    set_number=set_counter[exercise_name],
                    set_type=self._extract_set_type(raw_set),
                    reps=raw_set.get("repetitionCount"),
                    weight_kg=weight_kg,
                    duration_seconds=raw_set.get("duration"),
                    rest_time_seconds=rest_time_seconds,
                )
            )

        logger.info("Aktivität %s: %d aktive Sätze geparst.", activity_id, len(parsed))
        return parsed

    @staticmethod
    def _extract_set_type(raw_set: dict[str, Any]) -> str:
        """Leitet den SparkyFitness-``set_type`` aus einem Garmin-Satz ab.

        Garmin markiert erkannte Aufwärmübungen über
        ``exercises[0]["category"] == "WARM_UP"``. Drop-Sets/Failure-Sätze
        sind in den Garmin-Rohdaten nicht als eigenes Feld erkennbar, dafür
        gibt es daher aktuell keine automatische Erkennung - alle übrigen
        aktiven Sätze werden als regulärer Arbeitssatz eingestuft.
        """
        exercises = raw_set.get("exercises") or []
        if exercises and (exercises[0] or {}).get("category") == GARMIN_WARMUP_CATEGORY:
            return SPARKY_SET_TYPE_WARMUP
        return SPARKY_SET_TYPE_WORKING

    @staticmethod
    def _extract_exercise_name(raw_set: dict[str, Any]) -> str:
        """Leitet einen menschenlesbaren Übungsnamen aus einem Garmin-Satz ab.

        Garmin liefert je nach Watch/Firmware entweder eine erkannte
        `exercises`-Liste mit Kategorie/Name, oder nur eine Kategorie ohne
        konkreten Namen. Reihenfolge der Priorität:
          1. exercises[0]["name"]      (z.B. "PULL_UP")
          2. exercises[0]["category"]  (z.B. "PULL_UP" Oberkategorie)
          3. raw_set["category"]       (Fallback auf Satz-Ebene)
          4. "Unbekannte Übung"        (letzter Fallback, nie None)
        """
        exercises = raw_set.get("exercises") or []
        if exercises:
            first = exercises[0] or {}
            name = first.get("name") or first.get("category")
            if name:
                return GarminClient._humanize(name)

        category = raw_set.get("category")
        if category:
            return GarminClient._humanize(category)

        return "Unbekannte Übung"

    @staticmethod
    def _humanize(garmin_enum_name: str) -> str:
        """Wandelt Garmin-Enum-Namen wie 'PULL_UP' in 'Pull Up' um."""
        return garmin_enum_name.replace("_", " ").title()
