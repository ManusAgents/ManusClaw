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


class LLMStreamingConfig(BaseModel):
    enabled:       bool = True
    buffer_size:   int  = 4096
    chunk_timeout: int  = 30


class LLMFallbackConfig(BaseModel):
    enabled:             bool          = False
    chain:               list[str]     = Field(default_factory=lambda: ["gpt-4o", "claude-3-5-sonnet"])
    cooldown_s:          float         = 60.0
    cooldown_multiplier: float         = 2.0
    max_cooldown_s:      float         = 600.0
    triggers:            list[str]     = Field(default_factory=lambda: ["rate_limit", "service_unavailable", "context_window", "quota"])


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
    streaming:      LLMStreamingConfig = Field(default_factory=LLMStreamingConfig)
    fallback:       LLMFallbackConfig  = Field(default_factory=LLMFallbackConfig)

    @model_validator(mode="after")
    def _coerce_provider(self) -> "LLMConfig":
        safe = {"mock", "ollama", "lmstudio", "openai-compat", "universal", "gguf", "huggingface", "hf", ""}
        # FIX: Only silently coerce to mock if provider is NOT a known real provider.
        # Previously, valid providers like "openai", "anthropic" were silently
        # changed to "mock" when no API key was present in the config file,
        # even though environment variables might provide keys later.
        # Now we only coerce truly unknown/empty providers.
        known_providers = {"openai", "anthropic", "google", "gemini", "mistral", "bedrock"}
        if self.provider not in safe and self.provider not in known_providers and not self.api_key and not self.base_url:
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


class SecurityConfig(BaseModel):
    enabled:                bool       = True
    analyzers:              list[str]  = Field(default_factory=lambda: ["pattern", "rails"])
    confirmation_threshold: str        = "medium"
    cipher_key:             Optional[str] = None


class HooksConfig(BaseModel):
    enabled:   bool       = True
    auto_load: bool       = True
    hook_dirs: list[str]  = Field(default_factory=lambda: ["~/.manusclaw/hooks"])
    timeout_s: int        = 30


class ContextConfig(BaseModel):
    max_events:     int  = 200
    max_tokens:     int  = 80000
    condenser_type: str  = "rolling"


class ConversationConfig(BaseModel):
    max_iterations:    int  = 30
    confirmation_mode: str  = "confirm_risky"
    stuck_detection:   bool = True
    stuck_threshold:   int  = 3


class ObservabilityConfig(BaseModel):
    tracing_enabled:  bool  = False
    tracing_endpoint: str   = "http://localhost:4317"
    tracing_service:  str   = "manusclaw"
    metrics_enabled:  bool  = False
    metrics_port:     int   = 9090
    health_enabled:   bool  = True
    health_path:      str   = "/health"


class SecretsConfig(BaseModel):
    backend:           Optional[str] = "file"
    file_path:         Optional[str] = None
    encryption_enabled: bool         = True


class FileStoreConfig(BaseModel):
    backend:    str           = "local"
    base_dir:   Optional[str] = None
    s3_bucket:  Optional[str] = None
    s3_region:  str           = "us-east-1"
    s3_endpoint: Optional[str] = None
    gcs_bucket: Optional[str] = None


class GitProvidersConfig(BaseModel):
    default_provider:      str           = "github"
    github_token:          Optional[str] = None
    gitlab_token:          Optional[str] = None
    gitlab_url:            str           = "https://gitlab.com"
    azure_devops_token:    Optional[str] = None
    azure_devops_org:      Optional[str] = None
    bitbucket_username:    Optional[str] = None
    bitbucket_app_password: Optional[str] = None
    forgejo_url:           Optional[str] = None
    forgejo_token:         Optional[str] = None


class IntegrationsConfig(BaseModel):
    jinja_templates_dir: str           = "~/.manusclaw/templates"
    webhooks_enabled:    bool          = False
    webhook_secret:      Optional[str] = None


class ParallelExecutorConfig(BaseModel):
    max_workers: int   = 4
    timeout_s:   int   = 300


class MigrationsConfig(BaseModel):
    enabled:     bool          = True
    auto_run:    bool          = False
    database_url: Optional[str] = None


