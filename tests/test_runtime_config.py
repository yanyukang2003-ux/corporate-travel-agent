from __future__ import annotations

import pytest

from corporate_travel_agent.services.runtime_config import validate_deployment_environment


def test_development_profile_allows_ephemeral_demo_defaults() -> None:
    validate_deployment_environment({"ENVIRONMENT": "development"})


@pytest.mark.parametrize("profile", ["production", "prod", "staging"])
def test_production_profiles_reject_missing_security_and_durability(profile: str) -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL.*AUTH_ENABLED=true.*RAW_RESPONSE"):
        validate_deployment_environment({"ENVIRONMENT": profile})


def test_production_profile_accepts_explicit_required_configuration() -> None:
    validate_deployment_environment(
        {
            "ENVIRONMENT": "production",
            "DATABASE_URL": "postgresql+psycopg://db/travel",
            "AUTH_ENABLED": "true",
            "AUTH_USERS_FILE": "/run/secrets/auth-users.json",
            "AUTH_SIGNING_SECRET": "provided-by-secret-store",
            "RAW_RESPONSE_STORE_DIR": "/var/lib/travel-agent/raw",
        }
    )
