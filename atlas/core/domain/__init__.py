"""
domain/ — the innermost ring: immutable data with validation, no rules.

May not import policy/ or ports/ (asserted by test_domain_does_not_import_policy_or_ports).
"""
from atlas.core.domain.proxy import (
    Anonymity, Endpoint, InvalidProxy, LatencyProfile, Protocol, Proxy, ProxyState,
)
from atlas.core.domain.source import (
    ParserKind, Source, SourceState, SourceStats, Target,
)
from atlas.core.domain.verdict import Grade, ReasonCode, Score, Verdict

__all__ = [
    "Anonymity", "Endpoint", "InvalidProxy", "LatencyProfile", "Protocol",
    "Proxy", "ProxyState",
    "ParserKind", "Source", "SourceState", "SourceStats", "Target",
    "Grade", "ReasonCode", "Score", "Verdict",
]
