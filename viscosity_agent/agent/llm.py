"""Provider-agnostic LLM construction.

One code path drives either Anthropic (default ``claude-opus-4-8``) or OpenAI
(default ``gpt-5.6-sol``) through LangChain's ``init_chat_model``. The graph
never touches provider SDKs directly, so switching keys is the only change
needed to switch models.

IMPORTANT -- Opus 4.8 rejects ``temperature`` / ``top_p`` / ``top_k`` (400
error). We therefore NEVER pass sampling params; ``max_tokens`` is the only
generation knob. This also keeps behaviour identical across providers.
"""

import os

from langchain.chat_models import init_chat_model

from .config import Settings


def _export_keys(cfg: Settings) -> None:
    """Make sure the chosen provider's key is visible to LangChain via env.

    init_chat_model reads ANTHROPIC_API_KEY / OPENAI_API_KEY from the process
    environment. When the key came from our .env/Settings we mirror it there so
    the underlying SDK finds it.
    """
    if cfg.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = cfg.anthropic_api_key
    if cfg.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = cfg.openai_api_key


def make_llm(cfg: Settings):
    """Return a LangChain chat model for the configured provider.

    No temperature / top_p / top_k -- Opus 4.8 rejects them and we want identical
    behaviour on both providers. ``max_tokens`` is passed so long reports/critiques
    don't truncate.
    """
    provider = cfg.resolve_provider()
    model = cfg.resolve_model()
    _export_keys(cfg)
    return init_chat_model(
        model=model,
        model_provider=provider,
        max_tokens=cfg.max_tokens,
    )


def describe_llm(cfg: Settings) -> str:
    return f"{cfg.resolve_provider()}:{cfg.resolve_model()}"
