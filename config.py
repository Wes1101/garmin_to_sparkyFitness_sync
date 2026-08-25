"""
Zentrale Konfiguration.

Alle Zugangsdaten werden ausschliesslich aus Umgebungsvariablen gelesen
(optional per .env-Datei via python-dotenv geladen). Es werden nirgends
Zugangsdaten hart codiert oder geloggt.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    garmin_email: str
    garmin_password: str
    sparky_base_url: str
    sparky_api_key: str

    # Verzeichnis für persistenten Zustand (Sync-State, Exercise-ID-Cache,
    # Garmin-Session-Token). Kann per GARMIN_SPARKY_STATE_DIR überschrieben
    # werden, Default ist ein verstecktes Verzeichnis im Home-Verzeichnis.
    state_dir: Path = Path.home() / ".garmin_sparky_sync"

    # Wie viele strength-training Aktivitäten maximal pro Lauf verarbeitet
    # werden (Sicherheitsnetz gegen versehentliche Massen-Importe).
    max_activities_per_run: int = 50

    # Optional: Google Gemini API-Key zur automatischen Anreicherung neu
    # angelegter SparkyFitness-Übungen mit Metadaten (Muskelgruppe,
    # Equipment, Kategorie, ...). Ohne Key wird auf generische
    # Platzhalter-Werte zurückgefallen (siehe gemini_client.py/mapper.py).
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"


class ConfigError(RuntimeError):
    """Wird geworfen, wenn Pflicht-Konfiguration fehlt."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Umgebungsvariable '{name}' fehlt oder ist leer. "
            f"Bitte in .env oder als Umgebungsvariable setzen."
        )
    return value


def load_config(env_file: str | None = None) -> Config:
    """Lädt die Konfiguration aus .env (falls vorhanden) + Umgebung.

    Args:
        env_file: Optionaler expliziter Pfad zu einer .env-Datei.
                  Wenn None, wird die Standard-Suche von python-dotenv
                  verwendet (.env im aktuellen/übergeordneten Verzeichnis).
    """
    if env_file:
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        load_dotenv(override=False)

    try:
        cfg = Config(
            garmin_email=_require("GARMIN_EMAIL"),
            garmin_password=_require("GARMIN_PASSWORD"),
            sparky_base_url=_require("SPARKY_BASE_URL").rstrip("/"),
            sparky_api_key=_require("SPARKY_API_KEY"),
            state_dir=Path(
                os.environ.get("GARMIN_SPARKY_STATE_DIR", "").strip()
                or str(Path.home() / ".garmin_sparky_sync")
            ),
            max_activities_per_run=int(
                os.environ.get("GARMIN_SPARKY_MAX_ACTIVITIES", "").strip() or "50"
            ),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip() or None,
            gemini_model=os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash",
        )
    except ConfigError as exc:
        print(f"[Konfigurationsfehler] {exc}", file=sys.stderr)
        raise

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg
