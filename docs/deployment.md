# Deployment

## Topology

```
 Teams ──▶ Azure Bot Service ──▶ https://infra-agent.<internal>/api/messages ─┐
 Browser ──▶ oauth2-proxy / Entra App Proxy ──▶ infra-agent :8080 ─────────────┤
                                                                               ▼
                                            infra-agent (API + UI + collector)
                                              │ read-only
             ┌────────────┬────────────┬──────┴──────┬───────────────┬──────────────┐
         Prometheus      Loki       Grafana API   OnCall API    AWS/Azure/GCP APIs   health endpoints
```

Run it where it can reach the private health endpoints and vibe VM IPs, for example as a
Deployment in one of the existing clusters (`monitoring` namespace), with egress to the other
cloud networks.

## Credentials (all read-only)

| System | Credential | Permission |
|---|---|---|
| Prometheus / Loki | token or basic auth (if fronted) | query only |
| Grafana | service account token | **Viewer** |
| Grafana OnCall / IRM | API token | read (alert groups) |
| AWS | IAM role (IRSA / instance profile) | `ec2:DescribeInstances`, `ec2:DescribeInstanceStatus` |
| Azure | managed identity / service principal | **Reader** on the VM subscriptions |
| GCP | workload identity service account | `roles/compute.viewer` |
| LLM provider | API key | — |
| Teams | Entra app registration (bot) | Bot Framework only |

AWS policy:

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus"], "Resource": "*"}]}
```

## Configuration

```bash
cp config/agent.example.yaml config/agent.yaml
export PROMETHEUS_URL=... LOKI_URL=... GRAFANA_URL=... GRAFANA_TOKEN=...
export ONCALL_API_URL=... ONCALL_API_TOKEN=...
export ANTHROPIC_API_KEY=...          # or the key for whichever provider llm.model uses
export LEDGER_URL=postgresql+psycopg://infra_agent:...@db/infra_agent   # production
infra-agent kb validate --live
infra-agent ledger backfill --hours 168
infra-agent serve
```

Scaling: the API is stateless given `memory.type: postgres`. Run **one** collector: either
keep `ledger.run_in_server: true` with a single replica, or set it to false and run
`infra-agent collector run` as its own single-replica Deployment.

## Microsoft Teams

1. **Entra ID**: register a single-tenant app and create a client secret.
2. **Azure Bot**: create an Azure Bot resource using that app ID, set the messaging endpoint to
   `https://<host>/api/messages`, and enable the **Microsoft Teams** channel.
3. **Environment**:
   ```bash
   CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<app id>
   CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<secret>
   CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<tenant id>
   ```
   Set `teams.enabled: true` in the config.
4. **App package**: edit `deploy/teams/manifest.json` (replace `${BOT_APP_ID}`, add
   `color.png` 192×192 and `outline.png` 32×32), zip the three files, and upload it via Teams
   Admin Center → *Manage apps*, or sideload it for testing.
5. In a channel: `@Infra Agent /alerts`, `@Infra Agent is the development server down?`

`/alerts`, `/status`, `/downtime`, `/health` and `/uptime` don't use the LLM. They answer
instantly and still work if the model provider is down.

Long LLM turns: `teams.long_running_messages: true` makes the SDK acknowledge immediately and
reply proactively, avoiding the channel's ~15 s timeout.

## Web UI authentication

- `server.auth.mode: trusted_header`: put the service behind oauth2-proxy (Entra ID OIDC) or
  Entra application proxy, which sets `X-Forwarded-Email`. Don't expose the pod directly.
- `api_key`: for service-to-service use; keys come from `INFRA_AGENT_API_KEYS` (comma separated).
- `none`: local development only.

## Optional channel notifications

Create a Teams Workflow "When a Teams webhook request is received → Post card in a channel",
put its URL in `TEAMS_WORKFLOW_URL`, and set `notifications.teams_workflow_url_env:
TEAMS_WORKFLOW_URL`. Every ledger DOWN/RECOVERED transition then posts an adaptive card. OnCall
remains the paging system.

## Container

```bash
docker build -t infra-agent:0.1.0 .
docker run -p 8080:8080 -v $PWD/config:/app/config -v $PWD/knowledge:/app/knowledge \
  --env-file .env infra-agent:0.1.0
```

## Operating it

- `GET /healthz` for liveness. `GET /api/meta` shows whether the model and collector are up.
- `LOG_LEVEL=INFO` logs collector transitions and backend errors.
- Ledger growth: roughly one sample row per health check per minute, pruned after
  `sample_retention_days`. Outage rows are kept.
- Cost control: `llm.max_tool_rounds` bounds tool loops per question, and `max_history_turns`
  bounds context size.
