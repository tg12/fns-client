# Data Update Flow

This document describes how NOTAM and aircraft data move through the stack,
what triggers updates, and how often each stage runs.

## End-to-End Workflow

```mermaid
flowchart TD
    A[FAA SWIM JMS stream] --> B[fns-client message consumer]
    B --> C[(PostgreSQL NOTAMS table)]

    D[FIL initial load] --> E[fns-client bootstrap]
    E --> C

    C --> F[/api/notams]
    C --> G[/api/notams/intelligence]
    G --> H[Rule-based classifier + translation]

    I[Browser dashboard] -->|every 60s| G
    I -->|every 60s| J[/api/aircraft proxy]
    I -->|bbox change debounced 700ms| K[/api/aircraft/control/bbox]

    J --> L[opensky-poller]
    K --> L
    L -->|every 60s| M[OpenSky REST API]
    L --> J

    I --> N[Map/Table/Sidebar render]
    G --> N
    J --> N
```

## Update Cadence

### NOTAM ingestion and persistence

- Live JMS ingestion: event-driven, writes on message arrival.
- Replay mode bootstrap: one-time load at startup from replay XML.
- FIL bootstrap/rebuild: one-time load at startup when FIL is configured, and
  repeated if missed/stale detection invalidates the DB.
- Old NOTAM cleanup loop: every `notamDb.removeOldNotams.frequency` hours
  (default 24h).

### Missed/stale protection

- Tracker loop runs every `messageTracker.scheduleRate` seconds (default 10s).
- Missed-gap threshold: `messageTracker.missedMessageTriggerTime` minutes
  (default 5m).
- Stale-stream threshold: `messageTracker.staleMessageTriggerTime` minutes
  (default 10m).
- On trigger, DB is marked invalid and FIL reinitialization is scheduled.

### API/query layer

- `/api/notams`: reads active rows from DB at request time.
- `/api/notams/intelligence`: same DB read plus intelligence enrichment at
  request time (category, severity, noise filtering, plain-English text).
- Intelligence is not persisted; it is derived per request.

### Aircraft path

- `opensky-poller` fetch interval: `POLL_INTERVAL` seconds (default 60s in
  `docker-compose.yml`).
- `opensky-poller` applies recent UI bbox for up to
  `CONTROL_BBOX_TTL_SECONDS` (default 120s).
- fns-client aircraft proxy cache TTL: `AIRCRAFT_PROXY_CACHE_SECONDS`
  (default 2s).
- fns-client aircraft proxy rate limit: `AIRCRAFT_PROXY_RPM`
  (default 240 in `docker-compose.yml`).

### Browser/dashboard refresh

- NOTAM intelligence refresh: every `NOTAM_REFRESH_MS` (currently 60000ms).
- Aircraft refresh: every `AIRCRAFT_REFRESH_MS` (currently 60000ms).
- Countdown badge repaint: every 15s without a full refetch.
- Bbox control updates are sent after drag/zoom end with 700ms debounce.

## Source of Truth for Intervals

- fns-client service orchestration:
  - `fns_client/service.py`
  - `fns_client/missed_tracker.py`
- API/proxy cache and rate limit:
  - `fns_client/rest_api.py`
- OpenSky poll cadence:
  - `opensky_poller/service.py`
  - `docker-compose.yml`
- Dashboard refresh cadence:
  - `public/index.html`

## Data Sources and Reference URLs

This section lists every external or upstream data source used by the stack,
plus where each one is consumed.

### NOTAM sources

- FAA SWIM AIM/FNS JMS stream (live NOTAM updates)
  - URL: https://www.faa.gov/air_traffic/technology/swim
  - Consumed by: `fns_client/transport.py` via Solace JMS
  - Notes: requires SWIM credentials, VPN, destination queue/topic config.

- FAA FIL SFTP initial load (bootstrap/rebuild snapshot)
  - URL: https://www.faa.gov/air_traffic/technology/swim
  - Consumed by: `fns_client/fil.py` and `fns_client/service.py`
  - Notes: used to initialize/recover DB state; requires RSA PEM private key.

- Local replay XML dataset (deterministic development mode)
  - Path: `docker/replay-data`
  - Consumed by: `fns_client/service.py` when `runtime.mode="replay"`
  - Notes: local fixture data, not live.

### Aircraft source (used for NOTAM proximity correlation)

- OpenSky Network states API
  - URL: https://openskynetwork.github.io/opensky-api/rest.html
  - Upstream endpoint used by poller:
    `https://opensky-network.org/api/states/all`
  - Consumed by: `opensky_poller/service.py`
  - Notes: polled at `POLL_INTERVAL`; bbox control used to reduce response size.

### Geospatial reference source

- ICAO airport reference dataset (`mwgg/Airports`)
  - URL: https://github.com/mwgg/Airports
  - Raw JSON used by dashboard:
    `https://raw.githubusercontent.com/mwgg/Airports/master/airports.json`
  - Consumed by: `public/index.html`
  - Notes: used for map coordinates and location resolution.

### Official operational reference (human verification)

- FAA NOTAM search portal (authoritative operational lookup)
  - URL: https://notams.aim.faa.gov/
  - Notes: use for official flight-critical confirmation; this project is not
    an operational aviation tool.