class AppConfig(BaseModel):
    env:                  AppEnv          = AppEnv.DEV
    llm:                  LLMConfig               = Field(default_factory=LLMConfig)
    browser:              BrowserConfig            = Field(default_factory=BrowserConfig)
    search:               SearchConfig             = Field(default_factory=SearchConfig)
    sandbox:              SandboxConfig            = Field(default_factory=SandboxConfig)
    mcp_servers:          list[MCPServerDef]        = Field(default_factory=list)
    runflow:              RunFlowConfig            = Field(default_factory=RunFlowConfig)
    logging:              LoggingConfig            = Field(default_factory=LoggingConfig)
    skins:                SkinsConfig              = Field(default_factory=SkinsConfig)
    security:             SecurityConfig           = Field(default_factory=SecurityConfig)
    hooks:                HooksConfig              = Field(default_factory=HooksConfig)
    context:              ContextConfig            = Field(default_factory=ContextConfig)
    conversation:         ConversationConfig       = Field(default_factory=ConversationConfig)
    observability:        ObservabilityConfig      = Field(default_factory=ObservabilityConfig)
    secrets:              SecretsConfig            = Field(default_factory=SecretsConfig)
    file_store:           FileStoreConfig          = Field(default_factory=FileStoreConfig)
    git_providers:        GitProvidersConfig       = Field(default_factory=GitProvidersConfig)
    integrations:         IntegrationsConfig       = Field(default_factory=IntegrationsConfig)
    parallel_executor:    ParallelExecutorConfig   = Field(default_factory=ParallelExecutorConfig)
    migrations:           MigrationsConfig         = Field(default_factory=MigrationsConfig)
    workspace_dir:        str                      = "workspace"
    max_steps:            int                      = 30
    token_budget:         int                      = 0
    auto_skill_threshold: int                      = 5
    redact_secrets:       bool                     = False

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def get(cls) -> "AppConfig":
        """Convenience: load config via the Config singleton."""
        return Config.get()._data


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
        # FIX: Thread-safety gap — the original checked _instance outside the lock
        # which could lead to a race condition where two threads both see None.
        # Double-checked locking pattern fixes this.
        if cls._instance is not None:
            return cls._instance
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

        # ── Overlay env vars for new modules ──────────────────────────────
        if not cfg.security.cipher_key:
            cfg.security.cipher_key = os.getenv("MANUSCLAW_CIPHER_KEY")
        if not cfg.git_providers.github_token:
            cfg.git_providers.github_token = os.getenv("GITHUB_TOKEN")
        if not cfg.git_providers.gitlab_token:
            cfg.git_providers.gitlab_token = os.getenv("GITLAB_TOKEN")
        if not cfg.git_providers.azure_devops_token:
            cfg.git_providers.azure_devops_token = os.getenv("AZURE_DEVOPS_TOKEN")
        if not cfg.git_providers.forgejo_token:
            cfg.git_providers.forgejo_token = os.getenv("FORGEJO_TOKEN")
        if not cfg.migrations.database_url:
            cfg.migrations.database_url = os.getenv("DATABASE_URL")
        if not cfg.file_store.s3_bucket:
            cfg.file_store.s3_bucket = os.getenv("S3_BUCKET")
        if not cfg.file_store.gcs_bucket:
            cfg.file_store.gcs_bucket = os.getenv("GCS_BUCKET")

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
    def skins(self) -> SkinsConfig:              return self._data.skins
    @property
    def security(self) -> SecurityConfig:         return self._data.security
    @property
    def hooks(self) -> HooksConfig:                return self._data.hooks
    @property
    def context(self) -> ContextConfig:            return self._data.context
    @property
    def conversation(self) -> ConversationConfig:  return self._data.conversation
    @property
    def observability(self) -> ObservabilityConfig: return self._data.observability
    @property
    def secrets(self) -> SecretsConfig:            return self._data.secrets
    @property
    def file_store(self) -> FileStoreConfig:       return self._data.file_store
    @property
    def git_providers(self) -> GitProvidersConfig: return self._data.git_providers
    @property
    def integrations(self) -> IntegrationsConfig:  return self._data.integrations
    @property
    def parallel_executor(self) -> ParallelExecutorConfig: return self._data.parallel_executor
    @property
    def migrations(self) -> MigrationsConfig:      return self._data.migrations
    @property
    def workspace_dir(self) -> str:                return self._data.workspace_dir
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
