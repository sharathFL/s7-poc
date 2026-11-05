# S7-1500 data acquisition PoC: Python and Go

Reads one Siemens S7-1500 through two independent protocols, **S7comm** (TCP 102) and **OPC UA**
(TCP 4840), and serves normalized data over the same REST API and dashboard from two
implementations:

| | Python (`:8001`) | Go (`:8002`) |
|---|---|---|
| S7comm | `python-snap7` (`db_read` only) | Apache PLC4X **plc4go** S7 driver (read requests only) |
| OPC UA | `asyncua` (poll **or subscribe**, browse, secure channel) | `gopcua` (poll, browse, secure channel) |

plc4py has no S7 driver, so Python uses python-snap7. plc4go does. Both are read-only: no write
call exists in either code base and the API registers only `GET` routes (other methods return 405).
Subscriptions are implemented in Python only; Go polls.

Both images are `linux/amd64`, run on Docker Desktop (Windows, Linux containers/WSL2) and need no GPU.

## Quick start (Windows PowerShell, same on Linux)

```powershell
# 1. no PLC: simulated data
$env:S7POC_MOCK="true"; docker compose up --build
# 2. real PLC: edit config\devices.yaml (ip, endpoint, DB addresses, node ids), then
Remove-Item Env:S7POC_MOCK; docker compose up --build
```

Without Docker (Python only, Windows): `.\run-windows.ps1 -Mock` or `.\run-windows.ps1`.

Dashboards: <http://localhost:8001> (Python), <http://localhost:8002> (Go). API docs for Python:
<http://localhost:8001/docs>. Run one only: `docker compose up --build python` or `... go`.
Stop: `docker compose down`.

The container reaches the PLC through Docker Desktop's NAT, so the Windows host must be able to reach
`192.168.x.x:102/4840`. Test first: `Test-NetConnection 192.168.1.10 -Port 102`.
Config can also go in `.env` (see `.env.example`). Docker Desktop must be in **Linux containers** mode.

### Modes

| Command | What runs |
|---|---|
| `docker compose up --build` | real drivers against `config/devices.yaml` |
| `S7POC_MOCK=true docker compose up --build` | mock drivers, deterministic simulated values |
| `CONFIG_FILE=devices.sim.yaml docker compose --profile sim up --build` | real OPC UA drivers against a generic OPC UA test server (S7comm off) |

**The mock and the sim server are not S7-1500 emulators.** They verify the software pipeline, API and
dashboard only. They say nothing about S7-1500 behaviour, timing, security or addressing.

## Configuration

`config/devices.yaml` is shared by both implementations. Key points:

- `s7comm`: `rack`, `slot`, `port`, `polling_ms`, `timeout_ms`. S7-1500 is normally rack 0 / slot 1.
- `opcua`: `endpoint`, `security_mode`/`security_policy` (`None`, or e.g. `SignAndEncrypt`/`Basic256Sha256`),
  `mode: poll|subscribe` (subscribe: Python only), `client_cert`/`client_key` under `/secrets`.
- `tags[]`: `s7_address` (`DB10.DBD0`, `DB10.DBX12.0`, `DBW`, `DBB`...), `type`, `opcua_node`. Do not
  assume node ids: S7-1500 ids look like `ns=3;s="PocData"."Temperature"` and the namespace index varies.
  Use `GET /opcua/browse` to find them.
- **Secrets** never go in YAML. `OPCUA_USERNAME`, `OPCUA_PASSWORD` (or `OPCUA_PASSWORD_FILE`, per-device
  `OPCUA_PASSWORD_<DEVICE_ID>`) come from the environment/`.env`; certificates and keys from `./secrets`
  (mounted read-only at `/secrets`). Error messages never include credentials.

Supported S7 types (Python decoders): BOOL, BYTE, SINT, USINT, INT, UINT, WORD, DINT, UDINT, DWORD,
REAL, LREAL, LINT, ULINT, LWORD, TIME, CHAR, STRING[n], WSTRING[n]. Go delegates decoding to plc4go.

## REST API (identical in both implementations)

All `GET`, JSON. Reading shape:

```json
{"device_id":"s7_1500_01","protocol":"s7comm","tag":"Temperature","value":35.5,"datatype":"REAL",
 "timestamp":"2026-11-05T10:00:00.000Z","quality":"GOOD","error":null,"latency_ms":4.2,
 "metadata":{"address":"DB10.DBD0"}}
```

`quality`: `GOOD`, `STALE` (older than `stale_after_factor` x `polling_ms`), `INVALID` (item-level error,
bad status, decode failure), `COMM_FAILURE` (transport down; last known value kept for reference).
`protocol` is `s7comm`/`opcua` (`mock-s7comm`/`mock-opcua` in mock mode). `metadata` is protocol
specific (S7 address; OPC UA node id, status code, source/server timestamps).

