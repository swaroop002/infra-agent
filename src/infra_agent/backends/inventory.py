"""Read-only cloud inventory adapters.

Required permissions:
  AWS   ec2:DescribeInstances, ec2:DescribeInstanceStatus
  Azure "Reader" (Microsoft.Compute/virtualMachines/read, .../instanceView/read)
  GCP   roles/compute.viewer
SDK calls are synchronous, so they run in worker threads.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from ..knowledge.schema import CloudRef
from .base import BackendError, InstanceState, InstanceStateName, InventoryBackend

_AWS_STATES: dict[str, InstanceStateName] = {
    "running": "running", "stopped": "stopped", "pending": "starting", "stopping": "stopping",
    "shutting-down": "stopping", "terminated": "terminated",
}


class AwsInventory(InventoryBackend):
    provider = "aws"

    def __init__(self, *, regions: list[str] | None = None, profile: str | None = None,
                 role_arn: str | None = None, name: str = "aws"):
        try:
            import boto3  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise BackendError("AWS inventory needs boto3: pip install 'infra-agent[aws]'") from exc
        self.name = name
        self._regions = regions or []
        self._profile = profile
        self._role_arn = role_arn
        self._clients: dict[str, object] = {}

    def _ec2(self, region: str | None):
        import boto3

        region = region or (self._regions[0] if self._regions else None)
        key = region or "default"
        if key not in self._clients:
            session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
            if self._role_arn:
                creds = session.client("sts").assume_role(RoleArn=self._role_arn,
                                                          RoleSessionName="infra-agent-readonly")["Credentials"]
                session = boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                        aws_secret_access_key=creds["SecretAccessKey"],
                                        aws_session_token=creds["SessionToken"])
            self._clients[key] = session.client("ec2", region_name=region)
        return self._clients[key]

    async def get_instance(self, ref: CloudRef) -> InstanceState:
        return await asyncio.to_thread(self._get_instance, ref)

    def _get_instance(self, ref: CloudRef) -> InstanceState:
        from botocore.exceptions import BotoCoreError, ClientError

        ec2 = self._ec2(ref.region)
        try:
            resp = ec2.describe_instances(InstanceIds=[ref.instance_id])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidInstanceID.NotFound":
                return InstanceState("aws", ref.instance_id, None, "not_found", "not-found", ref.region)
            raise BackendError(f"AWS DescribeInstances failed: {exc}") from exc
        except BotoCoreError as exc:
            raise BackendError(f"AWS DescribeInstances failed: {exc}") from exc
        instances = [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
        if not instances:
            return InstanceState("aws", ref.instance_id, None, "not_found", "not-found", ref.region)
        state = self._to_state(instances[0], ref.region)
        state.status_checks = self._status_checks(ec2, [state.id]).get(state.id, {})
        return state

    async def list_instances(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        return await asyncio.to_thread(self._list_instances, tag_filter)

    def _list_instances(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        from botocore.exceptions import BotoCoreError, ClientError

        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in tag_filter.items()]
        filters.append({"Name": "instance-state-name",
                        "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]})
        out: list[InstanceState] = []
        for region in self._regions or [None]:
            ec2 = self._ec2(region)
            try:
                pages = ec2.get_paginator("describe_instances").paginate(Filters=filters)
                region_states = [self._to_state(i, region) for page in pages
                                 for r in page.get("Reservations", []) for i in r.get("Instances", [])]
                checks = self._status_checks(ec2, [s.id for s in region_states if s.state == "running"])
            except (ClientError, BotoCoreError) as exc:
                raise BackendError(f"AWS DescribeInstances failed in {region}: {exc}") from exc
            for s in region_states:
                s.status_checks = checks.get(s.id, {})
            out.extend(region_states)
        return out

    @staticmethod
    def _status_checks(ec2, ids: list[str]) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for i in range(0, len(ids), 100):
            resp = ec2.describe_instance_status(InstanceIds=ids[i:i + 100], IncludeAllInstances=True)
            for st in resp.get("InstanceStatuses", []):
                result[st["InstanceId"]] = {
                    "system": st.get("SystemStatus", {}).get("Status", "unknown"),
                    "instance": st.get("InstanceStatus", {}).get("Status", "unknown"),
                }
        return result

    @staticmethod
    def _to_state(inst: dict, region: str | None) -> InstanceState:
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        raw = inst.get("State", {}).get("Name", "unknown")
        launched = inst.get("LaunchTime")
        return InstanceState(
            provider="aws", id=inst["InstanceId"], name=tags.get("Name"),
            state=_AWS_STATES.get(raw, "unknown"), raw_state=raw,
            region=region or inst.get("Placement", {}).get("AvailabilityZone"),
            private_ip=inst.get("PrivateIpAddress"), public_ip=inst.get("PublicIpAddress"),
            launched_at=launched if isinstance(launched, datetime) else None,
            instance_type=inst.get("InstanceType"), tags=tags,
        )


class AzureInventory(InventoryBackend):
    """Azure VMs. Credentials come from DefaultAzureCredential (managed identity, env, CLI)."""

    provider = "azure"

    def __init__(self, *, subscription_id: str, name: str = "azure"):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.compute import ComputeManagementClient
        except ImportError as exc:  # pragma: no cover
            raise BackendError("Azure inventory needs: pip install 'infra-agent[azure]'") from exc
        self.name = name
        self._subscription = subscription_id
        self._client = ComputeManagementClient(DefaultAzureCredential(), subscription_id)

    async def get_instance(self, ref: CloudRef) -> InstanceState:
        return await asyncio.to_thread(self._get, ref.resource_group, ref.vm_name)

    def _get(self, resource_group: str, vm_name: str) -> InstanceState:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            vm = self._client.virtual_machines.get(resource_group, vm_name)
            view = self._client.virtual_machines.instance_view(resource_group, vm_name)
        except ResourceNotFoundError:
            return InstanceState("azure", f"{resource_group}/{vm_name}", vm_name, "not_found", "not-found")
        except AzureError as exc:
            raise BackendError(f"Azure VM lookup failed: {exc}") from exc
        return self._to_state(vm, view, resource_group)

    async def list_instances(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        return await asyncio.to_thread(self._list, tag_filter)

    def _list(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        from azure.core.exceptions import AzureError

        out = []
        try:
            for vm in self._client.virtual_machines.list_all():
                tags = vm.tags or {}
                if all(tags.get(k) == v for k, v in tag_filter.items()):
                    rg = vm.id.split("/resourceGroups/")[1].split("/")[0]
                    view = self._client.virtual_machines.instance_view(rg, vm.name)
                    out.append(self._to_state(vm, view, rg))
        except AzureError as exc:
            raise BackendError(f"Azure VM list failed: {exc}") from exc
        return out

    @staticmethod
    def _to_state(vm, view, resource_group: str) -> InstanceState:
        power = next((s.code.split("/", 1)[1] for s in (view.statuses or [])
                      if s.code and s.code.startswith("PowerState/")), "unknown")
        mapping: dict[str, InstanceStateName] = {
            "running": "running", "stopped": "stopped", "deallocated": "stopped",
            "starting": "starting", "stopping": "stopping", "deallocating": "stopping",
        }
        health = {}
        if getattr(view, "vm_health", None) and view.vm_health.status:
            health["vm_health"] = view.vm_health.status.code or "unknown"
        return InstanceState("azure", f"{resource_group}/{vm.name}", vm.name, mapping.get(power, "unknown"),
                             power, vm.location, tags=vm.tags or {}, status_checks=health,
                             instance_type=getattr(vm.hardware_profile, "vm_size", None))


class GcpInventory(InventoryBackend):
    """GCE instances. Credentials come from Application Default Credentials."""

    provider = "gcp"

    def __init__(self, *, projects: list[str], name: str = "gcp"):
        try:
            from google.cloud import compute_v1
        except ImportError as exc:  # pragma: no cover
            raise BackendError("GCP inventory needs: pip install 'infra-agent[gcp]'") from exc
        self.name = name
        self._projects = projects
        self._client = compute_v1.InstancesClient()

    async def get_instance(self, ref: CloudRef) -> InstanceState:
        return await asyncio.to_thread(self._get, ref)

    def _get(self, ref: CloudRef) -> InstanceState:
        from google.api_core.exceptions import GoogleAPIError, NotFound

        try:
            inst = self._client.get(project=ref.project, zone=ref.zone, instance=ref.instance_name)
        except NotFound:
            return InstanceState("gcp", ref.instance_name, ref.instance_name, "not_found", "NOT_FOUND", ref.zone)
        except GoogleAPIError as exc:
            raise BackendError(f"GCP instance lookup failed: {exc}") from exc
        return self._to_state(inst)

    async def list_instances(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        return await asyncio.to_thread(self._list, tag_filter)

    def _list(self, tag_filter: dict[str, str]) -> list[InstanceState]:
        from google.api_core.exceptions import GoogleAPIError

        # GCP "labels" play the role of tags; keys must be lowercase.
        flt = " AND ".join(f'labels.{k.lower()}="{v}"' for k, v in tag_filter.items())
        out = []
        try:
            for project in self._projects:
                for _zone, scoped in self._client.aggregated_list(request={"project": project, "filter": flt}):
                    out.extend(self._to_state(i) for i in scoped.instances)
        except GoogleAPIError as exc:
            raise BackendError(f"GCP instance list failed: {exc}") from exc
        return out

    @staticmethod
    def _to_state(inst) -> InstanceState:
        mapping: dict[str, InstanceStateName] = {
            "RUNNING": "running", "TERMINATED": "stopped", "SUSPENDED": "stopped", "STOPPING": "stopping",
            "SUSPENDING": "stopping", "PROVISIONING": "starting", "STAGING": "starting", "REPAIRING": "starting",
        }
        nic = inst.network_interfaces[0] if inst.network_interfaces else None
        public = None
        if nic and nic.access_configs:
            public = nic.access_configs[0].nat_i_p or None
        started = None
        if inst.last_start_timestamp:
            try:
                started = datetime.fromisoformat(inst.last_start_timestamp)
            except ValueError:
                started = None
        return InstanceState("gcp", inst.name, inst.name, mapping.get(inst.status, "unknown"), inst.status,
                             inst.zone.rsplit("/", 1)[-1] if inst.zone else None,
                             private_ip=nic.network_i_p if nic else None, public_ip=public,
                             launched_at=started, instance_type=inst.machine_type.rsplit("/", 1)[-1],
                             tags=dict(inst.labels))
