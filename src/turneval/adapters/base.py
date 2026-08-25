"""Provider adapters.

The scorer never imports a vendor SDK. It reads :class:`RawSession`, which is
plain data. Everything that knows what LiveKit calls a field lives behind this
interface, which is the entire reason this harness is portable: supporting
another voice stack means writing one more module in this package, not
touching the scoring layers.

A second, less obvious benefit: because the adapter boundary is where vendor
field names get translated, a provider renaming ``sequence_id`` to
``speech_id`` between SDK versions breaks exactly one file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from turneval.schema import Tokens, Turn


@dataclass(slots=True)
class RawSession:
    """One call, normalized, before any scoring has happened.

    This carries observation only. Nothing here is a verdict: whether the call
    succeeded is decided later, by running the scenario's outcome checks
    against the world, not by reading the transcript.
    """

    provider_session_id: str
    adapter: str
    started_at: str
    ended_at: str
    turns: list[Turn] = field(default_factory=list)
    tokens: Tokens = field(default_factory=Tokens)
    audio_seconds: float = 0.0
    wall_clock_ms: float = 0.0
    recording_ref: str | None = None
    #: Which scenario this call was running, when the harness drove it.
    scenario_id: str | None = None
    #: How the call ended, verbatim from the provider. Used for layer 3 signal:
    #: a caller-initiated disconnect mid-agent-turn is the abandonment tell.
    ended_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    """Turns provider output into :class:`RawSession` objects."""

    name: str

    def load(self, source: str | Path) -> list[RawSession]:
        """Read every session in ``source`` (a file or directory)."""
        ...


_REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> Adapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise KeyError(f"unknown adapter {name!r} (known: {known})") from None


def available() -> list[str]:
    return sorted(_REGISTRY)
