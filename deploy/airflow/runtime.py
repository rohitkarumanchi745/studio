#!/usr/bin/env python3
"""Fail-closed, role-aware launcher for Studio's portable Airflow 3 image."""

from __future__ import annotations

import base64
import binascii
import os
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


SHARED_SENTINEL = ".studio-airflow-shared-v1"
SHARED_SENTINEL_CONTENT = "studio-airflow-shared-v1\n"
SERVICE_COMMANDS = {
    "api-server": ("airflow", "api-server"),
    "scheduler": ("airflow", "scheduler"),
    "dag-processor": ("airflow", "dag-processor"),
    "triggerer": ("airflow", "triggerer"),
}
ROLES = {"init", *SERVICE_COMMANDS}


class ConfigurationError(RuntimeError):
    """The container cannot safely start with its current environment."""


def _present(env: dict[str, str], *names: str) -> bool:
    return any(str(env.get(name, "")).strip() for name in names)


def _require_any(env: dict[str, str], label: str, *names: str) -> None:
    if not _present(env, *names):
        raise ConfigurationError(f"{label} is required; set one of: {', '.join(names)}")


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError("STUDIO_AIRFLOW_REQUIRE_SHARED_DAGS_SENTINEL must be true or false")


def _validate_fernet(value: str) -> None:
    # Airflow supports comma-separated keys during rotation.
    for encoded in value.split(","):
        try:
            decoded = base64.urlsafe_b64decode(encoded.strip().encode())
        except (ValueError, binascii.Error) as exc:
            raise ConfigurationError("AIRFLOW__CORE__FERNET_KEY is not a valid Fernet key") from exc
        if len(decoded) != 32:
            raise ConfigurationError("AIRFLOW__CORE__FERNET_KEY is not a valid Fernet key")


def _validate_secret(env: dict[str, str], prefix: str, label: str, *, fernet: bool = False) -> None:
    names = (prefix, f"{prefix}_CMD", f"{prefix}_SECRET")
    _require_any(env, label, *names)
    direct = env.get(prefix, "").strip()
    if not direct:
        return
    if fernet:
        _validate_fernet(direct)
    elif len(direct) < 32:
        raise ConfigurationError(f"{prefix} must contain at least 32 characters")


def _validate_database(env: dict[str, str]) -> None:
    prefix = "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
    _require_any(env, "Airflow's PostgreSQL metadata database", prefix, f"{prefix}_CMD", f"{prefix}_SECRET")
    direct = env.get(prefix, "").strip().lower()
    if direct and not direct.startswith(("postgresql://", "postgresql+")):
        raise ConfigurationError("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN must use PostgreSQL in production")


def _validate_execution_api(env: dict[str, str], role: str) -> None:
    if role not in {"scheduler", "triggerer"}:
        return
    name = "AIRFLOW__CORE__EXECUTION_API_SERVER_URL"
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required for the {role} role")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith("/execution/")):
        raise ConfigurationError(f"{name} must be an http(s) URL ending in /execution/")


def _dags_directory(env: dict[str, str]) -> Path:
    raw = env.get("AIRFLOW__CORE__DAGS_FOLDER", "/opt/airflow/dags").strip()
    directory = Path(raw)
    if not directory.is_absolute() or directory == Path("/"):
        raise ConfigurationError("AIRFLOW__CORE__DAGS_FOLDER must be an absolute, non-root directory")
    try:
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("AIRFLOW__CORE__DAGS_FOLDER must already exist") from exc
    if resolved != directory:
        raise ConfigurationError("AIRFLOW__CORE__DAGS_FOLDER may not contain symlink components")
    if not directory.is_dir():
        raise ConfigurationError("AIRFLOW__CORE__DAGS_FOLDER must be a directory")
    return directory


