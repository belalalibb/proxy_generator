"""
PORTS — Protocol interfaces the outer layers implement (hexagonal boundary).

core/ declares WHAT it needs; adapters/ decide HOW. These are typing.Protocol, so
no adapter inherits from anything here and no import ever points inward-to-outward.

ClockPort exists for a measured reason: the legacy tree called time.sleep() 13
times inside its control flow, which makes cooldown and scheduling logic
untestable without actually waiting. Time is an injected dependency here, so
ADR-006's exponential cooldown is verified in microseconds.
"""
from __future__ import annotations

from atlas.core.ports.clock import ClockPort
from atlas.core.ports.probe import ProbePort, ProbeResult
from atlas.core.ports.source import SourcePort, SourceFetch
from atlas.core.ports.store import StorePort

__all__ = [
    "ClockPort",
    "ProbePort",
    "ProbeResult",
    "SourcePort",
    "SourceFetch",
    "StorePort",
]
