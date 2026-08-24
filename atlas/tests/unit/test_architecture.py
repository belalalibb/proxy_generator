"""
ARCHITECTURAL FITNESS TESTS (§3 ACCEPTANCE).

These tests fail the build if the isolation rule is broken:

    atlas/core/ must not import adapters/, api/, engine/, or ANY network,
    database, or filesystem library.

Why a test and not a convention: a convention decays the moment someone needs
"just one quick import". This is enforced by AST analysis of every module under
atlas/core/, so it cannot be satisfied by a comment or a lint-disable.

Also enforced here (learned from the legacy audit, engineering/BUG_LEDGER.md):
  * no bare `except:`                      (legacy had 9)
  * no `except ...: pass` silent handlers   (legacy had 23)
  * no hardcoded proxy-source URLs in .py   (legacy had 257 URL literals)  -- §4
  * no CAPTCHA/2captcha references anywhere (H5 / §20)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ATLAS = Path(__file__).resolve().parents[2]
CORE = ATLAS / "core"

# ── libraries core/ may never touch ────────────────────────────────────────────
FORBIDDEN_IN_CORE = {
    # network
    "socket", "ssl", "http", "http.client", "urllib", "urllib.request", "urllib3",
    "requests", "aiohttp", "httpx", "httplib2", "socks", "socksio", "websockets",
    "ftplib", "telnetlib", "smtplib", "asyncio",
    # persistence / io
    "sqlite3", "psycopg2", "pymysql", "redis", "shelve", "dbm", "pickle",
    # filesystem / process / env
    "os", "io", "shutil", "pathlib", "tempfile", "glob", "subprocess",
    "multiprocessing", "threading", "signal", "fcntl", "mmap",
    # frameworks
    "fastapi", "starlette", "uvicorn", "flask", "django", "pydantic",
    # anything from our own outer layers
    "atlas.adapters", "atlas.api", "atlas.engine", "atlas.obs", "atlas.cli",
}

# stdlib that IS allowed in core/: pure computation only
ALLOWED_IN_CORE = {
    "dataclasses", "enum", "typing", "abc", "math", "statistics", "random",
    "datetime", "ipaddress", "re", "json", "hashlib", "uuid", "collections",
    "collections.abc", "functools", "itertools", "operator", "copy", "string",
    "decimal", "fractions", "bisect", "heapq", "textwrap", "unicodedata",
    "__future__", "types", "numbers", "contextlib", "warnings", "sys",
}


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in str(p))


def _imports(path: Path) -> list[tuple[str, int]]:
    """(top-level module name, lineno) for every import in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:      # relative import, stays in-package
                continue
            if node.module:
                out.append((node.module, node.lineno))
    return out


def _root_of(module: str) -> str:
    return module.split(".")[0]


# ══════════════════════════════════════════════════════════════════════════════
# 1. core/ isolation — the headline rule
# ══════════════════════════════════════════════════════════════════════════════
def test_core_has_no_io_or_network_imports() -> None:
    violations: list[str] = []
    for f in _py_files(CORE):
        for mod, line in _imports(f):
            root = _root_of(mod)
            if mod in FORBIDDEN_IN_CORE or root in FORBIDDEN_IN_CORE:
                violations.append(f"{f.relative_to(ATLAS)}:{line} imports '{mod}'")
    assert not violations, (
        "core/ must stay pure (§3 'قاعدة العزل الصارمة'). Violations:\n  "
        + "\n  ".join(violations)
    )


def test_core_imports_are_on_the_allowlist() -> None:
    """Stricter than the denylist: anything unexpected in core/ must be justified."""
    violations: list[str] = []
    for f in _py_files(CORE):
        for mod, line in _imports(f):
            root = _root_of(mod)
            if root == "atlas":
                # only atlas.core.* is permitted inside core/
                if not mod.startswith("atlas.core"):
                    violations.append(f"{f.relative_to(ATLAS)}:{line} imports '{mod}'")
                continue
            if mod not in ALLOWED_IN_CORE and root not in ALLOWED_IN_CORE:
                violations.append(
                    f"{f.relative_to(ATLAS)}:{line} imports '{mod}' (not on core allowlist)"
                )
    assert not violations, (
        "core/ may only use pure-computation stdlib. If a new import is genuinely "
        "pure, add it to ALLOWED_IN_CORE in this test and record an ADR.\n  "
        + "\n  ".join(violations)
    )


