"""Bootstrap the catalog from what actually exists in Prometheus, Loki and the cloud.

Writes draft YAML + a label report into <catalog_dir>/_discovered/ (ignored by the loader).
Humans review, add aliases/owners/health checks, and move entries into the real catalog.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from ..backends import Backends
from ..backends.base import BackendError
from ..timeutil import utcnow

_ENV_KEYS = ("env", "environment", "stage", "tier")
_CLOUD_KEYS = ("cloud", "provider", "cloud_provider", "platform")
_STATUS_KEYS = ("status", "status_code", "statusCode", "http_status", "response_code", "code", "httpStatus")
_PATH_KEYS = ("path", "route", "uri", "endpoint", "request_path", "url", "http_path")
_STREAM_LABELS = ("app", "service_name", "service", "app_kubernetes_io_name", "job", "container")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-") or "unnamed"


def guess_cloud(labels: dict[str, str]) -> str:
    for k in _CLOUD_KEYS:
        v = labels.get(k, "").lower()
        for cloud in ("aws", "azure", "gcp"):
            if cloud in v:
                return cloud
    blob = " ".join(f"{k}={v}" for k, v in labels.items()).lower()
    if re.search(r"\bi-[0-9a-f]{8,}\b|ec2|amazonaws", blob):
        return "aws"
    if "azure" in blob or "cloudapp.net" in blob:
        return "azure"
    if "gce" in blob or "google" in blob or "gcp" in blob:
        return "gcp"
    return "unknown"


def detect_log_format(lines: list[str]) -> dict[str, Any]:
    """Guess parser, status field and path field from sample log lines."""
    if not lines:
        return {"parser": None}
    json_fields: Counter[str] = Counter()
    logfmt_fields: Counter[str] = Counter()
    json_ok = logfmt_ok = 0
    for line in lines:
        s = line.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    json_ok += 1
                    json_fields.update(obj.keys())
                    continue
            except ValueError:
                pass
        pairs = re.findall(r'(\w[\w.]*)=("[^"]*"|\S+)', s)
        if len(pairs) >= 3:
            logfmt_ok += 1
            logfmt_fields.update(k for k, _ in pairs)
    n = len(lines)
    if json_ok >= n * 0.6:
        parser, fields = "| json", json_fields
    elif logfmt_ok >= n * 0.6:
        parser, fields = "| logfmt", logfmt_fields
    else:
        return {"parser": "", "note": "unstructured lines; use a line filter such as |~ \"(?i)error\""}
    status = next((k for k in _STATUS_KEYS if fields.get(k)), None)
    path = next((k for k in _PATH_KEYS if fields.get(k)), None)
    level = next((k for k in ("level", "severity", "lvl", "log_level") if fields.get(k)), None)
    out: dict[str, Any] = {"parser": parser, "fields_seen": [k for k, _ in fields.most_common(15)]}
    if status:
        out["error_filter"] = f"| {status} >= 500"
    elif level:
        out["error_filter"] = f'| {level}=~"(?i)error|fatal|critical"'
    out["path_label"] = path
    return out


async def discover(backends: Backends, catalog_dir: Path, *, node_job_regex: str | None = None,
                   vibe_tags: dict[str, str] | None = None, log_sample_streams: int = 40) -> Path:
    out_dir = catalog_dir / "_discovered"
    out_dir.mkdir(parents=True, exist_ok=True)
    report: list[str] = ["# Discovery report", "", f"Generated {utcnow().isoformat(timespec='seconds')}", ""]
    hosts: list[dict] = []
    services: list[dict] = []
    clusters: dict[str, dict] = {}
    m, logs = backends.metrics, backends.logs

    # ------------------------------------------------------------ Prometheus
    if m is not None:
        try:
            targets = await m.targets()
        except BackendError as exc:
            targets = []
            report.append(f"⚠️ Prometheus targets failed: {exc}")
        by_job: dict[str, list] = defaultdict(list)
        for t in targets:
            by_job[t.labels.get("job", "?")].append(t)
        report += ["## Prometheus targets by job", "", "| job | targets | down | labels present (share of targets) |",
                   "|---|---|---|---|"]
        for job, items in sorted(by_job.items()):
            keys: Counter[str] = Counter()
            for t in items:
                keys.update(k for k in t.labels if k not in ("job", "instance"))
            share = ", ".join(f"`{k}` {100 * c // len(items)}%" for k, c in keys.most_common(10)) or "—"
            down = sum(1 for t in items if t.health != "up")
            report.append(f"| {job} | {len(items)} | {down} | {share} |")
        report.append("")

        nodenames: dict[str, str] = {}
        try:
            for s in await m.query("node_uname_info"):
                nodenames[s.labels.get("instance", "")] = s.labels.get("nodename", "")
        except BackendError as exc:
            report.append(f"⚠️ node_uname_info query failed: {exc}")
        node_re = re.compile(node_job_regex) if node_job_regex else None
        seen_ids: Counter[str] = Counter()
        for t in targets:
            job = t.labels.get("job", "")
            is_node = (node_re.fullmatch(job) if node_re else (":9100" in t.scrape_url or "node" in job))
            if not is_node:
                continue
            inst = t.labels.get("instance", "")
            name = nodenames.get(inst) or inst.split(":")[0]
            hid = slug(name)
            seen_ids[hid] += 1
            if seen_ids[hid] > 1:
                hid = f"{hid}-{seen_ids[hid]}"
            extra = {k: v for k, v in t.labels.items() if k not in ("job", "instance")}
            env = next((extra[k] for k in _ENV_KEYS if k in extra), None)
            host = {"id": hid, "name": name, "aliases": [], "description": "", "environment": env,
                    "team": None, "cloud": guess_cloud(t.labels), "prometheus": {"job": job, "instance": inst},
                    "tags": extra}
            hosts.append({k: v for k, v in host.items() if v not in (None, {}, "")})
        report.append(f"Drafted **{len(hosts)} hosts** from node_exporter targets.\n")

        missing = [k for k in ("env", "service", "team", "cloud")
                   if not any(k in t.labels for t in targets)]
        if missing:
            report += ["## Label recommendations", "",
                       f"No target carries: {', '.join('`' + k + '`' for k in missing)}. Add them in "
                       "`relabel_configs` from cloud SD metadata so alerts and dashboards can be grouped. The "
                       "agent does not require them because the catalog uses explicit selectors.", "",
                       "```yaml", "relabel_configs:", "  - source_labels: [__meta_ec2_tag_Environment]",
                       "    target_label: env", "  - source_labels: [__meta_ec2_tag_Team]", "    target_label: team",
                       "  - target_label: cloud", "    replacement: aws", "```", ""]

        # kube-state-metrics
        try:
            workloads = await m.query("count by (cluster, namespace, deployment) (kube_deployment_spec_replicas)")
            nodes = await m.query("kube_node_labels")
        except BackendError as exc:
            workloads, nodes = [], []
            report.append(f"⚠️ kube-state-metrics queries failed: {exc}")
        pool_keys = Counter(k for s in nodes for k in s.labels
                            if k.startswith("label_") and re.search(r"pool|nodegroup", k))
        for s in workloads:
            cid = slug(s.labels.get("cluster", "default-cluster"))
            cl = clusters.setdefault(cid, {"id": cid, "name": s.labels.get("cluster", cid), "aliases": [],
                                           "cloud": "unknown", "namespaces": set(),
                                           "prometheus": {"cluster": s.labels["cluster"]} if "cluster" in s.labels else {}})
            ns = s.labels.get("namespace", "default")
            cl["namespaces"].add(ns)
            dep = s.labels.get("deployment", "")
            if ns in ("kube-system", "monitoring", "cert-manager", "ingress-nginx", "gatekeeper-system"):
                continue
            services.append({"id": slug(dep), "name": dep, "aliases": [], "description": "", "team": None,
                             "kubernetes": [{"cluster": cid, "namespace": ns, "name": dep}], "health_checks": []})
        for cl in clusters.values():
            cl["namespaces"] = sorted(cl["namespaces"])
            if pool_keys:
                cl["nodepool_label"] = pool_keys.most_common(1)[0][0].removeprefix("label_")
        report.append(f"Drafted **{len(clusters)} clusters** and **{len(services)} services** from kube-state-metrics.\n")

    # ------------------------------------------------------------------ Loki
    if logs is not None:
        now = utcnow()
        start = now - timedelta(hours=1)
        try:
            names = await logs.label_names(start, now)
        except BackendError as exc:
            names = []
            report.append(f"⚠️ Loki labels failed: {exc}")
        report += ["## Loki", "", f"Stream labels: {', '.join('`' + n + '`' for n in names) or 'none'}", ""]
        stream_label = next((label for label in _STREAM_LABELS if label in names), None)
        if stream_label:
            values = await logs.label_values(stream_label, start, now)
            report += [f"Using `{stream_label}` as the service stream label ({len(values)} values).", "",
                       "| stream | parser | error filter | path label |", "|---|---|---|---|"]
            by_id = {s["id"]: s for s in services}
            for value in values[:log_sample_streams]:
                selector = f'{{{stream_label}="{value}"}}'
                try:
                    sample = await logs.query_logs(selector, start, now, limit=25)
                except BackendError:
                    continue
                fmt = detect_log_format([s.line for s in sample])
                report.append(f"| `{selector}` | {fmt.get('parser') or 'raw'} | {fmt.get('error_filter', '—')} | "
                              f"{fmt.get('path_label') or '—'} |")
                signal = {"selector": selector, "parser": fmt.get("parser") or "",
                          "error_filter": fmt.get("error_filter", '|~ "(?i)error"'), "path_label": fmt.get("path_label")}
                sid = slug(value)
                if sid in by_id:
                    by_id[sid]["logs"] = signal
                else:
                    svc = {"id": sid, "name": value, "aliases": [], "description": "", "team": None,
                           "health_checks": [], "logs": signal}
                    services.append(svc)
                    by_id[sid] = svc
            report.append("")

    # ----------------------------------------------------------------- vibe
    if vibe_tags:
        for inv in backends.inventory:
            try:
                vms = await inv.list_instances(vibe_tags)
                states = Counter(v.state for v in vms)
                report.append(f"Vibe VMs via {inv.name} tagged {vibe_tags}: {dict(states)}")
            except BackendError as exc:
                report.append(f"⚠️ {inv.name} inventory failed: {exc}")

    header = ("# DRAFT generated by `infra-agent kb discover`. Review, add aliases/owners/health checks,\n"
              "# then move entries into knowledge/catalog/. Files in _discovered/ are ignored by the agent.\n")
    for fname, key, items in (("hosts.yaml", "hosts", hosts), ("services.yaml", "services", services),
                              ("clusters.yaml", "clusters", list(clusters.values()))):
        (out_dir / fname).write_text(header + yaml.safe_dump({key: items}, sort_keys=False, allow_unicode=True))
    report += ["## Next steps", "", "1. Review the draft files in this folder.",
               "2. Add `aliases` people actually use (\"development server\", \"payments\").",
               "3. Add `health_checks`, `owner`/`team`, `slo` and `depends_on` for important services.",
               "4. Move the reviewed entries into `knowledge/catalog/` and run `infra-agent kb validate --live`."]
    (out_dir / "REPORT.md").write_text("\n".join(report) + "\n")
    return out_dir
