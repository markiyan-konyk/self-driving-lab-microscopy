"""Runtime context shared by all nodes and tools.

This holds the non-serialisable objects (the live scope, the LLM, the notebook)
that must NOT live in the graph state. Nodes and plain tool functions receive a
``Context``; only pure JSON-able data flows through ``AgentState``.
"""

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .notebook import Notebook


@dataclass
class Context:
    cfg: Settings
    scope: Any            # scopio_client.Scopio or mock_scope.MockScope
    nb: Notebook
    run_dir: str
    llm: Any = None       # LangChain chat model (None in offline mode)
    offline: bool = False # deterministic heuristics instead of the LLM
    _clip_seq: int = field(default=0, repr=False)
    _scene_seq: int = field(default=0, repr=False)

    def next_clip_id(self) -> str:
        self._clip_seq += 1
        return f"clip_{self._clip_seq:03d}"

    def next_scene_id(self) -> str:
        self._scene_seq += 1
        return f"scene_{self._scene_seq:03d}"