def test_core_modules_import_cleanly_without_side_effects() -> None:
    """
    Importing core/ must not touch the network, filesystem or global logging.
    Legacy v1.py:45 configured a FileHandler at import time (BUG_LEDGER B-14).
    """
    import importlib

    for f in _py_files(CORE):
        rel = f.relative_to(ATLAS.parent)
        mod = str(rel.with_suffix("")).replace("/", ".")
        importlib.import_module(mod)   # raises if the module has a bad import


# ── ADR-012: guards are callable scanners, so they can be negative-controlled ──
# Dunders the import machinery REQUIRES to be a list/dict. `__all__` must be a
# list of str by language convention; it cannot hold accumulated state. This is an
# exhaustive allowlist -- nothing else is exempt.
_MACHINERY_DUNDERS = frozenset({"__all__", "__slots__", "__match_args__"})


def scan_module_level_mutable_state(source: str, label: str = "<src>") -> list[str]:
    """
    Report module-level bindings to a mutable literal (list/dict/set).

    Exposed as a function so ADR-012's negative controls can feed it known-bad
    source and prove the guard still fires.
    """
    offenders: list[str] = []
    for node in ast.parse(source).body:              # module level only
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            if t.id in _MACHINERY_DUNDERS:           # ADR-012(b)
                continue
            if t.id.isupper():                       # UPPER_CASE = intended constant
                continue
            if isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
                offenders.append(f"{label}:{node.lineno} '{t.id}'")
    return offenders


def scan_prohibited_target_hosts(source: str, label: str = "<src>") -> list[str]:
    """
    Report prohibited hosts appearing in *executable* string values (ADR-012(a)).

    Docstrings are ignored -- and only docstrings. SECURITY.md REQUIRES the refusal
    to be documented at the code it governs, so a line-level regex could not tell a
    prohibition from a violation. Every form that can actually cause traffic --
    assignment, default argument, collection member, dict value, f-string -- still
    fails the build.
    """
    banned = re.compile(r"instagram\.com|facebook\.com|tiktok\.com|twitter\.com|x\.com/", re.I)
    tree = ast.parse(source)

    # collect the id() of every docstring node so they can be excluded precisely
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if banned.search(node.value):
                offenders.append(f"{label}:{node.lineno} {node.value[:60]!r}")
    return offenders


def test_core_declares_no_module_level_mutable_state() -> None:
    """Pure policy must not accumulate state between calls."""
    offenders: list[str] = []
    for f in _py_files(CORE):
        offenders += scan_module_level_mutable_state(
            f.read_text(encoding="utf-8"), str(f.relative_to(ATLAS))
        )
    assert not offenders, "mutable module-level state in core/:\n  " + "\n  ".join(offenders)


# ── negative controls: prove the relaxed guards CAN still fail (ADR-012(c)) ────
@pytest.mark.parametrize("name,src", [
    ("module_const", 'TEST_URL = "https://www.instagram.com"'),
    ("lowercase_var", 'target = "https://instagram.com/explore"'),
    ("default_arg",   'def probe(url="https://www.instagram.com"): pass'),
    ("list_item",     'TARGETS = ["https://example.com", "https://instagram.com"]'),
    ("dict_value",    'CFG = {"target": "https://www.facebook.com"}'),
    ("fstring",       'u = f"https://instagram.com/{name}"'),
    ("nested_call",   'probe(target="https://tiktok.com/foo")'),
])
def test_target_guard_still_fires_on_known_bad_source(name: str, src: str) -> None:
    """
    ADR-012 relaxed this guard to ignore docstrings. These cases prove the
    relaxation did not turn it into a no-op: every form that can cause real
    traffic must still be caught.
    """
    assert scan_prohibited_target_hosts(src, name), f"guard went blind on: {name}"


