"""P02 registry tests.

Two jobs:
  1. Behaviour — the loader must ENFORCE its contract, so every rejection path is
     driven with a deliberately malformed registry (a validator that never
     rejects anything is not a validator).
  2. ADR-002 — no source URL literal may live in Python. Guarded by an AST scan
     with negative controls (ADR-012), so the guard can prove it still bites.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from atlas.adapters.registry import (
    Registry,
    RegistryError,
    SourceRow,
    load_registry,
)

ATLAS = pathlib.Path(__file__).resolve().parents[2]
REGISTRY_PATH = ATLAS / "data" / "sources" / "sources.json"
SNAPSHOT = ATLAS.parent / "engineering" / "raw" / "source_probe_20260827T222532Z.json"


# --------------------------------------------------------------------------- #
# real registry
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry(REGISTRY_PATH)


def test_registry_file_exists():
    assert REGISTRY_PATH.exists(), "P02 requires atlas/data/sources/sources.json"


def test_registry_loads_and_is_non_empty(reg: Registry):
    assert len(reg) > 0
    assert all(isinstance(r, SourceRow) for r in reg)


def test_enabled_count_equals_snapshot_active(reg: Registry):
    """The registry may not invent or lose sources relative to its evidence."""
    results = json.loads(SNAPSHOT.read_text())["results"]
    active = sum(
        1 for r in results if r["verdict"] in {"ALIVE", "ALIVE_JSON", "ALIVE_HTML_TABLE"}
    )
    assert len(reg.enabled()) == active


def test_every_disabled_row_names_a_reason(reg: Registry):
    """Core P02 contract: a DISABLED source must say WHY it is disabled."""
    nameless = [r.id for r in reg.disabled() if not (r.disabled_reason or "").strip()]
    assert nameless == [], f"DISABLED rows without a reason: {nameless}"


def test_every_enabled_row_has_a_known_parser(reg: Registry):
    assert all(r.parser in {"regex_adjacent", "json_path", "html_table"} for r in reg.enabled())


def test_ids_and_urls_are_unique(reg: Registry):
    ids = [r.id for r in reg]
    urls = [r.url for r in reg]
    assert len(set(ids)) == len(ids)
    assert len(set(urls)) == len(urls)


def test_no_protocol_label_is_claimed_verified(reg: Registry):
    """ADR-005: nothing has been probed yet, so no label may claim verification."""
    assert not any(r.label_is_verified for r in reg)
    assert not any(r.protocol_is_certain for r in reg)


def test_ambiguous_and_unknown_hints_are_representable(reg: Registry):
    """A corpus this messy MUST contain non-committal labels; collapsing them to a
    guess is the failure this test guards against."""
    hints = {r.labelled_protocol for r in reg}
    assert "unknown" in hints, "no `unknown` hint — the labeller is over-claiming"
    assert {"http", "https"} & hints


def test_every_hint_records_how_it_was_derived(reg: Registry):
    assert all(r.label_derivation for r in reg)


def test_socks_list_http_txt_is_ambiguous_not_guessed(reg: Registry):
    """Regression: TheSpeedX/SOCKS-List/master/http.txt says SOCKS in the repo name
    and http in the filename. Picking either would be a fabricated fact."""
    rows = [r for r in reg if "SOCKS-List" in r.url and r.url.endswith("http.txt")]
    if rows:  # only assert if that source is still in the corpus
        assert rows[0].labelled_protocol == "ambiguous"
        assert "conflicting" in rows[0].label_derivation


def test_ssl_filter_does_not_override_declared_proxytype(reg: Registry):
    """Regression: `proxytype=http&ssl=yes` is an HTTP list with an SSL filter.
    An earlier version labelled it `https` by matching `ssl`."""
    rows = [r for r in reg if "proxytype=http" in r.url and "ssl=" in r.url]
    for r in rows:
        assert r.labelled_protocol == "http", f"{r.url} mislabelled {r.labelled_protocol}"


def test_urls_helper_returns_only_enabled(reg: Registry):
    assert set(reg.urls()) == {r.url for r in reg.enabled()}


# --------------------------------------------------------------------------- #
# the loader must actually REJECT bad input
# --------------------------------------------------------------------------- #
def _write(tmp_path: pathlib.Path, doc: dict) -> pathlib.Path:
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(doc))
    return p


def _row(**over) -> dict:
    base = {
        "id": "example-1",
        "url": "https://example.invalid/list.txt",
        "state": "ENABLED",
        "parser": "regex_adjacent",
        "labelled_protocol": "http",
        "label_derivation": "path_token:http",
        "label_is_verified": False,
        "disabled_reason": None,
    }
    base.update(over)
    return base


def _doc(*rows: dict) -> dict:
    return {"schema_version": 1, "sources": list(rows)}


@pytest.mark.parametrize(
    "doc,fragment",
    [
        ({"schema_version": 99, "sources": [_row()]}, "schema_version"),
        ({"schema_version": 1, "sources": []}, "no `sources`"),
        (_doc(_row(state="MAYBE")), "invalid state"),
        (_doc(_row(state="DISABLED", disabled_reason=None)), "must name a disabled_reason"),
        (_doc(_row(state="DISABLED", disabled_reason="   ")), "must name a disabled_reason"),
        (_doc(_row(parser="telepathy")), "valid parser"),
        (_doc(_row(parser=None)), "valid parser"),
        (_doc(_row(labelled_protocol="carrier-pigeon")), "invalid labelled_protocol"),
        (_doc(_row(id="")), "missing required field `id`"),
        (_doc(_row(url="")), "missing required field `url`"),
        (_doc(_row(), _row()), "duplicate id"),
        (_doc(_row(), _row(id="other")), "duplicate url"),
    ],
)
def test_loader_rejects_malformed_registry(tmp_path, doc, fragment):
    with pytest.raises(RegistryError) as exc:
        load_registry(_write(tmp_path, doc))
    assert fragment in str(exc.value)


def test_loader_rejects_nonbool_label_is_verified(tmp_path):
    row = _row()
    row["label_is_verified"] = "false"  # a truthy string is the classic trap
    with pytest.raises(RegistryError, match="explicit bool"):
        load_registry(_write(tmp_path, _doc(row)))


def test_loader_accepts_a_valid_minimal_registry(tmp_path):
    """Negative control for the rejection tests: valid input must NOT raise, or
    the tests above would pass even if the loader rejected everything."""
    good = _doc(
        _row(),
        _row(id="d-1", url="https://example.invalid/dead.txt", state="DISABLED",
             parser=None, disabled_reason="probe_verdict_DEAD: http_404"),
    )
    reg = load_registry(_write(tmp_path, good))
    assert len(reg) == 2 and len(reg.enabled()) == 1 and len(reg.disabled()) == 1


def test_missing_file_raises(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "nope.json")


# --------------------------------------------------------------------------- #
# ADR-002 — sources are data, not literals
# --------------------------------------------------------------------------- #
_FETCHABLE = re.compile(r"^https?://[A-Za-z0-9]")


def _url_literals(tree: ast.AST) -> list[str]:
    """Executable string constants that look like fetchable source URLs.

    Docstrings/comments are excluded (ADR-012): documentation must stay free to
    reference a URL when explaining a decision.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        if not isinstance(node.value, str):
            continue
        v = node.value
        # A bare scheme prefix ("http://") is scheme VALIDATION, not a source URL.
        # `Source.url` legitimately does url.startswith(("http://", "https://")).
        # Require a host character after the scheme before calling it a URL.
        if _FETCHABLE.match(v):
            found.append(v)
    return found


