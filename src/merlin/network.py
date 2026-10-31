"""Shared validation for exact public domain authorizations."""

import ipaddress
import re


def validate_domains(domains: list[str], *, limit: int = 50) -> list[str]:
    if len(domains) > limit:
        raise ValueError(f"At most {limit} exact public domains may be authorized")
    normalized = []
    for domain in domains:
        value = domain.lower().rstrip(".")
        if (
            len(value) > 253
            or "." not in value
            or value.endswith((".local", ".internal", ".localhost", ".lan", ".home"))
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in value.split(".")
            )
        ):
            raise ValueError("Network authorization requires exact public domain names")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError("IP literals cannot be authorized")
        if value not in normalized:
            normalized.append(value)
    return normalized