def test_target_guard_permits_a_docstring_citation() -> None:
    """
    The exemption is pinned: SECURITY.md requires the refusal to be documented at
    the code it governs, so citing the legacy default in a docstring is legal.
    """
    src = (
        'class Target:\n'
        '    """Required, never defaulted. Legacy defaulted to instagram.com (v1.py:29)."""\n'
        '    url: str\n'
    )
    assert scan_prohibited_target_hosts(src) == []


def test_mutable_state_guard_still_fires_and_exempts_only_machinery() -> None:
    """The __all__ exemption must not become a blanket dunder loophole."""
    assert scan_module_level_mutable_state('__all__ = ["A", "B"]') == []
    assert scan_module_level_mutable_state('__slots__ = ["a"]') == []
    assert scan_module_level_mutable_state('cache = {}'), "guard blind to dict state"
    assert scan_module_level_mutable_state('seen = []'), "guard blind to list state"
    assert scan_module_level_mutable_state('_pool = set()') == [], "set() call is not a literal"
    assert scan_module_level_mutable_state('__custom__ = []'), "only 3 dunders are exempt"


def test_guard_scan_set_is_not_empty() -> None:
    """
    Vacuity check inside the suite itself (ADR-010). A sync loss once left
    core/ empty while these tests reported success by globbing nothing.
    """
    assert len(_py_files(CORE)) >= 5, "core/ has too few modules -- tests may be vacuous"


# ══════════════════════════════════════════════════════════════════════════════
# 2. layering — outer layers may not be imported by inner ones
# ══════════════════════════════════════════════════════════════════════════════
def test_adapters_do_not_import_api_or_engine() -> None:
    """Adapters implement ports; they must not reach up into orchestration."""
    violations: list[str] = []
    adapters = ATLAS / "adapters"
    if not adapters.exists():
        pytest.skip("adapters/ not created yet")
    for f in _py_files(adapters):
        for mod, line in _imports(f):
            if mod.startswith(("atlas.api", "atlas.engine", "atlas.cli")):
                violations.append(f"{f.relative_to(ATLAS)}:{line} imports '{mod}'")
    assert not violations, "adapters/ must not import api/engine/cli:\n  " + "\n  ".join(violations)


def test_domain_does_not_import_policy_or_ports() -> None:
    """domain/ is the innermost ring: plain data, no rules, no interfaces."""
    violations: list[str] = []
    domain = CORE / "domain"
    if not domain.exists():
        pytest.skip("core/domain not created yet")
    for f in _py_files(domain):
        for mod, line in _imports(f):
            if mod.startswith(("atlas.core.policy", "atlas.core.ports")):
                violations.append(f"{f.relative_to(ATLAS)}:{line} imports '{mod}'")
    assert not violations, "core/domain must not import policy/ports:\n  " + "\n  ".join(violations)


# ══════════════════════════════════════════════════════════════════════════════
# 3. regression guards against the specific legacy defects we measured
# ══════════════════════════════════════════════════════════════════════════════
def _all_atlas_py() -> list[Path]:
    return [p for p in _py_files(ATLAS) if "tests" not in p.relative_to(ATLAS).parts]


def test_no_bare_except_anywhere() -> None:
    """Legacy had 9 bare `except:` (engineering/raw/bug_scan.json)."""
    offenders: list[str] = []
    for f in _all_atlas_py():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                offenders.append(f"{f.relative_to(ATLAS)}:{node.lineno}")
    assert not offenders, "bare `except:` is forbidden (BUG_LEDGER B-02):\n  " + "\n  ".join(offenders)