def test_no_hardcoded_source_urls_in_python():
    offenders: dict[str, list[str]] = {}
    for py in sorted(ATLAS.rglob("*.py")):
        if "tests" in py.parts:  # tests legitimately use example.invalid fixtures
            continue
        hits = [u for u in _url_literals(ast.parse(py.read_text())) if "example.invalid" not in u]
        if hits:
            offenders[str(py.relative_to(ATLAS))] = hits
    assert offenders == {}, f"ADR-002 violated — URLs must live in sources.json: {offenders}"


def test_adr002_guard_has_teeth():
    """Negative control: the guard must fire on known-bad source (ADR-012)."""
    bad = 'SOURCES = ["https://raw.githubusercontent.com/x/y/list.txt"]'
    assert len(_url_literals(ast.parse(bad))) == 1

    bad_call = 'r = fetch("http://proxy.example.org/all.txt")'
    assert len(_url_literals(ast.parse(bad_call))) == 1

    ok_doc = '"""See https://example.org/spec for the format."""\nX = 1'
    assert _url_literals(ast.parse(ok_doc)) == []

    ok_data = 'URL = cfg["url"]'
    assert _url_literals(ast.parse(ok_data)) == []

    # A bare scheme prefix is validation, not a source (real case: Source.url).
    ok_scheme = 'ok = u.startswith(("http://", "https://"))'
    assert _url_literals(ast.parse(ok_scheme)) == []
    # ...but the guard must still catch a real URL that merely starts the same way.
    assert len(_url_literals(ast.parse('U = "https://a.example/list.txt"'))) == 1
