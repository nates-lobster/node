from pathlib import Path
from typing import List
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = BASE_ROOT / "data"


class Settings(BaseSettings):
    app_name: str = "Node"
    app_version: str = "0.15.0"
    environment: str = "production"
    local_timezone: str = Field(
        default="America/New_York",
        validation_alias=AliasChoices("LOCAL_TIMEZONE", "TZ"),
    )

    # SQLite persistence
    database_url: str = f"sqlite:///{DEFAULT_DATA_DIR / 'hub.db'}"

    # Server-only OS Lock Screen Authentication (supports both AUTH_* and NODE_AUTH_* env vars)
    auth_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("AUTH_ENABLED", "NODE_AUTH_ENABLED"),
    )
    auth_username: str = Field(
        default="admin",
        validation_alias=AliasChoices("AUTH_USERNAME", "NODE_AUTH_USERNAME"),
    )
    auth_password: str = Field(
        default="changeme",
        validation_alias=AliasChoices("AUTH_PASSWORD", "NODE_AUTH_PASSWORD"),
    )
    auth_password_hash: str = Field(
        default="",
        validation_alias=AliasChoices("AUTH_PASSWORD_HASH", "NODE_AUTH_PASSWORD_HASH"),
    )
    auth_credentials_path: str = str(DEFAULT_DATA_DIR / "auth_credentials.json")
    auth_guard_token_path: str = str(DEFAULT_DATA_DIR / "auth_guard_token.txt")
    # Scoped guard tokens (focus_guard, worker) + legacy flag. Empty = a
    # `guard_tokens.json` next to auth_guard_token_path.
    auth_guard_tokens_path: str = ""
    auth_session_days: int = Field(
        default=7,
        validation_alias=AliasChoices("AUTH_SESSION_DAYS", "NODE_AUTH_SESSION_DAYS"),
    )

    # Persistent AI Secretary Markdown Memory + Shared Multi-Agent Logbook + Notebook + File Drop
    ai_memory_path: str = str(DEFAULT_DATA_DIR / "ai_memory.md")
    ai_notes_path: str = str(DEFAULT_DATA_DIR / "ai_notes.md")
    ai_shared_log_path: str = str(DEFAULT_DATA_DIR / "ai_shared_log.md")
    ai_notebook_dir: str = str(DEFAULT_DATA_DIR / "notebook")
    uploads_dir: str = str(DEFAULT_DATA_DIR / "uploads")
    focus_guard_path: str = str(DEFAULT_DATA_DIR / "focus_guard.json")
    schedule_settings_path: str = str(DEFAULT_DATA_DIR / "schedule_settings.json")
    os_settings_path: str = str(DEFAULT_DATA_DIR / "os_settings.json")
    os_presets_path: str = str(DEFAULT_DATA_DIR / "os_presets.json")
    desktop_widgets_path: str = str(DEFAULT_DATA_DIR / "desktop_widgets.json")
    importance_learner_path: str = str(DEFAULT_DATA_DIR / "importance_learner.json")

    # Multi-Provider AI Registry & Custom Extensions Directory
    ai_providers_path: str = str(DEFAULT_DATA_DIR / "ai_providers.json")
    extensions_dir: str = str(DEFAULT_DATA_DIR / "extensions")

    # Installed-apps registry (written by the App Library / AI) + browser runtime state.
    # Runtime data, so it lives under data/ — never in a tracked file.
    apps_config_path: str = str(DEFAULT_DATA_DIR / "apps_config.yaml")
    # Where pre-0.12 installs kept the registry (tracked scrapers/ file or its
    # old container mount). Read once on boot to migrate, never written.
    legacy_apps_config_paths: List[str] = [
        str(BASE_ROOT / "scrapers" / "apps_config.yaml"),
        "/config/apps_config.yaml",
    ]
    browser_profile_dir: str = str(DEFAULT_DATA_DIR / "browser_profile")
    browser_profiles_root: str = str(DEFAULT_DATA_DIR / "browser_profiles")
    browser_executable_path: str = ""  # Optional custom binary; auto-prefers /usr/bin/brave-browser
    scraper_state_path: str = str(DEFAULT_DATA_DIR / "scraper_state" / "scraper_state.json")

    # Default Local / Remote Ollama Endpoint (additional Ollama & Cloud APIs live in ai_providers.json).
    # One Ollama serves BOTH models Node uses, picked by name per request:
    #   * the chat model     -> /api/chat        (CHAT_MODEL, e.g. an LFM3 chat tag)
    #   * the decision model -> /v1/systemone    (DECISION_MODEL, e.g. `d1`)
    ollama_base_url: str = "http://localhost:11434"
    chat_model: str = Field(default="", validation_alias=AliasChoices("CHAT_MODEL"))
    # Deprecated (pre-0.13 names). Still read so existing .env files keep working;
    # CHAT_MODEL wins when set. See `effective_chat_model`.
    ollama_model: str = ""
    ollama_fast_model: str = ""
    ollama_timeout_seconds: float = 180.0
    ollama_num_ctx: int = 4096
    ollama_num_threads: int = 4
    ollama_enable_thinking: bool = True
    ollama_temperature: float = 0.7

    # Optional Web Search endpoint (e.g., local SearXNG JSON URL; defaults to built-in DuckDuckGo/Wiki search)
    searxng_url: str = ""

    # Decision model: LFM3 d1 (LiquidAI) over the TypeSafe System One wire protocol.
    # Every notification/tag decision and every chat tool-plan goes through it first.
    # Empty DECISION_BASE_URL = use the Ollama server above (Ollama serves d1 at /v1/systemone).
    # Override for llama.cpp (`llama serve`), or https://api.liquid.ai/decisions + DECISION_API_KEY.
    decision_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("DECISION_BASE_URL", "LFM3_BASE_URL", "SYSTEM_ONE_BASE_URL"),
    )
    decision_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DECISION_API_KEY", "LFM3_API_KEY", "LIQUID_API_KEY"),
    )
    decision_model: str = Field(
        default="auto",
        validation_alias=AliasChoices("DECISION_MODEL", "LFM3_MODEL"),
    )
    decision_confidence_threshold: float = Field(
        default=0.55,
        validation_alias=AliasChoices("DECISION_CONFIDENCE_THRESHOLD", "LFM3_CONFIDENCE_THRESHOLD"),
    )
    # Generous enough to survive Ollama cold-loading d1 on the first call.
    decision_timeout_seconds: float = Field(
        default=12.0,
        validation_alias=AliasChoices("DECISION_TIMEOUT_SECONDS", "LFM3_TIMEOUT_SECONDS"),
    )
    decision_max_parallel: int = 4
    decision_enabled: bool = True

    # Chat tool-router cache (LFM3 gate attaches only the relevant tools per turn)
    tool_plan_cache_ttl_seconds: float = 900.0

    # Shared Drive / Inbox body harvesting caps
    inbox_body_max_chars: int = 8000
    inbox_thread_window_hours: int = 72

    # HD Stream defaults (adaptive 1080p w/ zero-idle bandwidth for slow Wi-Fi)
    stream_default_width: int = 1600
    stream_default_height: int = 900
    stream_max_width: int = 1920
    stream_max_height: int = 1080

    # Disabled by default: start clean with zero fake pings or prepopulated schedule
    seed_demo_data: bool = False

    # Static Lock-Screen directory & SDK docs
    dashboard_dir: str = str(BASE_ROOT / "dashboard")
    docs_dir: str = str(BASE_ROOT / "docs")

    # CORS origins
    cors_origins: List[str] = ["*"]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def effective_chat_model(self) -> str:
        """CHAT_MODEL, else the deprecated OLLAMA_MODEL / OLLAMA_FAST_MODEL, else 'auto'."""
        for raw in (self.chat_model, self.ollama_model, self.ollama_fast_model):
            val = (raw or "").strip()
            if val and val.lower() != "auto":
                return val
        return "auto"


settings = Settings()
