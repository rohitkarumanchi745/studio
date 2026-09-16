#!/usr/bin/env python3
"""Create a private, non-overwriting environment file for the portable stack."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path


def token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def document() -> str:
    values = {
        "STUDIO_IMAGE": "studio:portable",
        "STUDIO_AIRFLOW_IMAGE": "studio-airflow:3.3.1",
        "STUDIO_SECRET": token(48),
        "STUDIO_ADMIN_EMAIL": "admin@studio.local",
        "STUDIO_ADMIN_PASSWORD": token(18),
        "STUDIO_DEMO_MODE": "0",
        "STUDIO_BIND_ADDRESS": "127.0.0.1",
        "STUDIO_DB_PASSWORD": token(24),
        "AIRFLOW_DB_PASSWORD": token(24),
        "WAREHOUSE_ADMIN_PASSWORD": token(24),
        "WAREHOUSE_READER_PASSWORD": token(24),
        "WAREHOUSE_PIPELINE_WRITER_PASSWORD": token(24),
        "AIRFLOW_FERNET_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "AIRFLOW_API_SECRET": token(48),
        "AIRFLOW_JWT_SECRET": token(48),
        "AIRFLOW_USERNAME": "studio",
        "AIRFLOW_PASSWORD": token(18),
        "AIRFLOW_PUBLIC_URL": "http://127.0.0.1:8080",
        "AGL_KEY": token(48),
        "STUDIO_AGL_RECOVERY_MODEL": "studio-recovery",
        "STUDIO_RECOVERY_GATEWAY_MODE": "langchain",
        "STUDIO_RECOVERY_UPSTREAM_MODEL": "anthropic:claude-sonnet-5",
        "STUDIO_BITNET_GATEWAY_KEY": token(48),
        "STUDIO_BITNET_GGUF_REVISION": "29f884c2aefd035cd498fa0750b7781e6f269032",
        "STUDIO_BITNET_GGUF_SHA256": "4221b252fdd5fd25e15847adfeb5ee88886506ba50b8a34548374492884c2162",
        "STUDIO_BITNET_ADAPTER_URL": "",
        "STUDIO_BITNET_ADAPTER_VERSION": "1",
        "STUDIO_BITNET_ADAPTER_SHA256": "",
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
    }
    return "".join(f"{key}={value}\n" for key, value in values.items())


def main() -> int:
    path = Path(__file__).with_name(".env")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        print(f"Refusing to overwrite {path}")
        return 1
    try:
        payload = document().encode("utf-8")
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    print(f"Created {path} with mode 0600. Add the provider key selected by STUDIO_LLM; "
          "BitNet mode also requires its adapter URL, version, and SHA-256.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
