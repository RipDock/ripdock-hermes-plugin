# Contributing To RipDock Hermes Plugin

Thank you for helping improve the RipDock Hermes plugin.

This repository is the Hermes reference Runtime implementation for the RipDock
Protocol. Changes should keep Hermes-specific behavior in this repository and
should not redefine protocol behavior locally.

## Repository Scope

This repository owns:

- Hermes platform plugin registration
- Runtime identity, metadata, and local state handling
- Pairing, Device trust state, and Session handling for Hermes
- Agent discovery and Runtime dispatch through Hermes
- transfer and artifact handling for Hermes
- dashboard/admin integration for local Hermes management

Protocol contracts belong in
[`RipDock/ripdock-protocol`](https://github.com/RipDock/ripdock-protocol).
App behavior belongs in the App repositories.

## Terminology

Use these role names in public documentation and issues:

- App
- Runtime
- Agent
- Connector
- Session
- Pairing
- Device

Avoid adding new public role names for the same concepts.

## Making A Change

For documentation-only changes:

1. Update the relevant Markdown file.
2. Confirm public route descriptions still match the protocol repository.
3. Run the validation commands below before review.

For Runtime behavior changes:

1. Confirm the behavior is Hermes-specific.
2. Update or add focused unit tests.
3. Run the validation commands below.

For protocol behavior changes:

1. Update `ripdock-protocol` first.
2. Add or update protocol schemas, examples, and fixtures there.
3. Update this plugin only after the protocol change is accepted.

Do not add compatibility shims, alternate wire shapes, or implementation-only
protocol dialects in this repository.

## Security-Sensitive Changes

Treat these areas as security-sensitive:

- Runtime identity and private key storage
- Pairing request approval, rejection, expiry, and revocation
- Device trust state
- Session creation, resume, replay protection, and rotation
- transfer URLs, artifact paths, and artifact byte serving
- dashboard/admin routes and tunnel exposure
- logging of identifiers, tokens, Pairing material, or local paths

Security-sensitive changes should update `SECURITY.md` when they change the
operator guidance or threat boundary.

## Validation

Run:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
make lint PYTHON=.venv/bin/python
make test PYTHON=.venv/bin/python
```

The test suite uses Python `unittest` and focuses on Hermes plugin behavior,
route handling, protocol conformance expectations, and repository hygiene.
Runtime identity and signed Session resume require `cryptography`; missing
dependencies should fail validation rather than skip tests.

## Pull Requests

Pull requests should include:

- a concise summary
- affected Runtime, dashboard, Pairing, Device, Agent, transfer, or artifact
  behavior
- protocol impact, or an explicit statement that there is none
- validation performed
- any relevant security or privacy considerations

Do not include secrets, private keys, Session IDs, Pairing material, local
operator state, or private deployment URLs in issues, pull requests, fixtures,
or logs.
