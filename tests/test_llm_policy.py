"""Unit tests for libs/llm/policy.py"""

import sys
sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.llm.policy import TenantPolicy, PolicyViolationError, enforce_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _locked_tenant(tenant_id="acme-corp", default_backend="local"):
    return TenantPolicy(
        tenant_id=tenant_id,
        llm_backend=default_backend,
        privacy_locked=True,
    )


def _open_tenant(tenant_id="open-corp", default_backend="groq"):
    return TenantPolicy(
        tenant_id=tenant_id,
        llm_backend=default_backend,
        privacy_locked=False,
    )


# ---------------------------------------------------------------------------
# No tenant policy (tenant_policy=None)
# ---------------------------------------------------------------------------

class TestNoTenantPolicy:
    def test_no_policy_no_backend_returns_default(self):
        result = enforce_policy(
            tenant_policy=None,
            requested_backend=None,
            default_backend="local",
        )
        assert result == "local"

    def test_no_policy_explicit_groq_returns_groq(self):
        result = enforce_policy(
            tenant_policy=None,
            requested_backend="groq",
            default_backend="local",
        )
        assert result == "groq"

    def test_no_policy_explicit_local_returns_local(self):
        result = enforce_policy(
            tenant_policy=None,
            requested_backend="local",
            default_backend="groq",
        )
        assert result == "local"

    def test_no_policy_default_groq_returns_groq(self):
        result = enforce_policy(
            tenant_policy=None,
            requested_backend=None,
            default_backend="groq",
        )
        assert result == "groq"


# ---------------------------------------------------------------------------
# Privacy-locked tenant
# ---------------------------------------------------------------------------

class TestPrivacyLockedTenant:
    def test_locked_tenant_explicit_groq_raises(self):
        tenant = _locked_tenant()
        with pytest.raises(PolicyViolationError):
            enforce_policy(
                tenant_policy=tenant,
                requested_backend="groq",
                default_backend="local",
            )

    def test_locked_tenant_no_backend_returns_tenant_default(self):
        # privacy_locked=True, tenant default is "local" → should be "local"
        tenant = _locked_tenant(default_backend="local")
        result = enforce_policy(
            tenant_policy=tenant,
            requested_backend=None,
            default_backend="local",
        )
        assert result == "local"

    def test_locked_tenant_explicit_local_returns_local(self):
        tenant = _locked_tenant()
        result = enforce_policy(
            tenant_policy=tenant,
            requested_backend="local",
            default_backend="groq",
        )
        assert result == "local"

    def test_locked_tenant_groq_error_message_mentions_tenant(self):
        tenant = _locked_tenant(tenant_id="secret-org")
        with pytest.raises(PolicyViolationError) as exc_info:
            enforce_policy(
                tenant_policy=tenant,
                requested_backend="groq",
                default_backend="local",
            )
        assert "secret-org" in str(exc_info.value)

    def test_locked_tenant_groq_error_is_policy_violation(self):
        tenant = _locked_tenant()
        with pytest.raises(PolicyViolationError):
            enforce_policy(
                tenant_policy=tenant,
                requested_backend="groq",
                default_backend="local",
            )

    def test_locked_tenant_whose_own_default_is_groq_raises_when_no_request(self):
        # A privacy-locked tenant that somehow has groq as their default backend
        # should still be blocked (resolved backend is "groq" → raise)
        tenant = TenantPolicy(
            tenant_id="misconfigured",
            llm_backend="groq",
            privacy_locked=True,
        )
        with pytest.raises(PolicyViolationError):
            enforce_policy(
                tenant_policy=tenant,
                requested_backend=None,
                default_backend="local",
            )


# ---------------------------------------------------------------------------
# Non-locked tenant
# ---------------------------------------------------------------------------

class TestOpenTenant:
    def test_open_tenant_explicit_groq_returns_groq(self):
        tenant = _open_tenant()
        result = enforce_policy(
            tenant_policy=tenant,
            requested_backend="groq",
            default_backend="local",
        )
        assert result == "groq"

    def test_open_tenant_no_backend_returns_tenant_default(self):
        tenant = _open_tenant(default_backend="groq")
        result = enforce_policy(
            tenant_policy=tenant,
            requested_backend=None,
            default_backend="local",
        )
        assert result == "groq"

    def test_open_tenant_explicit_local_returns_local(self):
        tenant = _open_tenant()
        result = enforce_policy(
            tenant_policy=tenant,
            requested_backend="local",
            default_backend="groq",
        )
        assert result == "local"

    def test_open_tenant_does_not_raise_for_groq(self):
        tenant = _open_tenant()
        # Should complete without exception
        result = enforce_policy(
            tenant_policy=tenant,
            requested_backend="groq",
            default_backend="local",
        )
        assert result == "groq"


# ---------------------------------------------------------------------------
# TenantPolicy dataclass
# ---------------------------------------------------------------------------

class TestTenantPolicyDataclass:
    def test_defaults(self):
        policy = TenantPolicy(tenant_id="t1")
        assert policy.llm_backend == "local"
        assert policy.privacy_locked is False

    def test_custom_values(self):
        policy = TenantPolicy(
            tenant_id="t2",
            llm_backend="groq",
            privacy_locked=True,
        )
        assert policy.tenant_id == "t2"
        assert policy.llm_backend == "groq"
        assert policy.privacy_locked is True

    def test_policy_violation_error_is_exception(self):
        err = PolicyViolationError("test error")
        assert isinstance(err, Exception)
