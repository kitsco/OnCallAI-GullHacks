"""Part C: running an APPROVED fix through SSM, and checking on it.

The agent never runs shell commands itself. It asks AWS Systems Manager to run the
OnCall-Remediate document on this one server, with parameters AWS checks against
an allowlist. The agent's IAM role can run that document and nothing else.
"""
import urllib.request

from agent.aws import ssm
from agent.config import INSTANCE_ID, SSM_DOCUMENT

_instance = {"id": INSTANCE_ID}


def instance_id() -> str:
    """This server's EC2 instance ID, from the instance metadata service (IMDSv2)."""
    if _instance["id"]:
        return _instance["id"]
    token_req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                       headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
    token = urllib.request.urlopen(token_req, timeout=2).read().decode()
    id_req = urllib.request.Request("http://169.254.169.254/latest/meta-data/instance-id",
                                    headers={"X-aws-ec2-metadata-token": token})
    _instance["id"] = urllib.request.urlopen(id_req, timeout=2).read().decode()
    return _instance["id"]


def run_fix(action: str, service: str, count=None) -> str:
    """Starts the fix. Returns the SSM command ID."""
    params = {"Action": [action], "Service": [service]}
    if action == "scale":
        params["Count"] = [str(count)]
    resp = ssm.send_command(
        InstanceIds=[instance_id()],
        DocumentName=SSM_DOCUMENT,
        Parameters=params,
        Comment=f"OnCall AI approved fix: {action} {service}"[:100],
        TimeoutSeconds=300,
    )
    return resp["Command"]["CommandId"]


def fix_status(command_id: str):
    """Returns (status, output). status: Pending/InProgress/Success/Failed/... ."""
    try:
        inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id())
    except ssm.exceptions.InvocationDoesNotExist:
        return "Pending", ""
    output = (inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", "")).strip()
    return inv["Status"], output[-600:]