def test_no_silent_exception_handlers() -> None:
    """
    Legacy had 23 handlers whose entire body was `pass`/`continue`/`return <empty>`,
    which is why 35 dead source URLs were retried forever (BUG_LEDGER B-02).
    Every handler in v4 must do something observable: log, emit a reason-code,
    re-raise, or return a typed failure.
    """
    offenders: list[str] = []
    for f in _all_atlas_py():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if len(node.body) != 1:
                continue
            stmt = node.body[0]
            if isinstance(stmt, ast.Pass):
                offenders.append(f"{f.relative_to(ATLAS)}:{node.lineno} (pass)")
            elif isinstance(stmt, ast.Continue):
                offenders.append(f"{f.relative_to(ATLAS)}:{node.lineno} (continue)")
    assert not offenders, (
        "silent exception handler (BUG_LEDGER B-02). Emit a reason-code or log:\n  "
        + "\n  ".join(offenders)
    )


PROXY_SOURCE_URL = re.compile(
    r"https?://[^\s\"']*("
    r"proxy|proxies|socks|proxylist|freeproxy|openproxy|geonode|spys|hideip"
    r")[^\s\"']*",
    re.I,
)


def test_no_hardcoded_proxy_source_urls_in_code() -> None:
    """
    §4 H-RULE: hardcoding a source URL in a .py file is a contract breach.
    Legacy had 257 URL literals across 6 files (engineering/raw/bug_scan.json).
    Sources live in atlas/data/sources/sources.json.
    """
    offenders: list[str] = []
    for f in _all_atlas_py():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if PROXY_SOURCE_URL.search(node.value):
                    offenders.append(
                        f"{f.relative_to(ATLAS)}:{node.lineno} {node.value[:70]!r}"
                    )
    assert not offenders, (
        "proxy-source URL hardcoded in code (§4 / ADR-002). Move it to "
        "data/sources/sources.json:\n  " + "\n  ".join(offenders)
    )


def test_no_captcha_or_bypass_machinery() -> None:
    """
    H5 / §20. The legacy tree shipped a working 2captcha client
    (bebo.py:11-28, 10 mechanical matches). It must never reappear.
    """
    banned = re.compile(
        r"2captcha|anti-?captcha|capmonster|deathbycaptcha|recaptcha|hcaptcha"
        r"|captcha_id|captcha_solution|solve_captcha|cf_clearance|bypass_waf",
        re.I,
    )
    offenders: list[str] = []
    for f in _py_files(ATLAS):
        # this test file names them in order to ban them; skip itself
        if f.name == "test_architecture.py":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line):
                offenders.append(f"{f.relative_to(ATLAS)}:{i} {line.strip()[:70]}")
    assert not offenders, (
        "CAPTCHA/WAF-bypass machinery is prohibited (H5/§20, ADR-007, SECURITY.md):\n  "
        + "\n  ".join(offenders)
    )


def test_no_tls_verification_disabled() -> None:
    """
    Legacy disabled TLS verification in 9 places, making a MITM proxy
    indistinguishable from an honest one (BUG_LEDGER B-09).
    """
    banned = re.compile(r"verify\s*=\s*False|disable_warnings|CERT_NONE|check_hostname\s*=\s*False")
    offenders: list[str] = []
    for f in _all_atlas_py():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line) and "noqa: tls-intentional" not in line:
                offenders.append(f"{f.relative_to(ATLAS)}:{i} {line.strip()[:70]}")
    assert not offenders, (
        "TLS verification must stay on (BUG_LEDGER B-09). A probe that "
        "deliberately tests TLS failure must be tagged '# noqa: tls-intentional':\n  "
        + "\n  ".join(offenders)
    )


def test_no_default_target_url_constant() -> None:
    """
    H5 / ADR-007: there is no default target. Legacy shipped
    TEST_URL = "https://www.instagram.com" (v1.py:29, v3.py:30).
    """
    offenders: list[str] = []
    for f in _all_atlas_py():
        if f.name == "test_architecture.py":
            continue                      # names them to ban them; negative controls above
        offenders += scan_prohibited_target_hosts(
            f.read_text(encoding="utf-8"), str(f.relative_to(ATLAS))
        )
    assert not offenders, (
        "no ToS-hostile default target may be embedded in EXECUTABLE code "
        "(H5, ADR-007, ADR-012):\n  " + "\n  ".join(offenders)
    )