def _validate_shared_directory(env: dict[str, str], role: str) -> Path:
    directory = _dags_directory(env)
    if role == "init" and not os.access(directory, os.W_OK | os.X_OK):
        raise ConfigurationError("AIRFLOW__CORE__DAGS_FOLDER must be writable by the init role")
    require_sentinel = _truthy(env.get("STUDIO_AIRFLOW_REQUIRE_SHARED_DAGS_SENTINEL"), default=True)
    if require_sentinel and role in {"scheduler", "dag-processor"}:
        marker = directory / SHARED_SENTINEL
        try:
            content = _read_sentinel(marker)
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError(
                "shared DAG storage is not initialized; run the init role on the same mounted volume"
            ) from exc
        if content != SHARED_SENTINEL_CONTENT:
            raise ConfigurationError("shared DAG storage has an invalid runtime sentinel")
    return directory


def _read_sentinel(marker: Path) -> str:
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(marker, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
            raise OSError("shared DAG sentinel is not a bounded regular file")
        chunks = []
        remaining = 129
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks).decode("ascii")
    finally:
        os.close(fd)


def validate(role: str, env: dict[str, str] | None = None) -> Path:
    """Validate common production invariants and return the pinned DAG path."""
    values = dict(os.environ if env is None else env)
    if role not in ROLES:
        raise ConfigurationError(f"AIRFLOW_ROLE must be one of: {', '.join(sorted(ROLES))}")
    _validate_database(values)
    _validate_secret(values, "AIRFLOW__CORE__FERNET_KEY", "Airflow's Fernet key", fernet=True)
    _validate_secret(values, "AIRFLOW__API__SECRET_KEY", "Airflow's API session secret")
    if not _present(
        values,
        "AIRFLOW__API_AUTH__JWT_SECRET",
        "AIRFLOW__API_AUTH__JWT_SECRET_CMD",
        "AIRFLOW__API_AUTH__JWT_SECRET_SECRET",
        "AIRFLOW__API_AUTH__JWT_PRIVATE_KEY_PATH",
    ):
        raise ConfigurationError(
            "Airflow's shared JWT signing key is required; set AIRFLOW__API_AUTH__JWT_SECRET "
            "(or its _CMD/_SECRET variant or JWT_PRIVATE_KEY_PATH)"
        )
    direct_jwt = values.get("AIRFLOW__API_AUTH__JWT_SECRET", "").strip()
    if direct_jwt and len(direct_jwt) < 32:
        raise ConfigurationError("AIRFLOW__API_AUTH__JWT_SECRET must contain at least 32 characters")
    _validate_execution_api(values, role)
    return _validate_shared_directory(values, role)


def mark_shared(directory: Path) -> None:
    """Record that the one-shot initializer and service roles share this mount."""
    marker = directory / SHARED_SENTINEL
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(marker, flags, 0o444)
    except FileExistsError:
        try:
            content = _read_sentinel(marker)
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("refusing to replace an invalid shared DAG sentinel") from exc
        if content != SHARED_SENTINEL_CONTENT:
            raise ConfigurationError("refusing to replace an invalid shared DAG sentinel")
        return
    try:
        content = SHARED_SENTINEL_CONTENT.encode("ascii")
        offset = 0
        while offset < len(content):
            offset += os.write(fd, content[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def run(env: dict[str, str] | None = None) -> int:
    values = dict(os.environ if env is None else env)
    role = values.get("AIRFLOW_ROLE", "").strip()
    directory = validate(role, values)
    if role == "init":
        subprocess.run(("airflow", "db", "migrate"), env=values, check=True)
        mark_shared(directory)
        subprocess.run(("airflow", "db", "check"), env=values, check=True)
        return 0
    if role == "api-server" and values.get("PORT") and not values.get("AIRFLOW__API__PORT"):
        try:
            port = int(values["PORT"])
        except ValueError as exc:
            raise ConfigurationError("PORT must be an integer from 1 to 65535") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("PORT must be an integer from 1 to 65535")
        values["AIRFLOW__API__PORT"] = str(port)
    command = SERVICE_COMMANDS[role]
    os.execvpe(command[0], command, values)
    return 1  # pragma: no cover - execvpe does not return


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "run"
    try:
        if action == "run" and len(args) == 1:
            return run()
        if action == "preflight" and len(args) == 1:
            validate(os.getenv("AIRFLOW_ROLE", "").strip())
            return 0
        raise ConfigurationError("usage: runtime.py [run|preflight]")
    except (ConfigurationError, subprocess.CalledProcessError) as exc:
        print(f"airflow runtime refused to start: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
