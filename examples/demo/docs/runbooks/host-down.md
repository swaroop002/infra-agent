# Runbook: VM down or unreachable

Applies to standalone VMs scraped by node_exporter (AWS, Azure, GCP).

## Triage by verdict

### UNREACHABLE (VM running, scrape failing)
1. Check the cloud console serial/boot diagnostics for a kernel panic or a hung OS.
2. From the bastion: `nc -zv <ip> 9100`. If it's refused, node_exporter has stopped: `systemctl status node_exporter`.
3. If SSH also times out, check security group / NSG / firewall changes in the last 24h (CloudTrail, Azure Activity Log, GCP Audit Logs).
4. Escalate to the owning team listed in the catalog if the application port is also unreachable.

### DOWN (VM stopped)
1. Find who stopped it: CloudTrail `StopInstances`, Azure Activity Log `deallocate`, GCP Audit Logs `compute.instances.stop`.
2. Scheduled stops for dev and staging VMs are expected from 22:00 to 07:00 IST. Don't escalate those.
3. For production, contact the owning team before starting it again.

### NOT_MONITORED
The VM is running but missing from Prometheus service discovery. Check the SD tags/labels (e.g. `prometheus=true`) on the instance.

## Disk pressure (DEGRADED)
- Clean up `/var/log/journal` with `journalctl --vacuum-time=7d`.
- On app servers, old releases live in `/opt/releases`. Keep the last 3.
