"""Keep existing Canvas credentials out of configs and reports."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from urllib.parse import urlsplit


class CredentialError(RuntimeError):
    pass


def normalize_token(value: str) -> str:
    token = value.strip().replace("\\~", "~")
    if not token or any(c.isspace() for c in token):
        raise CredentialError("Canvas token must be nonempty and contain no whitespace.")
    if "\x00" in token or "\r" in token or "\n" in token:
        raise CredentialError("Invalid token format.")
    return token


def service_name(origin: str) -> str:
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise CredentialError("Canvas origin must be an HTTPS host without credentials.")
    return "codex.canvas-notion-study." + parts.netloc.lower()


def save_token(origin: str, token: str, account: str = "canvas-student") -> None:
    if sys.platform != "darwin":
        raise CredentialError("Keychain storage requires macOS; supply CANVAS_TOKEN in the process environment instead.")
    result = subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U", "-a", account,
         "-s", service_name(origin), "-w", normalize_token(token)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise CredentialError("Could not store the credential in macOS Keychain.")


def load_token(origin: str, account: str = "canvas-student") -> str:
    value = os.environ.get("CANVAS_TOKEN")
    if value:
        return normalize_token(value)
    if sys.platform != "darwin":
        raise CredentialError("Set CANVAS_TOKEN in the process environment.")
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", account,
         "-s", service_name(origin), "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise CredentialError("No saved Canvas credential. Run auth-store or set CANVAS_TOKEN.")
    return normalize_token(result.stdout)


def prompt_store(origin: str, account: str = "canvas-student") -> None:
    token = getpass.getpass("Canvas token (hidden): ")
    save_token(origin, token, account)
    print("Canvas credential stored in macOS Keychain.")
