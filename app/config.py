from __future__ import annotations

"""
ManusClaw Configuration System
================================
Config loads in priority order (highest first):
  1. Environment variables
  2. ~/.manusclaw/profiles/<MANUSCLAW_PROFILE>/.env
  3. ~/.manusclaw/profiles/<MANUSCLAW_PROFILE>/config.yaml
  4. ~/.manusclaw/.env
  5. ~/.manusclaw/config.yaml
  6. ./config.toml  (legacy)
  7. Built-in defaults (MockLLM — safe for immediate use)
"""

import os
import threading
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

from pydantic import BaseModel, Field, model_validator
from app.exceptions import ConfigError

_HOME = Path(os.getenv("MANUSCLAW_HOME", str(Path.home() / ".manusclaw")))


class AppEnv(str, Enum):
    DEV  = "dev"
    PROD = "prod"
    TEST = "test"


class LLMConfig(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    provider:       str            = "mock"
    model:          str            = "gpt-4o"
    base_url:       Optional[str]  = None
    api_key:        Optional[str]  = None
    max_tokens:     int            = 4096
    temperature:    float          = 0.0
    max_retries:    int            = 15
    timeout:        int            = 1800   # 30 minutes — safe for deep-reasoning models (DeepSeek R1, o1, etc.)
    extra_headers:  dict[str, str] = Field(default_factory=dict)
    extra_api_keys: list[str]      = Field(default_factory=list)

    @model_validator(mode="after")
    def _coerce_provider(self) -> "LLMConfig":
        safe = {"mock", "ollama", "lmstudio", "openai-compat", "universal", "gguf", "huggingface", "hf", ""}
        if self.provider not in safe and not self.api_key and not self.base_url:
            self.provider = "mock"
        return self


class BrowserConfig(BaseModel):
    headless:           bool = True
    disable_security:   bool = False
    max_content_length: int  = 10_000


class SearchConfig(BaseModel):
    engines:     list[str] = Field(default_factory=lambda: ["duckduckgo", "bing"])
    max_results: int        = 10

    @model_validator(mode="after")
    def _normalize(self) -> "SearchConfig":
        valid = {"duckduckgo", "bing", "google"}
        self.engines = [e.lower().strip() for e in self.engines
                        if e.lower().strip() in valid]
        if not self.engines:
            self.engines = ["duckduckgo", "bing"]
        return self


class SandboxConfig(BaseModel):
    enabled:      bool = False
    docker_image: str  = "python:3.11-slim"
    memory_limit: str  = "256m"
    timeout:      int  = 30


class MCPServerDef(BaseModel):
    name:      str
    transport: str           = "stdio"
    command:   Optional[str] = None
    args:      list[str]     = Field(default_factory=list)
    url:       Optional[str] = None


class RunFlowConfig(BaseModel):
    enable_data_analysis: bool = False
    timeout:              int  = 3600


class LoggingConfig(BaseModel):
    level:          str  = "DEBUG"
    json_format:    bool = False
    include_trace:  bool = True
    redact_secrets: bool = False


class SkinsConfig(BaseModel):
    active:       str = "default"
    border_color: str = "#FFD700"


class AppConfig(BaseModel):
    env:                  AppEnv          = AppEnv.DEV
    llm:                  LLMConfig       = Field(default_factory=LLMConfig)
    browser:              BrowserConfig   = Field(default_factory=BrowserConfig)
    search:               SearchConfig    = Field(default_factory=SearchConfig)
    sandbox:              SandboxConfig   = Field(default_factory=SandboxConfig)
    mcp_servers:          list[MCPServerDef] = Field(default_factory=list)
    runflow:              RunFlowConfig   = Field(default_factory=RunFlowConfig)
    logging:              LoggingConfig   = Field(default_factory=LoggingConfig)
    skins:                SkinsConfig     = Field(default_factory=SkinsConfig)
    workspace_dir:        str             = "workspace"
    max_steps:            int             = 30
    token_budget:         int             = 0
    auto_skill_threshold: int             = 5
    redact_secrets:       bool            = False


class Config:
    """
    Thread-safe singleton config loader with named profile support.
    """

    _instance: Optional["Config"] = None
    _lock: threading.Lock          = threading.Lock()

    def __init__(self, path: str = "config.toml") -> None:
        self._data: AppConfig = self._load(path)

    @classmethod
    def get(cls, path: str = "config.toml") -> "Config":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(path)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Internal loading
    # ------------------------------------------------------------------

    def _load(self, path: str) -> AppConfig:
        self._load_dotenv_chain()
        raw = self._load_config_files(path)

        env_str = os.getenv("APP_ENV", raw.get("env", "dev")).lower()
        try:
            app_env = AppEnv(env_str)
        except ValueError:
            app_env = AppEnv.DEV

        try:
            cfg = AppConfig.model_validate(raw) if raw else AppConfig()
        except Exception as e:
            raise ConfigError(f"Config validation failed: {e}") from e

        cfg.env = app_env

        # Overlay environment variables
        if not cfg.llm.api_key:
            # FIX: Pick provider-specific env var first so that having both
            # OPENAI_API_KEY and ANTHROPIC_API_KEY doesn't incorrectly pick
            # OPENAI_API_KEY when provider="anthropic".
            _provider_key_map = {
                "openai":    os.getenv("OPENAI_API_KEY"),
                "anthropic": os.getenv("ANTHROPIC_API_KEY"),
                "mistral":   os.getenv("MISTRAL_API_KEY"),
                "google":    os.getenv("GOOGLE_API_KEY"),
                "gemini":    os.getenv("GOOGLE_API_KEY"),
            }
            cfg.llm.api_key = (
                _provider_key_map.get(cfg.llm.provider)
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("MISTRAL_API_KEY")
                or os.getenv("LLM_API_KEY")
            )
        if not cfg.llm.base_url:
            cfg.llm.base_url = os.getenv("LLM_BASE_URL")
        if cfg.llm.provider in ("mock", ""):
            detected = self._detect_provider()
            if detected:
                cfg.llm.provider = detected

        # Model override from CLI
        model_override = os.getenv("LLM_MODEL_OVERRIDE", "")
        if model_override:
            cfg.llm.model = model_override

        # Test environment overrides
        if app_env == AppEnv.TEST:
            cfg.llm.provider = "mock"
            cfg.max_steps = 5
            cfg.runflow.timeout = 60

        # Final fallback
        safe_providers = {"mock", "ollama", "lmstudio", "universal", "openai-compat", "gguf", "huggingface", "hf", ""}
        if cfg.llm.provider not in safe_providers and not cfg.llm.api_key and not cfg.llm.base_url:
            import warnings
            warnings.warn(
                f"LLM provider {cfg.llm.provider!r} needs API key. Falling back to MockLLM.",
                stacklevel=3,
            )
            cfg.llm.provider = "mock"

        cfg.redact_secrets = (
            cfg.logging.redact_secrets
            or os.getenv("MANUSCLAW_REDACT", "").lower() in ("1", "true", "yes")
        )
        return cfg

    def _load_dotenv_chain(self) -> None:
        profile = os.getenv("MANUSCLAW_PROFILE", "")
        candidates: list[Path] = []
        if profile:
            candidates.append(_HOME / "profiles" / profile / ".env")
        candidates.append(_HOME / ".env")
        candidates.append(Path(".env"))
        try:
            from dotenv import load_dotenv
            for p in reversed(candidates):
                if p.exists():
                    load_dotenv(p, override=False)
        except ImportError:
            pass

    def _load_config_files(self, legacy_path: str) -> dict:
        profile = os.getenv("MANUSCLAW_PROFILE", "")
        candidates: list[Path] = []
        if profile:
            pd = _HOME / "profiles" / profile
            candidates.append(pd / "config.yaml")
            candidates.append(pd / "config.toml")
        candidates.append(_HOME / "config.yaml")
        candidates.append(_HOME / "config.toml")
        candidates.append(Path(legacy_path))

        for p in candidates:
            if not p.exists():
                continue
            try:
                if p.suffix in (".yaml", ".yml") and _HAS_YAML:
                    with open(p) as f:
                        return _yaml.safe_load(f) or {}
                elif p.suffix == ".toml" and tomllib is not None:
                    with open(p, "rb") as f:
                        return tomllib.load(f)
            except Exception as e:
                raise ConfigError(f"Failed to parse {p}: {e}") from e
        return {}

    @staticmethod
    def _detect_provider() -> Optional[str]:
        if os.getenv("OPENAI_API_KEY"):    return "openai"
        if os.getenv("ANTHROPIC_API_KEY"): return "anthropic"
        if os.getenv("MISTRAL_API_KEY"):   return "mistral"
        if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
            return "bedrock"
        if os.getenv("GOOGLE_API_KEY"):    return "google"
        return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def env(self) -> AppEnv:              return self._data.env
    @property
    def llm(self) -> LLMConfig:           return self._data.llm
    @property
    def browser(self) -> BrowserConfig:   return self._data.browser
    @property
    def search(self) -> SearchConfig:     return self._data.search
    @property
    def sandbox(self) -> SandboxConfig:   return self._data.sandbox
    @property
    def mcp_servers(self) -> list[MCPServerDef]: return self._data.mcp_servers
    @property
    def runflow(self) -> RunFlowConfig:   return self._data.runflow
    @property
    def logging(self) -> LoggingConfig:   return self._data.logging
    @property
    def skins(self) -> SkinsConfig:       return self._data.skins
    @property
    def workspace_dir(self) -> str:       return self._data.workspace_dir
    @property
    def max_steps(self) -> int:           return self._data.max_steps
    @property
    def token_budget(self) -> int:        return self._data.token_budget
    @property
    def auto_skill_threshold(self) -> int: return self._data.auto_skill_threshold
    @property
    def redact_secrets(self) -> bool:     return self._data.redact_secrets

    def is_prod(self) -> bool:  return self._data.env == AppEnv.PROD
    def is_dev(self) -> bool:   return self._data.env == AppEnv.DEV
    def is_test(self) -> bool:  return self._data.env == AppEnv.TEST
