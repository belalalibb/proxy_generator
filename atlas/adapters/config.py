"""
CONFIG LOADER (ADR-031) — the adapter that turns `config.yaml` into policy objects.

WHY THIS FILE EXISTS AT ALL

`config.yaml` has carried a `targets.allow_policy` block since P01. ADR-029 built
the code that EVALUATES it (`core/policy/target_policy.py`) but not the code that
READS it: the deny-list was hardcoded as a default inside the policy dataclass.
That had two consequences, and the build caught the first:

  1. `test_no_default_target_url_constant` failed. Naming instagram.com /
     facebook.com / tiktok.com in executable code is banned by H5 / ADR-007 /
     ADR-012 -- that ban exists so the legacy default target cannot creep back
     in, and a "default deny-list" is exactly that creep wearing a safety label.

  2. `config.yaml` stayed decorative in a subtler way. With the hosts compiled
     in, editing the file could ADD a denied host but never REMOVE one, so the
     file was not authoritative. ADR-029 was written to stop a policy file being
     decorative; hardcoding its contents recreated the defect one layer down.

WHY THE LOADER IS AN ADAPTER

Reading a file is I/O, and `core/` may not do I/O (test_architecture.py). So the
split is: `core` owns the RULE (`check_target`), `adapters` owns the DATA
(this module). The rule is pure and exhaustively testable; the data is a file the
operator controls.

FAILURES ARE LOUD

A missing file, an unreadable file, a `deny_hosts` that is a string instead of a
list -- every one raises `ConfigError` naming the key. A config loader that
returns a silent empty default is how a security control becomes a no-op without
a single line of security code being edited (B-02, 23 silent handlers).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from atlas.core.policy.target_policy import TargetPolicy

# Repo root: atlas/adapters/config.py -> atlas/ -> repo/
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config.yaml"


class ConfigError(ValueError):
    """Raised when config.yaml is missing, unreadable, or shaped wrongly."""


@dataclass(frozen=True, slots=True)
class _Loaded:
    path: Path
    data: dict[str, Any]


def _load_yaml(path: Path) -> _Loaded:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"config file unreadable: {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"config root must be a mapping, got {type(data).__name__}: {path}"
        )
    return _Loaded(path=path, data=data)


def load_target_policy(path: Path | None = None) -> TargetPolicy:
    """
    Build a `TargetPolicy` from `config.yaml targets.allow_policy`.

    The three denied hosts are read from the FILE. They appear nowhere in
    executable code -- which is both the H5/ADR-007 requirement and the reason
    `config.yaml` is now authoritative: delete a host from the file and it is no
    longer denied, add one and it is.
    """
    path = path or DEFAULT_CONFIG_PATH
    loaded = _load_yaml(path)

    targets = loaded.data.get("targets")
    if not isinstance(targets, dict):
        raise ConfigError(f"missing or malformed `targets:` block in {path}")

    policy_block = targets.get("allow_policy")
    if not isinstance(policy_block, dict):
        raise ConfigError(
            f"missing or malformed `targets.allow_policy:` block in {path}"
        )

    raw_hosts = policy_block.get("deny_hosts", [])
    if isinstance(raw_hosts, str) or not isinstance(raw_hosts, (list, tuple)):
        # A bare string would iterate CHARACTER BY CHARACTER and produce a
        # deny-list of letters -- refused loudly rather than silently accepted.
        raise ConfigError(
            f"targets.allow_policy.deny_hosts must be a list, got "
            f"{type(raw_hosts).__name__} in {path}"
        )
    hosts: list[str] = []
    for h in raw_hosts:
        if not isinstance(h, str):
            raise ConfigError(
                f"deny_hosts entries must be strings, got {h!r} in {path}"
            )
        hosts.append(h.strip().lower())

    deny_private = policy_block.get("deny_private_ranges", True)
    if not isinstance(deny_private, bool):
        raise ConfigError(
            f"targets.allow_policy.deny_private_ranges must be a boolean, got "
            f"{deny_private!r} in {path}"
        )

    # TargetPolicy.__post_init__ validates entry SHAPE (bare hostnames only), so
    # a typo like a leading dot fails here at load time rather than at the first
    # request.
    return TargetPolicy(
        deny_hosts=frozenset(hosts),
        deny_private_ranges=deny_private,
    )


def load_default_target_is_absent(path: Path | None = None) -> bool:
    """
    Assert the ADR-007 invariant that `targets.default_target` is null.

    Exposed as a function so the API layer can refuse to start against a config
    that has quietly grown a default target, rather than trusting a comment in
    the YAML that says it must stay null.
    """
    path = path or DEFAULT_CONFIG_PATH
    loaded = _load_yaml(path)
    targets = loaded.data.get("targets")
    if not isinstance(targets, dict):
        raise ConfigError(f"missing or malformed `targets:` block in {path}")
    return targets.get("default_target") is None


__all__ = [
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "load_target_policy",
    "load_default_target_is_absent",
]