| Endpoint | Description |
|---|---|
| `GET /health` | liveness, implementation name, connected channel count |
| `GET /devices` | devices with state `ONLINE`/`DEGRADED`/`OFFLINE` |
| `GET /devices/{id}/status` | per protocol: connected, last success/error, reconnects, success rate, latency avg/p95/max, recent errors |
| `GET /devices/{id}/tags` | configured tags |
| `GET /devices/{id}/readings?protocol=&tag=&history=&limit=` | latest readings (or history, `limit` 1..5000) |
| `GET /devices/{id}/tags/{tag}/latest?protocol=` | latest reading(s) of one tag |
| `GET /opcua/browse?device_id=&start=&depth=` | browse OPC UA address space (default Objects folder, depth 2, max 6) |
| `GET /comparison?device_id=&rel_tol=&abs_tol=` | per-tag S7comm vs OPC UA: values, diff, type match, sampling skew, freshness, plus success rate and latency. Defaults rel 2%, abs 0.05 |

Comparison never assumes simultaneous reads: it reports `skew_ms` and uses a tolerance. A counter can
legitimately differ by one count between two independent samples.

## Tests

```bash
# Python (mock + in-process generic OPC UA server + unit tests with synthetic byte arrays)
cd python && pip install -r requirements-dev.txt && pytest
# Go
docker run --rm -v "$PWD/go":/w -w /w golang:1.27 go test ./...
# Against a REAL PLC (read-only), Python only:
S7_REAL_PLC_IP=192.168.1.10 pytest -m plc
```

Markers: `plc` = real hardware (skipped unless `S7_REAL_PLC_IP` is set); `opcua_server` = generic
asyncua server; everything else is mock/unit. Real-PLC behaviour is not covered by CI.

## Configuring the S7-1500 (with your PLC administrator)

This application never changes PLC settings. Verify these in TIA Portal:

**S7comm**
1. Create a data block `DB10`, and **untick "Optimized block access"** (Properties > Attributes), then compile.
   Declare Temperature REAL @0, Pressure REAL @4, MotorSpeed REAL @8, MotorRunning BOOL @12.0, ProductionCount DINT @14.
   Check the real offsets in the compiled DB, since the addresses in the YAML are illustrative.
2. CPU properties > Protection & Security > Connection mechanisms: enable **"Permit access with PUT/GET
   communication from remote partner"**.
3. Access level must allow it (full access, or a level without password for this access). With "Full access
   incl. fail-safe" protection, PUT/GET reads will be refused.
4. Port TCP 102 reachable from the Docker host.

**OPC UA**
1. CPU properties > OPC UA > Server: **Activate OPC UA server**. Default port 4840.
2. Server > Security: for first tests allow the *None* security policy and guest authentication (or create
   a user in Runtime licenses/User management). For production use `Basic256Sha256` + `SignAndEncrypt`
   with a client certificate: put the client cert/key in `./secrets`, then **trust** the client certificate
   in the PLC (Security features > Certificate manager) and trust the server certificate in the client.
3. OPC UA server needs a runtime license (SIMATIC S7-1500 OPC UA, size per firmware/CPU).
4. Make DB10 variables visible: the DB/variable needs "Accessible from HMI/OPC UA/Web API" (default on).
   Optimized DBs are fine for OPC UA, only S7comm needs non-optimized.
5. Discover node ids with `GET /opcua/browse` and put them into `opcua_node`.

## Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| S7: `connect ... failed` / timeout | IP wrong, firewall on 102, wrong rack/slot (try slot 0), PUT/GET off, another client occupies connections (S7-1500 has few). `Test-NetConnection IP -Port 102` |
| S7: connects but item `INVALID`/ response code error | DB still *optimized*, wrong DB number/offset, DB too short, access level blocks PUT/GET |
| S7 values look wrong | Offset mismatch vs compiled DB; arrays/structs shift offsets; REAL is big-endian IEEE-754 (handled) |
| S7 works in Python but not Go (or inverse) | Compare `/devices/{id}/status` last_error; Go uses plc4go with auto detection of controller |
| OPC UA: `BadSecurityPolicyRejected` / `BadSecurityModeRejected` | The policy is not enabled on the PLC; match `security_policy`/`security_mode` |
| OPC UA: `BadCertificateUntrusted` / `BadSecurityChecksFailed` | Trust the client cert in the PLC certificate manager; mount cert/key in `./secrets` |
| OPC UA: `BadUserAccessDenied` / `BadIdentityTokenRejected` | Set `OPCUA_USERNAME`/`OPCUA_PASSWORD`, or enable guest access on the PLC |
| OPC UA: `BadNodeIdUnknown` | Wrong namespace index or name: browse and copy the node id (quotes matter: `ns=3;s="DB"."Var"`) |
| Both down from the container only | Docker Desktop in Windows containers mode, VPN, or host firewall; the host must route to the PLC subnet |
| Dashboard `API unreachable` | Container not running: `docker compose ps`, `docker compose logs python` |
| After network loss | Channels show `COMM_FAILURE`, reconnect with backoff 1 s up to 30 s; `reconnects` counter increments on recovery |

Logs are structured (JSON). Polling is conservative (1 s default) and timeouts are bounded. The
application does not scan networks.

## Layout

```
docker-compose.yml  config/  dashboard/  secrets/
python/  app/{drivers,services,api}  tests/  tools/opcua_sim.py  Dockerfile
go/      cmd/s7poc  internal/{driver,acq,compare,api,config,model}  Dockerfile
```

Add Modbus TCP: implement `Driver` (Python `drivers/base.py`, Go `internal/driver/driver.go`), register it
in the factory. The API and dashboard are unchanged.
