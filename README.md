# garmin_sparky_sync

CLI-Tool, das Krafttraining-Aktivitäten von Garmin Connect abruft und als
zusammenhängende Workout-Sessions in SparkyFitness (`/exercise-preset-entries`)
importiert.

## Architektur

```
garmin_sparky_sync/
├── config.py          # Env-Var-basierte Konfiguration (.env via python-dotenv)
├── garmin_client.py    # Login (mit Session-Caching) + Abruf/Parsing der Garmin-Daten
├── gemini_client.py     # Gemini-API-Anreicherung neuer Übungen (Muskelgruppe, Equipment, ...)
├── sparky_client.py    # Get-or-Create Übungen + POST /exercise-preset-entries, mit Retries
├── mapper.py            # Gruppiert Garmin-Sätze nach Übung, löst Exercise-IDs auf, Ignore-Liste
├── handlers.py           # Erweiterungspunkt für zukünftige Trainingsarten (siehe unten)
├── state.py             # Persistenter Sync-State (Idempotenz, Exercise-ID-Cache, Ignore-Liste)
└── cli.py                # argparse-Interface, Orchestrierung + Verwaltungsbefehle
```

Jedes Modul ist unabhängig testbar (siehe `test_mapper.py`, läuft komplett
offline mit Mocks).

### Erweiterbarkeit für weitere Trainingsarten

Aktuell wird ausschliesslich Krafttraining synchronisiert. Die Verarbeitung
läuft aber über ein generisches `ActivityTypeHandler`-Interface in
[`handlers.py`](handlers.py), das `cli.py` unabhängig von der konkreten
Trainingsart aufruft (`ACTIVE_HANDLERS`-Liste). Um z.B. Laufen oder
Radfahren zu ergänzen:

1. Neue Handler-Klasse (z.B. `RunningHandler`) analog zu
   `StrengthTrainingHandler` implementieren: `garmin_type_keys` setzen sowie
   `build_workout`/`post_workout` implementieren (eigener Mapper/Payload,
   da Laufen z.B. Distanz/Pace statt Sätzen/Gewicht braucht).
2. Instanz in `ACTIVE_HANDLERS` eintragen.

