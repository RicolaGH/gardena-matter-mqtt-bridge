"""Shared transport and subprocess policy (no device secrets in argv)."""
import ipaddress
import os
import re


def validate_host(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("Invalid host")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("Invalid host")
    return host


def known_hosts_path():
    return os.environ.get("GARDENA_KNOWN_HOSTS", "/data/ssh/known_hosts_gardena")


def ssh_options():
    # TOFU: first contact is accepted; changed keys are always rejected.
    # This does not authenticate the first contact against an active MITM.
    return ["-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + known_hosts_path(),
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]


class Command(list):
    """List-compatible command with private stdin and environment attributes."""
    def __init__(self, args, *, input=None, env=None):
        super().__init__(args)
        self.input = input
        self.env = env
