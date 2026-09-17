# Anker SOLIX MQTT Bridge

Home Assistant-independent Modbus TCP bridge for Anker SOLIX with MQTT and a local web interface.

The bridge uses the Home Assistant-independent Modbus core from the official
[`anker-charging/ha-anker-solix-official`](https://github.com/anker-charging/ha-anker-solix-official)
repository. The official code is downloaded during the image build, verified by SHA-256 and API
contract checks, and included in the image together with its MIT license. Home Assistant is not
required.

> Direct system control can change charging behavior and the backup reserve. Test with
> non-critical limits first, and restrict access to the web interface and MQTT broker to a trusted
> local network.

## Quick start

Requirements:

- Docker with Compose
- Modbus TCP enabled on the Anker device, using port `502` by default
- An MQTT broker reachable from the container, such as the IP-Symcon MQTT Server

```bash
cp .env.example .env
docker compose up --build -d
```

Set at least `SOLARBANK_HOST` and `MQTT_HOST` in `.env`. The dashboard is then available at
`http://<docker-host>:8080`. The health endpoint is `/healthz`.

By default, every new image build verifies and includes the latest stable official release. For a
reproducible build or rollback, set a specific tag in `.env`, for example
`ANKER_SOLIX_UPSTREAM_TAG=v1.5.0`.

## Configuration

The application reads environment variables with the `ANKER_` prefix.

| Variable | Default | Description |
| --- | --- | --- |
| `ANKER_DEVICES` | required | JSON list of Modbus devices |
| `ANKER_MQTT__HOST` | required | MQTT broker hostname or IP address |
| `ANKER_MQTT__PORT` | `1883` | MQTT port |
| `ANKER_MQTT__USERNAME` | empty | Optional username |
| `ANKER_MQTT__PASSWORD` | empty | Optional password |
| `ANKER_MQTT__TOPIC_PREFIX` | `anker-solix` | Topic prefix without wildcards |
| `ANKER_MQTT__TLS` | `false` | Enable TLS using the system CA store |
| `ANKER_WEB__HOST` | `0.0.0.0` | Bind address inside the container |
| `ANKER_WEB__PORT` | `8080` | Web port inside the container |
| `ANKER_LOG_LEVEL` | `INFO` | Python/Uvicorn log level |

Configure multiple devices as JSON:

```json
[
  {
    "device_id": "solarbank_home",
    "host": "192.0.2.10",
    "name": "Home Solarbank",
    "port": 502,
    "poll_interval": 5,
    "retry_interval": 10
  },
  {
    "device_id": "solarbank_garage",
    "host": "192.0.2.11",
    "name": "Garage Solarbank"
  }
]
```

Each `device_id` must be unique and may contain letters, numbers, `_`, and `-`.

## MQTT

All payloads except availability are UTF-8 JSON. State, metadata, and availability are published
as retained messages with QoS 1. The container sets a Last Will for bridge availability.

| Topic | Retained | Content |
| --- | --- | --- |
| `anker-solix/bridge/availability` | yes | `online` or `offline` |
| `anker-solix/<device_id>/availability` | yes | `online` or `offline` |
| `anker-solix/<device_id>/metadata` | yes | Device, entities, units, and limits |
| `anker-solix/<device_id>/state` | yes | Current complete device state |
| `anker-solix/<device_id>/command` | no | Incoming control command |
| `anker-solix/<device_id>/command/result` | no | Result of every accepted or rejected command |

Example command:

```json
{
  "request_id": "ips-20260324-0001",
  "entity_key": "charging_limit_soc",
  "value": 90
}
```

The `request_id` must be unique for each intended write operation. Repeated messages with the same
ID are deduplicated. Valid `entity_key` values, data types, options, and limits are available in
the metadata topic. MQTT and the web interface use the same validator and control engine.

## Temporarily inhibit battery discharge

If the Solarbank must not provide energy while an electric vehicle is charging, an external
scheduler such as IP-Symcon can set battery power to `0 W`. The bridge currently has no built-in
scheduler.

> A `0 W` setpoint keeps the battery completely neutral: it will neither charge nor discharge
> during this period. This sequence is not suitable if discharge should be blocked while surplus
> PV power should still charge the battery.

Send all commands to this topic:

```text
anker-solix/solarbank/command
```

At the start of the inhibit period, the scheduler must send the following three commands in order.
Only send the next command after receiving a successful result for the previous one.

### Step 1: Enable third-party control

```json
{
  "request_id": "auto-start-mode-20260828-2000",
  "entity_key": "operating_mode",
  "value": "third_party_control"
}
```

### Step 2: Select the battery direction

```json
{
  "request_id": "auto-start-direction-20260828-2000",
  "entity_key": "battery_power_direction",
  "value": "discharge"
}
```

### Step 3: Set battery power to zero

```json
{
  "request_id": "auto-start-power-20260828-2000",
  "entity_key": "battery_power_setpoint",
  "value": 0
}
```

Replace the example IDs with new, unique `request_id` values for every execution. Otherwise, the
bridge deduplicates the command and only returns the result from the earlier execution.

The result is published to:

```text
anker-solix/solarbank/command/result
```

For each sent `request_id`, the scheduler must at least verify `"status":"success"`. The retained
state at `anker-solix/solarbank/state` should then contain:

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

After the inhibit period, the Max AC can return to custom mode if that mode contains the desired
normal Anker schedule:

```json
{
  "request_id": "auto-end-mode-20260828-2300",
  "entity_key": "operating_mode",
  "value": "custom_mode"
}
```

The capability mask `36` reported by the tested device permits `third_party_control` and
`custom_mode`, but not `self_consumption`. Always obtain supported modes from the metadata topic of
the specific device. Test the return to the normal mode manually first while monitoring
`battery_charging_power` and `battery_discharging_power`.

If MQTT fails, a command is rejected, or state reports `online: false` or `stale: true`, the
scheduler must not assume that the inhibit is active. Reliable automation requires result
monitoring and an error notification.

## Web interface

The dashboard displays device connectivity, freshness, measurements, and every control enabled by
the official device profile. Changes are applied immediately. Origin validation, a JSON content
requirement, and a CSRF token protect write requests; authentication is intentionally not included.
The web port should therefore not be exposed directly to the internet.

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m anker_solix_mqtt.upstream_fetch --tag v1.5.0
.venv/bin/anker-solix-mqtt
```

Checks:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
.venv/bin/pytest
```

The provenance of the included official core is stored in the image at
`anker_solix_mqtt/vendor/anker_solix_official/_upstream.json`; its license is stored alongside it as
`UPSTREAM_LICENSE`.
