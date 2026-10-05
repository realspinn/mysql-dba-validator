from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TARGET_MODE_COMPANY = "COMPANY"
TARGET_MODE_LOCAL = "LOCAL"

_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_IPV4_LITERAL_RE = re.compile(r"^\d+(?:\.\d+){3}$")


@dataclass
class CompanyTarget:
    target_id: str
    display_name: str
    mode: str = TARGET_MODE_COMPANY
    host: str = ""
    port: int = 3306
    tls_required: bool = False
    tls_policy: str | None = None
    allowed_database_policy: str | None = None
    approval_state: str = "approved"
    approved_at: float | None = None
    dns_identity_snapshot: dict[str, Any] | None = None
    dns_snapshot_created_at: float | None = None
    tls_hostname: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


@dataclass
class CompanyTargetRegistry:
    targets: dict[str, CompanyTarget] = field(default_factory=dict)

    def register(self, target: CompanyTarget) -> CompanyTarget:
        validated = validate_company_target(target)
        if validated.approval_state == "approved" and validated.dns_identity_snapshot is None:
            validated.approval_state = "needs_reapproval"
        if validated.target_id in self.targets:
            raise ValueError("duplicate_company_target_id")
        self.targets[validated.target_id] = validated
        return validated

    def get(self, target_id: str) -> CompanyTarget | None:
        return self.targets.get(target_id)

    def is_approved(self, target_id: str) -> bool:
        target = self.targets.get(target_id)
        return bool(
            target
            and target.approval_state == "approved"
            and target.dns_identity_snapshot is not None
        )

    def list_approved(self) -> list[dict[str, Any]]:
        approved = []
        for target in self.targets.values():
            if target.approval_state == "approved":
                approved.append({
                    "target_id": target.target_id,
                    "display_name": target.display_name,
                    "mode": target.mode,
                    "host": target.host,
                    "port": target.port,
                    "tls_required": target.tls_required,
                })
        return approved


def validate_company_target(target: CompanyTarget | dict[str, Any]) -> CompanyTarget:
    if isinstance(target, dict):
        target = CompanyTarget(**target)

    if not isinstance(target, CompanyTarget):
        raise ValueError("invalid_company_target")

    target_id = (target.target_id or "").strip()
    if not target_id or not _TARGET_ID_RE.fullmatch(target_id):
        raise ValueError("invalid_company_target_id")

    display_name = (target.display_name or "").strip()
    if not display_name:
        raise ValueError("invalid_company_target_name")

    if target.mode != TARGET_MODE_COMPANY:
        raise ValueError("invalid_company_target_mode")

    host = (target.host or "").strip()
    if not host:
        raise ValueError("invalid_company_target_host")

    normalized = host.lower()
    if normalized in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}:
        raise ValueError("local_target_not_allowed_in_company_mode")
    if _IPV4_LITERAL_RE.fullmatch(host):
        raise ValueError("invalid_company_target_host")
    if "/" in host or " " in host or ":" in host:
        # This stage does not permit raw IP literals or other network-qualified values.
        # Hostnames are the policy object syntax we accept for explicit company targets.
        raise ValueError("invalid_company_target_host")
    if not host.replace("-", "").replace(".", "").replace("_", "").isalnum():
        raise ValueError("invalid_company_target_host")
    labels = host.split(".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid_company_target_host")
    if host.startswith(".") or host.endswith("."):
        raise ValueError("invalid_company_target_host")
    if any(label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError("invalid_company_target_host")

    if not isinstance(target.port, int) or not (1 <= target.port <= 65535):
        raise ValueError("invalid_company_target_port")

    if target.approval_state not in {"approved", "pending", "revoked", "needs_reapproval"}:
        raise ValueError("invalid_company_target_approval_state")

    if target.tls_required not in {True, False}:
        raise ValueError("invalid_company_target_tls_policy")

    return CompanyTarget(
        target_id=target_id,
        display_name=display_name,
        mode=target.mode,
        host=host,
        port=target.port,
        tls_required=bool(target.tls_required),
        tls_policy=target.tls_policy,
        allowed_database_policy=target.allowed_database_policy,
        approval_state=target.approval_state,
        approved_at=target.approved_at,
        dns_identity_snapshot=target.dns_identity_snapshot,
        dns_snapshot_created_at=target.dns_snapshot_created_at,
        tls_hostname=target.tls_hostname,
        created_at=target.created_at,
        updated_at=target.updated_at,
    )
