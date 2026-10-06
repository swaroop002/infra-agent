# Building the knowledge base

The agent is only as good as what it knows about *our* estate. The knowledge base has three
parts:

| Part | What it answers | Location |
|---|---|---|
| **Catalog** (YAML) | "What is the development server? Which query is the payments error rate? Who owns report generation?" | `knowledge/catalog/*.yaml` |
| **Docs** (Markdown) | "What do we do when a worker is OOMKilled? What happened last time payments had 5xx?" | `knowledge/docs/**.md` |
| **Ledger** (database) | "How long was it down last month?" | built automatically by the collector |

You can have the catalog in place in an afternoon. After that, keep it current by adding to it
whenever someone asks a question the agent can't answer.

---

## Step 1 — Discover what exists

```bash
cp config/agent.example.yaml config/agent.yaml     # fill in URLs; secrets via env vars
infra-agent kb discover --vibe-tag Purpose=vibe-coding
```

This reads Prometheus targets, `node_uname_info`, kube-state-metrics, Loki labels (sampling log
lines to detect JSON/logfmt and status/path fields) and cloud inventory. It writes:

```
knowledge/catalog/_discovered/
  REPORT.md        ← read this first: targets per job, which labels exist, log formats found
  hosts.yaml       ← one draft per node_exporter target
  services.yaml    ← drafts from Kubernetes deployments and Loki streams
  clusters.yaml    ← clusters, namespaces, nodepool label
```

Files under `_discovered/` are ignored by the agent. Nothing is live until you promote an entry.

## Step 2 — Promote and enrich (the part that makes it smart)

Copy entries into `knowledge/catalog/hosts.yaml`, `services.yaml` and `platform.yaml` (any file
names work; all YAML files in the folder are merged), then add what discovery can't know:

### Hosts

```yaml
hosts:
  - id: dev-app-01                        # stable, lowercase; used in tools and the ledger
    aliases: [development server, dev server, dev box]   # ← the words people actually use
    description: Shared development application server
    environment: development
    team: platform
    cloud: aws
    prometheus: {job: node, instance: "10.10.1.15:9100"}  # exact labels of the node_exporter target
    cloud_ref: {provider: aws, instance_id: i-0a1b2c3d4e5f60001, region: ap-south-1}
    services: [dev-portal]
    runbooks: [runbooks/host-down.md]
```

- **aliases** are the most valuable field. Collect them from Slack/Teams history: "dev server",
  "UAT box", "the jenkins machine".
- **prometheus** is an explicit selector, so inconsistent labels don't matter. Values starting
  with `~` are regex (`instance: "~dev-app-01.*"`), which helps when IPs change.
- **cloud_ref** lets the agent tell *stopped* from *crashed* from *network issue*. Optional,
  but it turns "down" into a diagnosis.

### Services

```yaml
services:
  - id: payments-api
    name: Payments API
    aliases: [payments, payment api]
    criticality: critical                 # critical | high | medium | low
    team: payments
    kubernetes:
      - {cluster: prod-aks, namespace: payments, name: payments-api}   # kind defaults to deployment
    hosts: []                             # VMs it runs on, if any
    health_checks:
      - name: healthz
        url: https://payments.internal.example.com/healthz
        expect_status: [200]
        # expect_body_contains: '"status":"ok"'
        # headers_env: {Authorization: PAYMENTS_HEALTH_TOKEN}
    logs:
      selector: '{namespace="payments", app="payments-api"}'
      parser: "| json"
      request_filter: '|= "\"type\":\"access\""'   # only request lines, if the stream mixes log types
      error_filter: "| status >= 500"
      path_label: path
    depends_on: [auth-service]
    slo: {availability_pct: 99.95, error_rate_pct: 1.0}
    alert_match:
      labels: {service: payments-api}      # how alerts are related to this service
    runbooks: [runbooks/payments-api.md]
```

**API error rate from Loki.** The agent computes

```
errors = sum(count_over_time({selector} <request_filter> <parser> | __error__="" <error_filter> [window]))
total  = sum(count_over_time({selector} <request_filter> <parser> | __error__="" [window]))
```

Check the result in Grafana Explore before trusting it:

| Log style | parser | error_filter |
|---|---|---|
| JSON access log with `status` | `\| json` | `\| status >= 500` |
| JSON with nested `http.status_code` | `\| json status="http.status_code"` | `\| status >= 500` |
| logfmt | `\| logfmt` | `\| status >= 500` or `\| level="error"` |
| nginx combined | `` \| pattern `<_> - <_> [<_>] "<method> <path> <_>" <status> <_>` `` | `\| status >= 500` |
| No status codes | *(empty)* | `\|~ "(?i)(error\|exception)"` |

Decide explicitly whether 4xx counts as failure. Usually it doesn't: clients cause 4xx.

