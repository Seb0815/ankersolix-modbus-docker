# Anker SOLIX MQTT Bridge

Home-Assistant-unabhängige Modbus-TCP-Bridge für Anker SOLIX mit MQTT und lokalem Webfrontend.

Die Bridge verwendet den Home-Assistant-unabhängigen Modbus-Kern des offiziellen
Repositories [`anker-charging/ha-anker-solix-official`](https://github.com/anker-charging/ha-anker-solix-official).
Der offizielle Code wird beim Image-Build geladen, per SHA-256 und API-Vertrag geprüft und
zusammen mit seiner MIT-Lizenz in das Image übernommen. Home Assistant wird nicht benötigt.

> Direkte Anlagensteuerung kann Ladeverhalten und Notstromreserve verändern. Zuerst mit
> unkritischen Grenzwerten testen und den Zugriff auf Weboberfläche und MQTT-Broker auf das
> vertrauenswürdige lokale Netz begrenzen.

## Schnellstart

Voraussetzungen:

- Docker mit Compose
- Modbus TCP am Anker-Gerät, standardmäßig Port `502`
- Ein vom Container erreichbarer MQTT-Broker, beispielsweise der IP-Symcon MQTT Server

```bash
cp .env.example .env
docker compose up --build -d
```

In `.env` mindestens `SOLARBANK_HOST` und `MQTT_HOST` setzen. Das Dashboard ist danach unter
`http://<docker-host>:8080` erreichbar. Der Healthcheck liegt unter `/healthz`.

Standardmäßig wird bei jedem neuen Image-Build der neueste stabile offizielle Release geprüft
und eingebaut. Für einen reproduzierbaren Build oder Rollback kann in `.env` beispielsweise
`ANKER_SOLIX_UPSTREAM_TAG=v1.4.3` gesetzt werden.

## Konfiguration

Die Anwendung liest ausschließlich Umgebungsvariablen mit Präfix `ANKER_`.

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `ANKER_DEVICES` | erforderlich | JSON-Liste der Modbus-Geräte |
| `ANKER_MQTT__HOST` | erforderlich | Hostname oder IP des MQTT-Brokers |
| `ANKER_MQTT__PORT` | `1883` | MQTT-Port |
| `ANKER_MQTT__USERNAME` | leer | Optionaler Benutzername |
| `ANKER_MQTT__PASSWORD` | leer | Optionales Passwort |
| `ANKER_MQTT__TOPIC_PREFIX` | `anker-solix` | Topic-Präfix ohne Wildcards |
| `ANKER_MQTT__TLS` | `false` | TLS mit System-CA aktivieren |
| `ANKER_WEB__HOST` | `0.0.0.0` | Bind-Adresse im Container |
| `ANKER_WEB__PORT` | `8080` | Web-Port im Container |
| `ANKER_LOG_LEVEL` | `INFO` | Python/Uvicorn-Loglevel |

Mehrere Geräte werden als JSON konfiguriert:

```json
[
  {
    "device_id": "solarbank_haus",
    "host": "192.0.2.10",
    "name": "Solarbank Haus",
    "port": 502,
    "poll_interval": 5,
    "retry_interval": 10
  },
  {
    "device_id": "solarbank_garage",
    "host": "192.0.2.11",
    "name": "Solarbank Garage"
  }
]
```

`device_id` muss eindeutig sein und darf Buchstaben, Zahlen, `_` und `-` enthalten.

## MQTT

Alle Nutzdaten außer Availability sind UTF-8-JSON. State, Metadata und Availability werden
retained mit QoS 1 publiziert. Der Container setzt einen Last Will für die Bridge-Availability.

| Topic | Retained | Inhalt |
| --- | --- | --- |
| `anker-solix/bridge/availability` | ja | `online` oder `offline` |
| `anker-solix/<device_id>/availability` | ja | `online` oder `offline` |
| `anker-solix/<device_id>/metadata` | ja | Gerät, Entitäten, Einheiten und Grenzen |
| `anker-solix/<device_id>/state` | ja | Aktueller vollständiger Gerätezustand |
| `anker-solix/<device_id>/command` | nein | Eingehender Steuerbefehl |
| `anker-solix/<device_id>/command/result` | nein | Ergebnis jedes gültigen oder abgewiesenen Befehls |

Beispiel für einen Command:

```json
{
  "request_id": "ips-20260324-0001",
  "entity_key": "charging_limit_soc",
  "value": 90
}
```

`request_id` muss pro beabsichtigtem Schreibvorgang eindeutig sein. Wiederholungen derselben ID
werden dedupliziert. Zulässige `entity_key`-Werte, Datentypen, Optionen und Grenzen stehen im
Metadata-Topic. MQTT und Weboberfläche durchlaufen denselben Validator und dieselbe ControlEngine.

## Entladung zeitweise sperren

Wenn beispielsweise während des Ladens eines Elektroautos keine Energie aus der Solarbank
verwendet werden soll, kann eine externe Zeitsteuerung wie IP-Symcon die Batterieleistung auf
`0 W` setzen. Die Bridge besitzt derzeit keinen eigenen Zeitplan.

> Ein Sollwert von `0 W` hält die Batterie vollständig neutral: Sie wird in diesem Zeitraum weder
> entladen noch geladen. Soll nur die Entladung verhindert werden, während PV-Überschuss weiterhin
> in die Batterie fließen darf, ist diese Sequenz nicht geeignet.

Alle Befehle werden an dieses Topic gesendet:

```text
anker-solix/solarbank/command
```

Zu Beginn des Sperrzeitraums muss die Zeitsteuerung die folgenden drei Befehle nacheinander
senden. Erst wenn für einen Befehl ein erfolgreiches Ergebnis empfangen wurde, darf der nächste
gesendet werden.

### Schritt 1: Drittanbieter-Steuerung aktivieren

```json
{
  "request_id": "auto-start-mode-20260828-2000",
  "entity_key": "operating_mode",
  "value": "third_party_control"
}
```

### Schritt 2: Batterierichtung festlegen

```json
{
  "request_id": "auto-start-direction-20260828-2000",
  "entity_key": "battery_power_direction",
  "value": "discharge"
}
```

### Schritt 3: Batterieleistung auf null setzen

```json
{
  "request_id": "auto-start-power-20260828-2000",
  "entity_key": "battery_power_setpoint",
  "value": 0
}
```

Die Beispiel-IDs müssen bei jeder Ausführung durch neue, eindeutige `request_id`-Werte ersetzt
werden. Andernfalls liefert die Bridge wegen ihrer Deduplizierung nur das Ergebnis der früheren
Ausführung zurück.

Das Ergebnis erscheint auf:

```text
anker-solix/solarbank/command/result
```

Die Zeitsteuerung muss für die jeweils gesendete `request_id` mindestens `"status":"success"`
prüfen. Danach sollte im retained State unter `anker-solix/solarbank/state` Folgendes gelten:

```json
{
  "online": true,
  "stale": false,
  "values": {
    "battery_charging_power": 0,
    "battery_discharging_power": 0,
    "battery_power_setpoint": 0
  }
}
```

Nach dem Sperrzeitraum kann die Max AC in den benutzerdefinierten Modus zurückgeschaltet werden,
sofern dort der gewünschte normale Anker-Zeitplan hinterlegt ist:

```json
{
  "request_id": "auto-end-mode-20260828-2300",
  "entity_key": "operating_mode",
  "value": "custom_mode"
}
```

Die vom Testgerät gemeldete Capability-Maske `36` erlaubt `third_party_control` und
`custom_mode`, aber nicht `self_consumption`. Unterstützte Modi müssen grundsätzlich aus dem
Metadata-Topic des jeweiligen Geräts übernommen werden. Den Rückwechsel zuerst manuell testen und
dabei `battery_charging_power` sowie `battery_discharging_power` beobachten.

Bei MQTT-Ausfall, einem abgewiesenen Befehl oder einem State mit `online: false` beziehungsweise
`stale: true` darf die Zeitsteuerung nicht davon ausgehen, dass die Sperre aktiv ist. Für eine
dauerhafte Automatisierung sind Ergebnisüberwachung und eine Störungsmeldung erforderlich.

## Weboberfläche

Das deutsche Dashboard zeigt Geräteverbindung, Aktualität, Messwerte und alle vom offiziellen
Geräteprofil freigegebenen Controls. Änderungen werden sofort ausgeführt. Schreibzugriffe sind
durch Origin-Prüfung, JSON-Pflicht und ein CSRF-Token geschützt; eine Anmeldung ist bewusst nicht
enthalten. Der Web-Port sollte daher nicht direkt aus dem Internet erreichbar sein.

## Lokale Entwicklung

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m anker_solix_mqtt.upstream_fetch --tag v1.4.3
.venv/bin/anker-solix-mqtt
```

Prüfungen:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
.venv/bin/pytest
```

Die Provenienz des eingebauten offiziellen Kerns steht im Image unter
`anker_solix_mqtt/vendor/anker_solix_official/_upstream.json`; dessen Lizenz liegt daneben als
`UPSTREAM_LICENSE`.
