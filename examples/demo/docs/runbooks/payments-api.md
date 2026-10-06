# Runbook: Payments API

Owner: payments team (payments-oncall) · Criticality: critical · SLO: 99.95% availability, 5xx rate ≤ 1%.

## Error rate high
1. Look at the top failing paths. `/v1/charges` failures with 502/504 usually mean the upstream bank-gateway is slow.
2. Check the bank-gateway status page and the `bank_gateway_latency_seconds` dashboard.
3. `connection pool exhausted` means DB saturation. Check `prod-db-01` and the pgbouncer pool.
4. If the 5xx rate stays above 5% for 10 minutes, page payments-oncall. The OnCall escalation is automatic at 2% for 15 minutes.

## Dependencies
- auth-service (token validation)
- prod-db-01 (PostgreSQL primary)
- bank-gateway (external)

## Past incidents
See postmortems/2026-08-14-payments-bank-timeouts.md.