**Health checks** give uptime history beyond Prometheus's 7 days. Give every user-facing
service at least one. Use a cheap endpoint that exercises real dependencies. Checks are GET or
HEAD only and run from wherever the agent is deployed, so they need network reach.

### Clusters

```yaml
clusters:
  - id: prod-aks
    aliases: [aks, azure cluster]
    cloud: azure
    prometheus: {cluster: prod-aks}        # label that scopes kube-state-metrics series to this cluster
    nodepool_label: agentpool              # kube_node_labels label, without the "label_" prefix
```

Common nodepool labels (they need kube-state-metrics' `--metric-labels-allowlist=nodes=[...]`):
AKS `agentpool` · GKE `cloud_google_com_gke_nodepool` · EKS `eks_amazonaws_com_nodegroup`.
If both clusters are scraped by one Prometheus without a `cluster` label, add one with
`external_labels` or relabeling first.

### Vibe-coding platform

```yaml
vibe:
  provider: aws
  tag_filter: {Purpose: vibe-coding}       # tags your boto3 script sets on launch
  owner_tag: Owner
  provision_status_tag: ProvisionStatus    # set by Ansible success/failure handlers (recommended)
  address: private_ip                      # the agent must be able to reach this
  apps:
    - {name: vscode, port: 8080, path: /healthz}
    - {name: deployments-manager, port: 9000, path: /health}
```

Two cheap changes to the provisioning scripts make this much more useful:
1. Tag `Owner` with the user's email, so "Alice's VM" resolves.
2. Have Ansible set `ProvisionStatus=complete|ansible-failed` at the end of the playbook.

### Glossary

```yaml
glossary:
  RGS: report generation service
  PG: payments api
```

Glossary expansion runs before matching, so internal acronyms resolve.

## Step 3 — Validate against reality

```bash
infra-agent kb validate          # schema, references, duplicate aliases, missing runbooks
infra-agent kb validate --live   # every selector returns data, every health check responds
infra-agent kb search "report exports failing"   # how a question resolves to entities and docs
```

Run `kb validate` in CI on every change to `knowledge/`.

## Step 4 — Write the docs that matter

Markdown in `knowledge/docs/`, split by headings (each section is a search unit):

```
knowledge/docs/
  runbooks/<service>.md        symptoms → checks → mitigations → escalation
  runbooks/host-down.md        generic VM triage by verdict
  architecture/<system>.md     components, dependencies, data flow
  postmortems/YYYY-MM-DD-*.md  summary, root cause, detection, action items
  faq.md                       "why is X always red on Mondays?"
```

Guidelines:
- **One topic per heading**, with the symptom in the heading ("Pods OOMKilled / CrashLoopBackOff").
- **Include the exact strings** people and logs use: error messages, alert titles, metric names.
  Search is keyword-based unless embeddings are configured.
- **Link runbooks from the catalog** (`runbooks:`) so health answers cite them.
- Docs are shown to the model as untrusted data. Don't put instructions to the agent in them.

Optional: set `knowledge.embeddings` (e.g. `openai:text-embedding-3-large`) for hybrid search
once the doc set grows past a few dozen pages.

## Step 5 — Seed history and measure

```bash
infra-agent ledger backfill --hours 168   # import the last 7 days of host outages from Prometheus
infra-agent collector run                 # or leave ledger.run_in_server: true
```

Turn real questions into eval cases (`evals/cases.yaml`) as they come in. For your own estate,
copy the case format, point it at a staging config, and track the pass rate when you change
the catalog, prompts or model.

## Label conventions (recommended, not required)

Discovery's REPORT.md lists which labels are missing. Adding these via `relabel_configs` from
cloud SD metadata makes the catalog shorter and Grafana better:

| Label | Source |
|---|---|
| `env` | cloud tag `Environment` |
| `service` | cloud tag `Service`, or Kubernetes `app.kubernetes.io/name` |
| `team` | cloud tag `Team` |
| `cloud` | static per scrape job |

```yaml
- job_name: node-aws
  ec2_sd_configs: [{region: ap-south-1, port: 9100}]
  relabel_configs:
    - {source_labels: [__meta_ec2_tag_Name], target_label: instance_name}
    - {source_labels: [__meta_ec2_tag_Environment], target_label: env}
    - {source_labels: [__meta_ec2_tag_Team], target_label: team}
    - {target_label: cloud, replacement: aws}
```

## Maintenance checklist

- New VM or service → add a catalog entry in the same PR that creates it.
- Service renamed or moved → update selectors; `kb validate --live` catches drift.
- After an incident → add a postmortem, and runbook steps if new.
- Monthly → review "NOT_MONITORED" hosts and targets that aren't in the catalog (`list_down_hosts`).
