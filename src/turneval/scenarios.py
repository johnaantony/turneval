"""Scenarios: what the caller wants, and how to check whether they got it.

The single design choice this whole harness rests on is that every scenario has
a **verifiable outcome**. A bare chat agent teaches you nothing about quality
because "was that a good call" has no ground truth. Give the agent one tool
whose effect you can read back off disk, and layer 1 stops being an opinion.

``expect`` is the ground truth. It is compared against the world after the call
by :mod:`turneval.scoring.layers`, never against the transcript, and never by a
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from turneval.taxonomy import Difficulty, ScenarioCategory


@dataclass(slots=True)
class Scenario:
    id: str
    title: str
    goal: str
    category: ScenarioCategory
    difficulty: Difficulty
    #: What the caller says and does, including the deliberate misbehavior.
    script: list[str] = field(default_factory=list)
    #: The record the agent should have written, or ``None`` for scenarios where
    #: the correct behavior is to write nothing at all.
    expect: dict[str, Any] | None = None
    #: Free-form note on what this scenario is probing. Goes into the writeup.
    probes: str = ""
    #: Set when the environment should be sabotaged before the call, e.g. to
    #: make the booking tool fail.
    fault: str | None = None


def load_scenarios(path: str | Path) -> list[Scenario]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [
        Scenario(
            id=item["id"],
            title=item["title"],
            goal=item["goal"],
            category=ScenarioCategory(item["category"]),
            difficulty=Difficulty(item.get("difficulty", "medium")),
            script=list(item.get("script") or []),
            expect=item.get("expect"),
            probes=item.get("probes", ""),
            fault=item.get("fault"),
        )
        for item in raw.get("scenarios", [])
    ]
