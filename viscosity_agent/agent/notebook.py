"""The run's lab notebook -- the single source of truth for what happened.

Three synchronised outputs, all under ``run_dir``:
    notebook.jsonl  append-only event stream (the dashboard tails this)
    notebook.md     human-readable markdown mirror
    state.json      latest trimmed snapshot of the graph state (dashboard headline)

Everything the agent does -- every tool call, decision, metric, result, error --
goes through here, so the notebook alone fully explains a run. Thread-safe:
tool threads and the graph write concurrently while the dashboard reads.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

try:                                     # pretty console is optional
    from rich.console import Console
    from rich.theme import Theme
    _RICH = True
except Exception:                        # pragma: no cover - fallback path
    _RICH = False

_KIND_STYLE = {
    "phase":    ("bold cyan",    "###"),
    "decision": ("bold magenta", "->"),
    "tool":     ("green",        " ·"),
    "metric":   ("yellow",       " ·"),
    "result":   ("bold green",   "**"),
    "note":     ("dim",          " ·"),
    "error":    ("bold red",     "!!"),
    "snapshot": ("blue",         " ·"),
}


class Notebook:
    def __init__(self, run_dir: str, echo: bool = True):
        self.run_dir = run_dir
        self.echo = echo
        os.makedirs(run_dir, exist_ok=True)
        self._jsonl = os.path.join(run_dir, "notebook.jsonl")
        self._md = os.path.join(run_dir, "notebook.md")
        self._state = os.path.join(run_dir, "state.json")
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._seq = 0
        if _RICH and echo:
            self._console = Console(theme=Theme({
                k: v[0] for k, v in _KIND_STYLE.items()}))
        else:
            self._console = None
        # start the markdown file with a header
        with open(self._md, "a", encoding="utf-8") as f:
            f.write(f"\n# Viscosity agent notebook\n\n"
                    f"_started {datetime.now().isoformat(timespec='seconds')}_\n\n")

    # ------------------------------------------------------------ core
    def log(self, kind: str, msg: str, data: dict | None = None):
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "t": time.time(),
                "elapsed_s": round(time.monotonic() - self._t0, 2),
                "iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "kind": kind,
                "msg": msg,
                "data": data or {},
            }
            with open(self._jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, default=_json_default) + "\n")
            self._append_md(ev)
        if self.echo:
            self._echo(ev)
        return ev

    def _append_md(self, ev):
        style, marker = _KIND_STYLE.get(ev["kind"], ("", "·"))
        line = f"- `{ev['elapsed_s']:>7.1f}s` **{ev['kind']}** {marker} {ev['msg']}"
        with open(self._md, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if ev["data"]:
                compact = json.dumps(ev["data"], default=_json_default)
                if len(compact) <= 400:
                    f.write(f"    - `{compact}`\n")

    def _echo(self, ev):
        style, marker = _KIND_STYLE.get(ev["kind"], ("", "·"))
        text = f"{ev['elapsed_s']:>7.1f}s {marker} {ev['msg']}"
        if self._console is not None:
            self._console.print(text, style=style)
        else:                            # plain fallback
            print(f"[{ev['kind']:>8}] {text}")

    # ------------------------------------------------------------ sugar
    def phase(self, name: str, **data):
        return self.log("phase", name, data or None)

    def tool(self, name: str, args: dict, result_summary: str, **extra):
        data = {"tool": name, "args": _trim(args)}
        data.update(extra)
        return self.log("tool", f"{name}({_fmt_args(args)}) -> {result_summary}", data)

    def decision(self, name: str, obj: dict):
        head = obj.get("action") or obj.get("verdict") or "?"
        why = obj.get("reasoning", "")
        return self.log("decision", f"{name}: {head} — {why}", {name: obj})

    def metric(self, msg: str, scene: dict):
        return self.log("metric", msg, {"scene": scene})

    def result(self, msg: str, **data):
        return self.log("result", msg, data or None)

    def note(self, msg: str, **data):
        return self.log("note", msg, data or None)

    def error(self, msg: str, **data):
        return self.log("error", msg, data or None)

    def snapshot(self, path: str, caption: str = ""):
        rel = os.path.relpath(path, self.run_dir).replace(os.sep, "/")
        return self.log("snapshot", caption or f"snapshot {rel}", {"path": rel})

    # ------------------------------------------------------------ state snapshot
    def set_state(self, state: dict):
        """Write a trimmed, JSON-safe copy of the graph state to state.json."""
        trimmed = {
            "status": state.get("status"),
            "provider_model": state.get("provider_model"),
            "dry_run": state.get("dry_run"),
            "um_per_px": state.get("um_per_px"),
            "calibration_source": state.get("calibration_source"),
            "iteration": state.get("iteration"),
            "survey_attempts": state.get("survey_attempts"),
            "scene": state.get("scene"),
            "scene_decision": state.get("scene_decision"),
            "critique": state.get("critique"),
            "aggregate": state.get("aggregate"),
            "n_clips": len(state.get("clips", [])),
            "n_results": len(state.get("results", [])),
            "usage": state.get("usage"),
            "fov_history": state.get("fov_history", []),
            "abort_reason": state.get("abort_reason"),
            "report_path": _rel_or_none(state.get("report_path"), self.run_dir),
            "started_at": state.get("started_at"),
            "elapsed_s": round(time.monotonic() - self._t0, 1),
            "updated": time.time(),
        }
        tmp = self._state + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, default=_json_default)
            os.replace(tmp, self._state)


# --------------------------------------------------------------------------- #
def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except Exception:
        pass
    return str(o)


def _trim(d, maxlen=200):
    out = {}
    for k, v in (d or {}).items():
        s = v
        if isinstance(v, str) and len(v) > maxlen:
            s = v[:maxlen] + "…"
        out[k] = s
    return out


def _fmt_args(d):
    parts = []
    for k, v in (d or {}).items():
        if isinstance(v, float):
            parts.append(f"{k}={v:g}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def _rel_or_none(path, base):
    if not path:
        return None
    try:
        return os.path.relpath(path, base).replace(os.sep, "/")
    except Exception:
        return path
