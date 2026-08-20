from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shlex
from pathlib import Path

from corporate_travel_agent.services.auth import Role, hash_password
from corporate_travel_agent.services.policy_config import (
    LoadedPolicyConfiguration,
    load_policy_configuration,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create private internal-trial auth files without storing plaintext passwords."
    )
    parser.add_argument(
        "--users-file",
        default="data/runtime/auth-users.json",
        help="Output path for scrypt password hashes",
    )
    parser.add_argument(
        "--env-file",
        default="data/runtime/auth.env",
        help="Output path for shell environment settings",
    )
    parser.add_argument(
        "--policy-config",
        default=os.getenv("POLICY_CONFIG_FILE", "config/travel-policy.json"),
        help="Validated travel policy file used to derive employees and approvers",
    )
    parser.add_argument(
        "--admin-id",
        default="A9001",
        help="Local internal-trial administrator user ID",
    )
    args = parser.parse_args()

    policy_configuration = load_policy_configuration(args.policy_config)
    users = []
    for user_id, roles, employee_id in _configured_users(
        policy_configuration, args.admin_id
    ):
        password = _confirmed_password(user_id)
        users.append(
            {
                "user_id": user_id,
                "password_hash": hash_password(password),
                "roles": sorted(role.value for role in roles),
                "employee_id": employee_id,
                "active": True,
            }
        )

    users_path = Path(args.users_file).expanduser().resolve()
    env_path = Path(args.env_file).expanduser().resolve()
    _write_private_new_file(
        users_path,
        json.dumps({"users": users}, ensure_ascii=False, indent=2).encode("utf-8")
        + b"\n",
    )
    signing_secret = secrets.token_urlsafe(48)
    env_content = "\n".join(
        (
            "export AUTH_ENABLED=true",
            f"export AUTH_USERS_FILE={shlex.quote(str(users_path))}",
            f"export AUTH_SIGNING_SECRET={shlex.quote(signing_secret)}",
            "export AUTH_TOKEN_TTL_MINUTES=60",
            f"export POLICY_CONFIG_FILE={shlex.quote(policy_configuration.source_identifier)}",
            "",
        )
    ).encode("utf-8")
    try:
        _write_private_new_file(env_path, env_content)
    except Exception:
        users_path.unlink(missing_ok=True)
        raise

    print(f"Created private users file: {users_path}")
    print(f"Created private environment file: {env_path}")
    print(f"Run: source {shlex.quote(str(env_path))}")


def _configured_users(
    policy_configuration: LoadedPolicyConfiguration,
    admin_id: str,
) -> tuple[tuple[str, frozenset[Role], str | None], ...]:
    employees = tuple(
        (employee.employee_id, frozenset({Role.EMPLOYEE}), employee.employee_id)
        for employee in policy_configuration.employee_snapshots
    )
    approvers = tuple(
        (approver.approver_id, frozenset({Role.APPROVER}), None)
        for approver in policy_configuration.config.approvers
    )
    existing_ids = {item[0] for item in (*employees, *approvers)}
    if not admin_id or admin_id in existing_ids:
        raise SystemExit("Admin ID must be non-empty and distinct from policy identities")
    return (*employees, *approvers, (admin_id, frozenset({Role.ADMIN}), None))


def _confirmed_password(user_id: str) -> str:
    password = getpass.getpass(f"Password for {user_id} (minimum 12 characters): ")
    confirmation = getpass.getpass(f"Confirm password for {user_id}: ")
    if password != confirmation:
        raise SystemExit(f"Passwords do not match for {user_id}")
    if len(password) < 12:
        raise SystemExit(f"Password for {user_id} is shorter than 12 characters")
    return password


def _write_private_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"Refusing to overwrite existing file: {path}") from None
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
