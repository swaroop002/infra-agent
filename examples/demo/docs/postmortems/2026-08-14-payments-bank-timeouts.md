# Postmortem: Payments API 5xx spike caused by bank-gateway timeouts (2026-08-14)

## Summary
From 10:05 to 11:20 IST, payments-api returned 502/504 on 6.8% of `/v1/charges` requests. The bank-gateway's p99 latency rose above our 10s client timeout.

## Root cause
A certificate rotation on the bank's side made TLS handshakes slow. Our client had no connection reuse for that host.

## Detection
OnCall alert "payments-api 5xx rate above 2%" fired at 10:12 IST.

## Action items
- Enable HTTP keep-alive to bank-gateway (done).
- Add a circuit breaker with a fallback to the secondary acquirer (in progress).
- Alert on `bank_gateway_latency_seconds` p99 > 5s (done).
