# RipDock Hermes Plugin

This repository contains the Hermes reference implementation for the RipDock
Protocol. It is a Hermes platform plugin.

## Purpose

The plugin adapts Hermes to the RipDock Protocol. It owns the Hermes-side
platform integration, Runtime dispatch, Pairing, dashboard state, and
protocol-facing message/transfer handling.

Protocol contracts live in
[`RipDock/ripdock-protocol`](https://github.com/RipDock/ripdock-protocol). This
repository implements the Hermes side of those contracts and should not redefine
protocol behavior locally.

## Requirements

- Python 3.12 for local validation and CI.
- Hermes with platform plugin support and a plugin directory that is scanned on
  restart or rescan.
- Python dependencies from `requirements-dev.txt`.

The plugin is loaded by Hermes from `plugin.yaml`. It is not currently packaged
as a standalone Python distribution.

## Installation

Local development install flow:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
mkdir -p ~/.hermes/plugins
rsync -a \
  --exclude .git \
  --exclude '.venv*' \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  --exclude codex-summary.log \
  --exclude runtime-identity.json \
  --exclude dashboard-state.json \
  --exclude session.json \
  --exclude public-runtime-url \
  --exclude ripdock \
  --exclude .hermes \
  --exclude artifacts \
  --exclude transfers \
  ./ ~/.hermes/plugins/ripdock/
```

Then restart Hermes or rescan plugins so Hermes reloads `plugin.yaml`.

The exact plugin directory is controlled by the Hermes installation. The
`~/.hermes/plugins/ripdock/` path above is the local development convention used
when Hermes scans plugins from the current user's home directory.

## Hermes dashboard integration

`plugin.yaml` registers this package as a Hermes platform plugin. The integration
exposes runtime identity, status, display settings, pairing state, and transfer
state for the Hermes dashboard integration. Keep dashboard-facing behavior in
the Hermes plugin layer unless it becomes shared protocol logic.

## Pairing flow

The plugin supports direct embedded Pairing through the Runtime endpoint. Pairing
state belongs to the Hermes plugin runtime and is persisted with the
runtime/session identity files used by the plugin.

## Route contract

The embedded Hermes endpoint exposes public App protocol routes and local
dashboard/admin routes. Public routes must stay aligned with `ripdock-protocol`.
Dashboard/admin routes are Hermes management routes and must not be published as
public App protocol routes.

HTTP JSON routes must return JSON and must not be routed to the WebSocket
handshake path. WebSocket-only routes require a WebSocket upgrade and carry
protocol events after the upgrade. When a tunnel proxy adapts Pairing POST
requests for the embedded endpoint, it must still reach these HTTP JSON handlers
and must not require `Upgrade`.

### Public App protocol routes

| Route | Purpose |
| --- | --- |
| `GET /.well-known/ripdock/runtime-identity` | Public Runtime identity. |
| `GET /.well-known/ripdock/runtime-metadata` | Public read-only Runtime-owned UI metadata. |
| `POST /ripdock/pairing/request` | Explicit Pairing approval request. |
| `POST /ripdock/pairing/status` | Read-only Pairing status by JSON body. |
| `GET /ripdock/app/pair/{pairingCode}` | Direct embedded Pairing socket. |
| `GET /ripdock/app` | Direct embedded Session socket. |
| `GET /ripdock/transfer/{transferId}/{role}` | Transfer socket for App/Runtime file chunks. |
| `GET /ripdock/transfer/{transferId}/artifact` | HTTP download for Runtime-generated artifact bytes. |

Pairing status responses use the stable Pairing result shape with `runtimeId`,
`deviceId`, `trustState`, and `message`. Trusted responses also include
`runtimeMetadata`, `runtimeAgents`, and `session_id` when chat is available.
Known status values are `pendingApproval`, `trusted`, `rejected`, `expired`,
`revoked`, and `notFound`.
Refresh/status routes are read-only and must not create a pending request.
`runtimeMetadata` comes from Runtime/plugin admin configuration. `icon` is an
emoji icon or `null` when unset. `accentColor` is `null` when unset, and
`backgroundColor` defaults to `#ffffff`.
For example:

```json
{
  "runtimeId": "runtime-id",
  "deviceId": "device-id",
  "trustState": "trusted",
  "message": "Device is trusted.",
  "runtimeMetadata": {
    "displayName": "Hermes",
    "icon": null,
    "accentColor": null,
    "backgroundColor": "#ffffff"
  },
  "runtimeAgents": [],
  "session_id": "runtime-session-id"
}
```

Unknown non-upgrade HTTP requests return JSON 404/error responses where the
embedded endpoint can identify the request as HTTP.

The artifact download route is intentionally HTTP, not a WebSocket upgrade.

### Local dashboard/admin routes

These routes are for local Hermes dashboard management. They are not public App
protocol routes and should not be exposed through an unauthenticated public
tunnel.

| Route | Purpose |
| --- | --- |
| `GET /ripdock/admin/state` | Dashboard/admin state. |
| `GET /ripdock/admin/conversations` | Dashboard/admin conversation snapshot. |
| `GET|POST /ripdock/admin/devices/{deviceId}/{approve|reject|revoke}` | Device admin action. |
| `POST /ripdock/admin/devices/{approve|reject|revoke}` | Device admin action with `deviceId` in the JSON body. |

## Local State And Runtime Identity

The plugin stores local Runtime identity, Pairing, Device trust, Session, and
dashboard state under the Hermes data directory. When `HERMES_HOME` is unset,
the plugin uses `$HOME/.hermes`, so the default Runtime identity file is:

```text
~/.hermes/ripdock/runtime-identity.json
```

If `HERMES_HOME` is set, the same files are stored under
`$HERMES_HOME/ripdock/`.

The Runtime identity includes the local Runtime private key. The plugin attempts
to write Runtime identity files with `0600` permissions. Keep the Hermes data
directory private and do not commit generated state files, private keys, Session
files, Pairing material, or private deployment URLs.

Operators may override local state paths with:

- `RIPDOCK_RUNTIME_IDENTITY_FILE`
- `RIPDOCK_DASHBOARD_STATE_FILE`
- `RIPDOCK_SESSION_FILE`
- `RIPDOCK_PUBLIC_RUNTIME_URL_FILE`

Security reporting and operator guidance live in `SECURITY.md`.

## Configuration

Common environment variables:

| Variable | Purpose |
| --- | --- |
| `HERMES_HOME` | Hermes data directory used for local Runtime, Session, Pairing, transfer, and dashboard state. |
| `RIPDOCK_RUNTIME_NAME` | Display name exposed in Runtime metadata when dashboard state has not overridden it. |
| `RIPDOCK_RUNTIME_IDENTITY_FILE` | Overrides the Runtime identity file path. |
| `RIPDOCK_DASHBOARD_STATE_FILE` | Overrides the dashboard state file path. |
| `RIPDOCK_SESSION_FILE` | Overrides the local Session state file path. |
| `RIPDOCK_PUBLIC_RUNTIME_URL` | Public, Device-facing HTTPS URL used for Pairing and public transfer URLs. |
| `RIPDOCK_PUBLIC_RUNTIME_URL_FILE` | File-based source for the public Runtime URL. |
| `RIPDOCK_DIRECT_RUNTIME_URL` | Direct Runtime URL used by embedded Pairing and transfer helpers. |

`RIPDOCK_PUBLIC_RUNTIME_URL` must be a Device-facing HTTPS URL. Localhost and
private/internal hosts are rejected for that public URL.

## Verification

After Hermes loads the plugin, verify the public Runtime routes from the
Device-facing origin:

```sh
curl -fsS https://runtime.example.com/.well-known/ripdock/runtime-identity
curl -fsS https://runtime.example.com/.well-known/ripdock/runtime-metadata
```

The responses should be JSON. The identity response must not expose private
keys, Pairing state, Device trust state, or dashboard state.

Keep dashboard/admin routes local or otherwise protected:

```text
/ripdock/admin/state
/ripdock/admin/conversations
/ripdock/admin/devices/{deviceId}/{approve|reject|revoke}
/ripdock/admin/devices/{approve|reject|revoke}
```

These routes are not public App protocol routes and should not be reachable
from an unauthenticated public tunnel.

## Development workflow

Useful commands:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
make help
make lint PYTHON=.venv/bin/python
make test PYTHON=.venv/bin/python
make clean
```

The Python dependencies are required; missing dependencies should fail validation
rather than skip tests.

Contribution guidelines live in `CONTRIBUTING.md`. This project is licensed
under the MIT License; see `LICENSE`.