`GarminClient.fetch_activities(start, end, type_keys, ...)` ist bereits
generisch (nicht mehr an Krafttraining-Typen gebunden) und kann von neuen
Handlern wiederverwendet werden. An `cli.py` selbst muss für eine neue
Trainingsart nichts geändert werden - das ist aktuell reine
Architektur-Vorbereitung, es ist noch kein weiterer Handler implementiert.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Danach `.env` im Projektverzeichnis anlegen (siehe [Konfiguration](#konfiguration)
für alle verfügbaren Variablen) und mit echten Zugangsdaten befüllen.

## Konfiguration

Alle Variablen werden aus `.env` (oder der Umgebung) gelesen, siehe `config.py`.

| Variable | Pflicht? | Beschreibung |
|---|---|---|
| `GARMIN_EMAIL` | ✅ | Garmin-Connect-Login. |
| `GARMIN_PASSWORD` | ✅ | Garmin-Connect-Passwort. |
| `SPARKY_BASE_URL` | ✅ | Basis-URL der SparkyFitness-API **inkl. `/api`-Präfix**, z.B. `https://sparky.example.org/api`. |
| `SPARKY_API_KEY` | ✅ | API-Key für den `x-api-key`-Header. |
| `GARMIN_SPARKY_STATE_DIR` | optional | Verzeichnis für Sync-State/Caches/Garmin-Session. Default: `~/.garmin_sparky_sync`. |
| `GARMIN_SPARKY_MAX_ACTIVITIES` | optional | Max. Aktivitäten pro Lauf. Default: `50`. |
| `GEMINI_API_KEY` | optional | Google-Gemini-API-Key zur automatischen Anreicherung neu angelegter Übungen (siehe [Automatische Übungs-Metadaten via Gemini](#automatische-übungs-metadaten-via-gemini)). Ohne Key: generische Platzhalter-Werte. |
| `GEMINI_MODEL` | optional | Gemini-Modell. Default: `gemini-2.5-flash`. |

## Nutzung

```bash
# Heutige Krafttraining-Aktivitäten importieren
python -m garmin_sparky_sync --timeframe today

# Letzte 7 Tage, NUR anzeigen was passieren würde (kein POST an Sparky)
python -m garmin_sparky_sync -t 7days --dry-run

# Letzte 30 Tage
python -m garmin_sparky_sync -t 30days

# Manueller Datumsbereich
python -m garmin_sparky_sync --start-date 2026-07-01 --end-date 2026-07-31

# Einzelne Aktivität (z.B. für Webhook-/Event-Trigger)
python -m garmin_sparky_sync --activity-id 123456789

# Bereits synchronisierte Aktivität erneut importieren
python -m garmin_sparky_sync --activity-id 123456789 --force

# Ausführliches Logging
python -m garmin_sparky_sync -t today -v
```

## Übungen ignorieren

Von der Garmin-Watch automatisch erkannte Aufwärm-/Pausenübungen (z.B.
"Arm Circles") lassen sich dauerhaft von jedem zukünftigen Sync ausschliessen:

```bash
# Übung zur Ignore-Liste hinzufügen (Name egal ob Gross-/Kleinschreibung)
python -m garmin_sparky_sync --ignore-add "Arm Circles"

# Mehrere auf einmal
python -m garmin_sparky_sync --ignore-add "Arm Circles" --ignore-add "Treadmill"

# Aktuelle Ignore-Liste anzeigen
python -m garmin_sparky_sync --ignore-list

# Wieder entfernen
python -m garmin_sparky_sync --ignore-remove "Arm Circles"
```

Ignorierte Übungen werden beim Sync komplett übersprungen (keine
Sparky-Get-or-Create-Anfrage, kein Logging als Fehler) - falls ein Workout
danach keine einzige Übung mehr enthält, wird es gar nicht erst importiert.
Diese und alle folgenden Verwaltungsbefehle laufen rein lokal: **kein**
Garmin-Login, **kein** Sparky-Request.

## Caches einsehen / zurücksetzen

Persistenter Zustand liegt in `GARMIN_SPARKY_STATE_DIR` (Default
`~/.garmin_sparky_sync`) als einfache JSON-Dateien:

```bash
# Inhalt aller Caches anzeigen (importierte Aktivitäten + Exercise-ID-Cache)
python -m garmin_sparky_sync --cache-show

# Exercise-ID-Cache leeren (Übungen werden beim nächsten Sync erneut in
# Sparky gesucht/angelegt - nützlich nach manuellen Änderungen in Sparky)
python -m garmin_sparky_sync --cache-clear-exercises

# Cache bereits importierter Aktivitäten leeren (Idempotenz-State - danach
# würden Aktivitäten im gewählten Zeitraum erneut importiert)
python -m garmin_sparky_sync --cache-clear-synced

# Beides auf einmal
python -m garmin_sparky_sync --cache-clear-all
```

`--cache-show` listet bereits synchronisierte Aktivitäten mit Datum und
Aktivitätenname auf (nicht nur der reinen Garmin-`activityId`), z.B.:

```
Bereits synchronisierte Garmin-Aktivitäten (2):
  - 2026-07-28 | 23764502856 | Push Day
  - 2026-07-29 | 23776855573 | Pull Day (Oberkörper)
```

Name/Datum werden ab dem ersten Sync mit dieser Version automatisch
mitgespeichert. Einträge aus einer älteren `synced_activities.json` (nur
IDs, ohne Name/Datum) werden beim Laden automatisch ins neue Format
migriert und zeigen bis zum nächsten erneuten Import dieser Aktivität
`unbekanntes Datum`/`unbekannter Name` an.

## Automatische Übungs-Metadaten via Gemini

Wird beim Sync eine Übung neu in SparkyFitness angelegt (nicht bereits per
Cache/Suche gefunden), müssen Felder wie Muskelgruppe, Equipment, Kategorie
usw. ausgefüllt werden. Ist `GEMINI_API_KEY` gesetzt, übernimmt das die
[Google Gemini API](https://aistudio.google.com/apikey): der Übungsname
(z.B. "Dumbbell Hammer Curl") wird mit strukturierter JSON-Ausgabe
(`responseSchema`) an Gemini geschickt, das Ergebnis befüllt `category`,
`equipment`, `primary_muscles`, `secondary_muscles`, `force`, `level`,
`mechanic`, `instructions` und `description`.

Gemini wird **ausschliesslich** beim tatsächlichen Neuanlegen aufgerufen
(nicht für bereits bekannte Übungen) - siehe `mapper.py::ExerciseIdResolver`.

**Sprache:** `category`/`force`/`level`/`mechanic` sind feste, in Sparky
hinterlegte englische Enum-Werte (siehe unten) und werden **nicht**
übersetzt - alle anderen Felder (`equipment`, `primary_muscles`,
`secondary_muscles`, `instructions`, `description`) lässt Gemini auf
**Deutsch** generieren, passend zu einer deutschsprachigen SparkyFitness-
Instanz.

**Fallback-Verhalten:** Ist `GEMINI_API_KEY` nicht gesetzt, Gemini nicht
erreichbar, oder liefert eine unerwartete Antwort, wird eine Warnung
geloggt und auf die alten generischen Platzhalter zurückgefallen
(`equipment=None`, `primary_muscles=other`, `category=strength`) - der
Sync läuft in jedem Fall weiter, eine fehlerhafte Gemini-Konfiguration
blockiert nie den Import.

**Gemini-Verbindung testen** (ohne Sync, ohne Garmin-Login):

```bash
python -m garmin_sparky_sync --test-gemini
```

Schickt eine echte Test-Anfrage ("Push-Up") über exakt denselben Codepfad
wie beim Anlegen neuer Übungen und gibt die Antwort aus. Exit-Code `0` bei
Erfolg, `2` bei Fehler (fehlender/ungültiger Key, Netzwerkproblem, falsches
Modell, ...) - nützlich um die `.env`-Konfiguration schnell zu prüfen.

### Gültige Sparky-Enum-Werte

SparkyFitness kennt nur ein festes Set an Werten für `category`
(Übungskategorie), `force`, `level` und `mechanic` - diese sind in der
Sparky-Oberfläche als Dropdown hinterlegt (nicht in der API dokumentiert,
per Quellcode-Recherche + Live-Tests gegen die Sparky-API verifiziert).
Andere Werte werden zwar gespeichert, aber vom Frontend nicht übersetzt/
gestylt angezeigt:

| Feld | Gültige Werte |
|---|---|
| `category` | `general`, `strength`, `cardio`, `yoga`, `powerlifting`, `olympic weightlifting`, `strongman`, `plyometrics`, `stretching`, `isometric` |
| `force` | `push`, `pull`, `static` |
| `level` | `beginner`, `intermediate`, `expert` |
| `mechanic` | `isolation`, `compound` |

`sparky_client.py::_normalize_category()` erzwingt für `category` immer
einen dieser Werte (case-insensitiv, Fallback auf `strength`), egal ob der
Wert von Gemini oder aus einem Default-Pfad kommt.

## Satz-Details (Dauer, Pausenzeit, Satztyp, Reihenfolge)

Pro Satz werden folgende Felder an SparkyFitness übertragen:

- **`duration`** (Satzdauer): Sparky behandelt dieses Feld trotz des
  Namens/der (veralteten) API-Doku als **Minuten**, nicht Sekunden - live
  über die Sparky-Edit-Oberfläche verifiziert (Feld heisst dort
  "DURATION (MIN)"). Garmin liefert die Satzdauer in Sekunden, `SparkySet
  .to_payload()` rechnet daher `/ 60` um (z.B. 63s Garmin-Dauer -> `1.05`).
  Ohne diese Umrechnung zeigte Sparky für einen 63-Sekunden-Satz "63
  Minuten" an - über mehrere Sätze summierten sich so absurde
  Übungsdauern von mehreren Stunden statt Minuten.
- **`rest_time`** (Pausenzeit): ist tatsächlich **Sekunden** (keine
  Umrechnung) - anders als `duration`. Garmin liefert die Pausenzeit eines
  Satzes nicht am Satz selbst (dort durchgängig `null`), sondern als
  `duration` des direkt nachfolgenden `REST`-Eintrags in der flachen
  `exerciseSets`-Liste. `garmin_client.py::fetch_exercise_sets` liest
  deshalb den jeweils nächsten Satz mit. Muss als Integer übertragen
  werden - SparkyFitness lehnt Floats mit einem DB-Fehler ab.
- **`set_type`**: `"Warm-up"` (mit Bindestrich - Sparkys tatsächlicher
  Dropdown-Wert, siehe `excerciseWorkoutSetTypes.ts` im Sparky-Frontend),
  falls Garmin die Übung als Aufwärmübung erkannt hat
  (`exercises[0]["category"] == "WARM_UP"`), sonst `"Working Set"`.
  Drop-Sets/Failure-Sätze sind in Garmins Rohdaten nicht unterscheidbar
  und werden daher nicht automatisch erkannt.
- **`sort_order`** (pro Übung, 0-basiert): hält die Original-Reihenfolge
  aus Garmin fest. Ohne dieses Feld setzt SparkyFitness für jede Übung den
  Default `sort_order: 0`, wodurch die Anzeige-Reihenfolge nicht mehr der
  tatsächlichen Trainingsreihenfolge entspricht.
- **`duration_minutes`** (pro Übung, echte Minuten): Summe aus
  Satz-`duration` + `rest_time` aller Sätze der Übung, umgerechnet in
  Minuten (unabhängig vom `duration`-Minuten-Quirk oben - dieses Feld ist
  eine separate, korrekte Größe). SparkyFitness berechnet diesen Wert
  NICHT automatisch aus den Sätzen (er bliebe sonst `0`) - ohne dieses
  Feld wäre sowohl die pro Übung angezeigte Dauer als auch
  `total_duration_minutes` der gesamten Workout-Session falsch bzw. 0.
- **`calories_burned`** (pro Übung): wird bewusst fest auf `0` gesetzt.
  Ohne dieses Feld würde SparkyFitness server-seitig aus
  `duration_minutes` und dem (teils geschätzten) `calories_per_hour` der
  Übung automatisch Kalorien berechnen - das Tool synct hier absichtlich
  keine Kalorien mit, falls diese bereits über eine andere Quelle
  (z.B. direkt von der Garmin-Watch) in SparkyFitness ankommen.

### WICHTIG: Vor dem ersten echten Lauf kalibrieren

Garmins JSON-Struktur für `get_activity_exercise_sets()` ist nicht offiziell
dokumentiert. Bevor ihr euch auf das Parsing verlasst, prüft die Rohdaten
einmal manuell:

```bash
python -m garmin_sparky_sync --activity-id <eure-activity-id> --debug-dump-raw
```

Das gibt das komplette rohe JSON aus, ohne irgendetwas an Sparky zu senden.
Falls die Feldnamen (`setType`, `repetitionCount`, `weight`, `restTime`, ...)
bei euch abweichen, müssen `garmin_client.py::fetch_exercise_sets` und
`_extract_exercise_name` entsprechend angepasst werden.

## Idempotenz / Cronjob-Betrieb

Jede erfolgreich importierte Garmin-`activityId` wird in
`~/.garmin_sparky_sync/synced_activities.json` vermerkt. Ein täglich
laufender Cronjob mit `--timeframe 7days` importiert dadurch keine
Duplikate, selbst wenn sich die Zeitfenster überlappen. Mit `--force`
lässt sich das für einzelne Aktivitäten gezielt umgehen.

Beispiel-Crontab (täglich um 22 Uhr):

```
0 22 * * * cd /pfad/zu/garmin_sparky_sync && .venv/bin/python -m garmin_sparky_sync -t 7days >> sync.log 2>&1
```

## Bekannte Einschränkungen / offene Punkte

- **`POST /exercise-preset-entries` ist in der SparkyFitness-OpenAPI-Spec
  ohne Request-Body-Schema dokumentiert.** Das Payload-Format hier basiert
  1:1 auf dem verifizierten PoC-Skript, nicht auf der (unvollständigen)
  Spec. Bei SparkyFitness-Updates kann sich das Format ändern - im Zweifel
  immer erst mit `--dry-run` gegenprüfen.
- **Satztyp-Erkennung erkennt nur Warm-up, kein Drop-Set/Failure:** Garmin
  markiert per Watch/App erkannte Aufwärmübungen über
  `exercises[0]["category"] == "WARM_UP"` - solche Sätze werden als
  `set_type: "Warm-up"` übertragen, alle anderen aktiven Sätze als
  `"Working Set"`. Drop-Sets/Failure-Sätze sind in den Garmin-Rohdaten
  (siehe `--debug-dump-raw`) nicht als eigenes Feld erkennbar, dafür gibt
  es daher aktuell keine automatische Erkennung.
- **`duration`-Minuten-Quirk ist nicht offiziell dokumentiert:** Dass
  Sparky das Satz-Feld `duration` als Minuten statt Sekunden behandelt
  (siehe [Satz-Details](#satz-details-dauer-pausenzeit-satztyp-reihenfolge)),
  wurde live über die Sparky-UI beobachtet, nicht aus einer offiziellen
  Spec. Bei SparkyFitness-Updates kann sich das ändern - im Zweifel mit
  `--dry-run` und einem Blick in die Sparky-UI gegenprüfen.
- **`--debug-dump-raw` ist weiterhin Krafttraining-spezifisch:** nutzt
  direkt `get_activity_exercise_sets()`, unabhängig von der generischen
  Handler-Architektur in `handlers.py`.
- **Alternativer Ansatz `POST /exercise-entries/import-fit`:** Die
  SparkyFitness-API bietet zusätzlich einen Endpoint zum direkten Upload
  roher `.fit`-Dateien (kein Get-or-Create nötig). Er nutzt allerdings
  `bearerAuth` statt `x-api-key` und wurde hier bewusst nicht eingebaut -
  könnte aber je nach Auth-Setup eine robustere Alternative sein.
- **MFA bei Garmin:** Falls Multi-Faktor-Authentifizierung aktiv ist, muss
  der allererste Login evtl. einmalig interaktiv (mit MFA-Code-Eingabe)
  erfolgen, bevor der Token-Cache greift.

## Tests

```bash
# Aus dem übergeordneten Verzeichnis (Package-Modus, wie beim normalen Aufruf):
python -m unittest garmin_sparky_sync.test_mapper -v
```
