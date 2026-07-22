"""Typed configuration for the agent.

Precedence (highest first): explicit init kwargs (CLI flags) > environment /
.env > config.yaml > field defaults. Connection + LLM secrets come only from
the environment; experiment/analysis/budget knobs come from config.yaml (or an
env override of the same name in upper case).
"""

import os
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_CONFIG_YAML = os.path.join(_APP_ROOT, "config.yaml")
_DOTENV = os.path.join(_APP_ROOT, ".env")


class _YamlSource(PydanticBaseSettingsSource):
    """A settings source that reads defaults from config.yaml (below env)."""

    def __init__(self, settings_cls, path):
        super().__init__(settings_cls)
        self._data = {}
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}

    def get_field_value(self, field, field_name):  # required abstract hook
        return self._data.get(field_name), field_name, False

    def __call__(self):
        return {name: self._data[name]
                for name in self.settings_cls.model_fields
                if name in self._data}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_DOTENV, env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
        protected_namespaces=(),      # we use *_model field names freely
    )

    # ---- microscope connection (env only) ----
    scopio_url: str = "http://127.0.0.1:8000"
    scopio_api_key: str = ""

    # ---- LLM (env only) ----
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_provider: Optional[str] = None        # None -> auto-detect from keys
    anthropic_model: str = "claude-opus-4-8"  # current Anthropic flagship
    openai_model: str = "gpt-5.6-sol"         # current OpenAI flagship
    max_tokens: int = 8192
    # optional cost overrides ($/1M tokens) if the built-in price table is stale
    price_in_per_mtok: Optional[float] = None
    price_out_per_mtok: Optional[float] = None

    # ---- the one human input ----
    um_per_px: Optional[float] = None

    # ---- experiment / physics ----
    temperature_K: float = 298.15
    bead_radius_m: float = 0.5e-6
    bead_radius_unc_m: float = 0.0
    literature_eta_Pa_s: float = 8.9e-4

    # ---- analysis ----
    min_coverage: float = 0.90
    fit_fraction: float = 0.25
    max_ecc: float = 0.30
    drift_correction: bool = True     # subtract linear stage drift before MSD
    min_beads: int = 5
    target_beads: int = 12

    # ---- scene / survey ----
    min_scene_beads: int = 6
    max_clump_fraction: float = 0.30
    focus_floor: float = 40.0

    # ---- acquisition ----
    target_fps: int = 30
    clip_min_s: int = 10
    clip_max_s: int = 60
    default_clip_s: int = 20
    track_workers: str = "auto"       # detect() CPU processes: "auto" | "1" | int

    # ---- budgets ----
    max_iterations: int = 5
    max_fov_moves: int = 6
    wall_clock_budget_s: int = 1800
    recursion_limit: int = 80
    max_jog_steps: int = 2000
    sandbox_timeout_s: int = 120

    # ---- misc ----
    dry_run: bool = False
    dashboard: bool = True
    dashboard_port: int = 8070

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings,
                                   env_settings, dotenv_settings,
                                   file_secret_settings):
        # init (CLI) > env > dotenv > yaml > defaults
        return (init_settings, env_settings, dotenv_settings,
                _YamlSource(settings_cls, _CONFIG_YAML), file_secret_settings)

    # ------------------------------------------------------------ helpers
    def resolve_provider(self) -> str:
        """Which LLM provider to use: explicit override, else auto-detect."""
        if self.llm_provider:
            p = self.llm_provider.strip().lower()
            if p not in ("anthropic", "openai"):
                raise ValueError(
                    f"LLM_PROVIDER must be 'anthropic' or 'openai', got {p!r}")
            return p
        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        raise ValueError(
            "No LLM key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY "
            "(the agent still needs an LLM even in --dry-run).")

    def resolve_model(self) -> str:
        return (self.anthropic_model if self.resolve_provider() == "anthropic"
                else self.openai_model)

    @property
    def clip_bounds_s(self):
        return (self.clip_min_s, self.clip_max_s)


def load_settings(**overrides) -> Settings:
    """Build Settings, applying non-None CLI overrides at the highest priority."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**clean)
