# Ridgeline TMS API

Start the server using the [setup commands](SETUP.md#5-terminal-a-start-the-tms). Base URL: `http://127.0.0.1:8000`. This local training service has no authentication. It binds only to localhost.

Drivers, loads, facilities and policies come from `tms/seed.json`. Status updates and callbacks are stored in server memory. Restart to reset everything. Read seed data and saved records through these endpoints; your agent must use HTTP, not read the seed file.

## Read

| GET path | Returns |
|---|---|
| `/health` | `{"status":"ok"}` |
| `/v1/drivers?truck={truck}` | Matching drivers |
| `/v1/drivers/{driver}` | One driver |
| `/v1/loads?driver={driver}` | Driver's loads, including delivered loads |
| `/v1/loads?driver={driver}&status={status}` | Driver's loads filtered by status |
| `/v1/loads/{load}` | Load, pickup and delivery details |
| `/v1/facilities/{facility}` | Facility address, hours, gate code and dock notes |
| `/v1/kb` | All company facts and policies |
| `/v1/kb?topic={topic}` | One topic and its answer |
| `/v1/check-calls` | All saved status updates |
| `/v1/loads/{load}/check-calls` | Status updates for one load |
| `/v1/callbacks?status={status}` | Dispatcher queue |

Replace each `{parameter}` with its JSON field value:

| Parameter | JSON field | Example |
|---|---|---|
| `{truck}` | Driver's `truck` | `117` |
| `{driver}` | Load or callback's `driver` (the driver's `id`) | `D117` |
| `{load}` | Status update or callback's `load` (the load's `id`) | `RF-48213` |
| `{facility}` | Load stop's `pickup.facility` or `delivery.facility` (the facility's `id`) | `receiver-1` |
| `{topic}` | KB response's `topic`; a key in the full KB | `fuel_card` |
| `{status}` | Load or callback's `status` | `assigned`, `in_transit`, `delivered` for loads; `open` for callbacks |

For example, a load with `"delivery": {"facility": "receiver-1"}` points to `GET /v1/facilities/receiver-1`. The returned facility has `"id": "receiver-1"`. The yard uses the same endpoint: `GET /v1/facilities/yard`.

Lists return `{"items":[...]}`; individual resources return `{"item":{...}}`. Unknown IDs and topics return 404. An unmatched list filter returns an empty list. Load status is `assigned`, `in_transit` or `delivered`. Upcoming loads include a `dispatch_order` field. Both `pickup` and `delivery` contain `facility`, `window_start` and `window_end`.

## Write

Send JSON with `Content-Type: application/json` and an `Idempotency-Key` header. Generate a new key for each confirmed action; reuse it on retries. A successful create returns 201 and `{"item":{...}}`. Repeating the same key, path and body returns the original record with 200. Reusing a key with different data returns 409. Keys reset when the server restarts.

### Save a driver status update

`POST /v1/loads/{load}/check-calls`

Example for `load = RF-48213`:

```json
{"driver":"D117","location":"Denver, CO","eta":"2026-11-03T13:30:00-07:00","notes":"Loaded; no issues"}
```

`driver`, `location` and `eta` are required nonempty strings. `notes` is an optional string. ETA must include a date and timezone offset. The server verifies driver assignment, rejects updates to loads already marked delivered, and saves the status update.

### Request a dispatcher callback

`POST /v1/callbacks`

```json
{"driver":"D109","load":"RF-48224","callback_number":"303-555-0109","reason":"Blown trailer tire; stopped on I-70 near Limon","urgency":"now"}
```

`driver`, `callback_number`, `reason` and `urgency` are required nonempty strings. `load` is optional; omit it when unknown. A supplied load must belong to the driver. Urgency is `now`, `today` or `routine`. The server adds the record ID, truck, timestamp and `status: "open"`. The agent chooses urgency using the brief's rules.

### Errors

Errors return `{"error":{"code":"...","message":"..."}}`.

| Status | Meaning |
|---|---|
| 400 | Invalid JSON object or body larger than 64 KiB |
| 404 | Unknown route, driver, load, facility or topic |
| 409 | Delivered load, driver mismatch, or reused key with different data |
| 422 | Missing/invalid field, ETA, urgency or idempotency key |
| 503 | Status-update service unavailable; nothing saved |

For failure testing, use the [outage-test setup commands](SETUP.md#9-finish-a-session-and-test-an-outage). Reads and callbacks still work; new status updates return 503. Restart normally to clear this mode.

## Inspect records

Open the GET URLs in a browser or run:

```sh
curl http://127.0.0.1:8000/v1/check-calls
curl http://127.0.0.1:8000/v1/callbacks
```

Compare these records with your expected results in the [manual eval sheet](TESTING.md).
