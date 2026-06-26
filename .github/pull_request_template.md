## Summary

Describe the Hermes plugin change.

## Change Type

- [ ] Documentation only
- [ ] Runtime behavior
- [ ] Dashboard/admin behavior
- [ ] Pairing, Device trust, or Session behavior
- [ ] Agent discovery or dispatch
- [ ] Transfer or artifact behavior
- [ ] Test or repository hygiene

## Protocol Impact

Explain affected protocol routes, events, fields, security behavior, or
fixtures. If there is no protocol impact, state that explicitly.

## Security And Privacy Impact

Describe changes to Runtime identity, private keys, Pairing, Device trust,
Session resume, dashboard/admin routes, logging, transfers, or artifacts.

## Validation

- [ ] `make lint PYTHON=.venv/bin/python`
- [ ] `make test PYTHON=.venv/bin/python`

## Checklist

- [ ] Public route documentation still matches `ripdock-protocol`
- [ ] Dashboard/admin routes are not described as public App protocol routes
- [ ] Tests cover changed behavior
- [ ] No secrets, private keys, Session IDs, Pairing material, or deployment URLs
      were committed
- [ ] `SECURITY.md` was updated for security-sensitive operator guidance
