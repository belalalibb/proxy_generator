"""
CONFIG LOADER FAILURE BRANCHES (ADR-031, gap closed as P08.T0).

WHY THIS FILE EXISTS

`adapters/config.py` advertises, in its own docstring, that "failures are loud":
every malformed shape raises `ConfigError` naming the key, because a loader that
returns a silent empty default is how a security control becomes a no-op without
a single line of security code being edited (B-02: 23 silent handlers in legacy).

That property had NO test. The happy path was covered incidentally --
`test_target_policy.py` calls `load_target_policy()` against the real
`config.yaml` -- so all 8 raise-sites were implemented but unverified. Under
ADR-014 (documented != demonstrated) an untested safety claim is not a safety
claim, and this one is load-bearing: if `deny_hosts` silently became empty, every
deny-list test would still pass while the deny-list denied nothing.

THE CASE THAT MATTERS MOST

`deny_hosts: "instagram.com"` -- a string instead of a list. Python iterates a
string CHARACTER BY CHARACTER, so a tolerant loader would build a deny-list of
the letters {i,n,s,t,a,g,r,m,.,c,o}. That denies nothing real (no host is one
character) while LOOKING populated in a debug dump. It is refused loudly, and
`test_a_string_deny_list_is_refused_not_iterated` pins the reason.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.adapters.config import (
    ConfigError,
    DEFAULT_CONFIG_PATH,
    load_default_target_is_absent,
    load_target_policy,
)

# A minimal VALID config, used as the baseline every negative case mutates. Kept
# here rather than reusing config.yaml so a real-file edit cannot silently make
# these tests vacuous.
VALID = """
targets:
  default_target: null
  allow_policy:
    deny_hosts:
      - example-denied.com
    deny_private_ranges: true
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ── the baseline must actually work, or every negative test below is vacuous ──
def test_the_valid_baseline_loads(tmp_path: Path) -> None:
    """
    ADR-012's vacuity lesson: a suite of negative cases proves nothing if the
    POSITIVE case also fails -- everything would "raise as expected" for the
    wrong reason.
    """
    policy = load_target_policy(_write(tmp_path, VALID))
    assert policy.deny_hosts == frozenset({"example-denied.com"})
    assert policy.deny_private_ranges is True


# ── the 8 loud-failure branches ──────────────────────────────────────────────
def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_target_policy(tmp_path / "does-not-exist.yaml")
    assert "not found" in str(exc.value)


def test_invalid_yaml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, "targets: [unclosed\n"))
    assert "not valid YAML" in str(exc.value)


def test_a_non_mapping_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, "- just\n- a\n- list\n"))
    assert "must be a mapping" in str(exc.value)


def test_a_missing_targets_block_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, "something_else: 1\n"))
    assert "targets" in str(exc.value)


def test_a_missing_allow_policy_block_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, "targets:\n  default_target: null\n"))
    assert "allow_policy" in str(exc.value)


def test_a_string_deny_list_is_refused_not_iterated(tmp_path: Path) -> None:
    """
    THE CASE THIS FILE EXISTS FOR. `deny_hosts: "instagram.com"` must not be
    iterated into a deny-list of single characters -- which would deny nothing
    while looking populated.
    """
    bad = VALID.replace(
        "    deny_hosts:\n      - example-denied.com",
        '    deny_hosts: "example-denied.com"',
    )
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, bad))
    msg = str(exc.value)
    assert "must be a list" in msg and "str" in msg


def test_a_non_string_deny_entry_is_refused(tmp_path: Path) -> None:
    bad = VALID.replace("      - example-denied.com", "      - 12345")
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, bad))
    assert "must be strings" in str(exc.value)


def test_a_non_boolean_deny_private_ranges_is_refused(tmp_path: Path) -> None:
    bad = VALID.replace("deny_private_ranges: true", 'deny_private_ranges: "yes"')
    with pytest.raises(ConfigError) as exc:
        load_target_policy(_write(tmp_path, bad))
    assert "must be a boolean" in str(exc.value)


# ── normalisation and the ADR-007 invariant ──────────────────────────────────
def test_deny_hosts_are_normalised_so_case_cannot_bypass(tmp_path: Path) -> None:
    """
    A deny entry written `Instagram.COM ` must match the lowercased host the
    splitter produces (ADR-030), or the list is bypassable by capitalisation.
    """
    cfg = VALID.replace("      - example-denied.com", "      - '  Example-DENIED.com  '")
    policy = load_target_policy(_write(tmp_path, cfg))
    assert policy.deny_hosts == frozenset({"example-denied.com"})


def test_a_default_target_is_detected(tmp_path: Path) -> None:
    """
    ADR-007: no default target may exist. The API layer refuses to start when
    one appears, so this must report the truth rather than trust the YAML
    comment saying it stays null.
    """
    assert load_default_target_is_absent(_write(tmp_path, VALID)) is True
    grown = VALID.replace("default_target: null",
                          "default_target: http://example.com/")
    assert load_default_target_is_absent(_write(tmp_path, grown)) is False


def test_the_shipped_config_has_no_default_target() -> None:
    """The invariant, asserted against the file that actually ships."""
    assert load_default_target_is_absent(DEFAULT_CONFIG_PATH) is True
