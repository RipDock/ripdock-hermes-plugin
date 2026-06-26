# Security Policy

## Reporting A Vulnerability

Please report suspected security vulnerabilities privately by emailing:

security@ripdock.com

Do not open a public GitHub issue for vulnerabilities that could expose users,
implementations, credentials, private keys, Session IDs, Pairing material,
Device trust state, artifact URLs, local file paths, or deployment details.

## Scope

Security reports for this repository should focus on:

- Hermes Runtime identity handling
- local Runtime private key generation and persistence
- Pairing request approval, rejection, expiry, and revocation
- Device trust state
- Session creation, resume, replay prevention, and rotation
- transfer and artifact authorization
- dashboard/admin route exposure
- unsafe logging or fixture data

Protocol-level vulnerabilities should also be reported privately. If the issue
is caused by the protocol contract rather than this implementation, it may need
to be fixed in `ripdock-protocol` first.

## Local State And Private Keys

The plugin creates local Runtime identity and dashboard state files. When
`HERMES_HOME` is unset, the plugin uses `$HOME/.hermes`, so the default Runtime
identity file is:

```text
~/.hermes/ripdock/runtime-identity.json
```

If `HERMES_HOME` is set, state is stored under `$HERMES_HOME/ripdock/`.
Operators may override paths with:

- `RIPDOCK_RUNTIME_IDENTITY_FILE`
- `RIPDOCK_DASHBOARD_STATE_FILE`
- `RIPDOCK_SESSION_FILE`
- `RIPDOCK_PUBLIC_RUNTIME_URL_FILE`

The Runtime identity contains the Runtime private key used by this local
Runtime. The plugin attempts to write Runtime identity files with `0600`
permissions. Operators are responsible for keeping the Hermes data directory
private, excluding local state files from source control, and rotating identity
state if a private key is exposed.

Do not commit generated state files, private keys, Session files, Pairing
material, or public tunnel URLs from a private deployment.

## Dashboard And Admin Routes

Dashboard/admin routes are for local Hermes management. They must not be
published as public App protocol routes or exposed through an unauthenticated
public tunnel.

## Response

We aim to acknowledge receipt within 3 business days.

After acknowledgment, we will:

1. investigate the report
2. confirm whether the issue is implementation-specific or protocol-level
3. coordinate a fix, clarification, or mitigation when needed
4. coordinate disclosure timing with the reporter when disclosure is appropriate

Response times may vary with severity, complexity, and maintainer availability,
but reports involving active exploitation or credential exposure are prioritized.
