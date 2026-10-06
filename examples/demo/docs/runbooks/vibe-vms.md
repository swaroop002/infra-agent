# Runbook: Vibe-coding VMs

Users create VMs from the portal. A boto3 script launches an EC2 instance tagged `Purpose=vibe-coding` and `Owner=<email>`, then Ansible installs code-server (port 8080) and the deployments manager (port 9000).

## ProvisionStatus tag
- `complete`: Ansible finished.
- `ansible-failed`: the playbook failed. Apps may be partially installed. Re-run the `vibe-provision` job with the instance id; users can't fix this themselves.

## VS Code not reachable
1. If EC2 status checks show `impaired`, the instance is unhealthy. The user should stop and start it from the portal, which moves it to new hardware.
2. If status checks are OK, the `code-server@<user>` service has probably crashed: `systemctl restart code-server@<user>` (platform team).

## Deployments manager not reachable
Usually the `deployments-manager` container failed to pull its image during provisioning. Check `ProvisionStatus` first.

## Stopped VMs
VMs auto-stop after 8 hours idle. Stopped is normal and not an outage.
