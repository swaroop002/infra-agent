# Runbook: Report Generation Service

Owner: reporting team · Criticality: high · Runs on: GKE `prod-gke/reports/report-generator` plus workers `prod-report-worker-01/02`.

## Architecture
The API pods accept export requests and queue jobs. The workers render PDF and Excel files and upload them to object storage. Large exports (over 200k rows) run in the pods' JVM, so memory spikes are expected.

## Symptoms and actions

### Pods OOMKilled / CrashLoopBackOff
- Known cause: very large Excel exports exceed the 2 GiB heap (`-Xmx2g`).
- Mitigation (needs a change request): raise the memory limit to 4Gi and set `-Xmx3g`, or pause the `monthly-ledger-export` schedule.
- Check the `/v2/reports/export` error rate. If it's above 10%, notify #reporting-support.

### Worker VM down
- Exports are routed to the remaining worker, so throughput halves. The queue grows if both workers are down.
- A stopped worker is often the result of the GCP committed-use schedule. Check the audit logs before starting it.

### Health check returns 503
The health endpoint returns 503 when fewer than 2 pods are ready, or when no worker is reachable.
