# Tenant policy enforcement
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class TenantPolicy:
    tenant_id: str
    llm_backend: str = "local"      # default backend
    privacy_locked: bool = False    # if True, never route to groq


class PolicyViolationError(Exception):
    pass


def enforce_policy(
    tenant_policy: Optional[TenantPolicy],
    requested_backend: Optional[str],
    default_backend: str,
) -> str:
    """
    Returns the effective backend string ("local" or "groq").

    Resolution order:
    1. If requested_backend is given, use it (after checking privacy lock).
    2. Else if tenant_policy is given, use tenant_policy.llm_backend.
    3. Else fall back to default_backend.

    Raises PolicyViolationError if the resolved backend is "groq" and the
    tenant has privacy_locked=True.
    """
    if requested_backend is not None:
        effective = requested_backend
    elif tenant_policy is not None:
        effective = tenant_policy.llm_backend
    else:
        effective = default_backend

    if tenant_policy is not None and tenant_policy.privacy_locked and effective == "groq":
        raise PolicyViolationError(
            f"Tenant '{tenant_policy.tenant_id}' is privacy-locked: routing to "
            "'groq' (an external cloud API) is not permitted. "
            "Use backend='local' or remove the privacy lock for this tenant."
        )

    return effective


# Default policy (no restrictions). The default tenant follows the
# deployment's LLM_BACKEND env rather than hardcoding a backend — otherwise a
# local/Ollama deployment would silently route the agents to Groq.
DEFAULT_POLICY = TenantPolicy(
    tenant_id="default",
    llm_backend=os.environ.get("LLM_BACKEND", "local"),
    privacy_locked=False,
)
