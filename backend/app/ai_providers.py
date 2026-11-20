import json
import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
import httpx

from .config import settings

logger = logging.getLogger("node.ai_providers")


CLOUD_PROVIDER_PRESETS: List[Dict[str, Any]] = [
    {
        "preset_id": "ollama-remote",
        "name": "Remote Ollama Server (LAN / Tailscale)",
        "protocol": "ollama",
        "base_url": "http://100.64.0.10:11434",
        "requires_key": False,
        "suggested_models": [],
        "auto_discover": True,
    },
    {
        "preset_id": "openai",
        "name": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "requires_key": True,
        "suggested_models": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "o4-mini"],
        "auto_discover": True,
    },
    {
        "preset_id": "anthropic",
        "name": "Anthropic Claude",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "requires_key": True,
        "suggested_models": ["claude-sonnet-4-5", "claude-haiku-4-5"],
        "auto_discover": False,
    },
    {
        "preset_id": "gemini",
        "name": "Google Gemini (OpenAI-Compatible)",
        "protocol": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "requires_key": True,
        "suggested_models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "auto_discover": False,
    },
    {
        "preset_id": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "requires_key": True,
        "suggested_models": [
            "google/gemma-3-27b-it:free",
            "qwen/qwen3-32b",
            "anthropic/claude-3.7-sonnet",
            "openai/gpt-4.1-mini",
        ],
        "auto_discover": False,
    },
    {
        "preset_id": "groq",
        "name": "Groq Cloud",
        "protocol": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "requires_key": True,
        "suggested_models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen-qwq-32b"],
        "auto_discover": True,
    },
    {
        "preset_id": "custom-openai",
        "name": "Custom OpenAI-Compatible Endpoint (vLLM / LM Studio / LocalAI)",
        "protocol": "openai",
        "base_url": "http://localhost:8000/v1",
        "requires_key": False,
        "suggested_models": [],
        "auto_discover": True,
    },
]


def _mask_key(raw: Optional[str]) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    if len(s) <= 8:
        return "••••••••"
    return f"{s[:4]}••••{s[-4:]}"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9\-]+", "-", (text or "provider").strip().lower()).strip("-")
    return slug[:64] or "provider"


def _estimate_param_rank(model_name: str) -> float:
    """Heuristic size score so `auto` picks bigger models for primary reasoning & tiny ones for fast triage."""
    low = (model_name or "").lower()
    if any(k in low for k in ("opus", "gpt-4.1", "sonnet", "gemini-2.5-pro", "70b", "72b", "32b")):
        return 50.0
    m = re.search(r"(?:^|[:\-_e])(\d+(?:\.\d+)?)b\b", low)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    if any(k in low for k in ("mini", "flash", "haiku", "instant", "nano")):
        return 3.0
    return 4.0


# Decision models (Liquid d1 family and Ollama's /v1/systemone lane) answer typed
# questions with probabilities and cannot generate text. They must never be picked
# as the chat model, and the decision gate prefers them automatically.
_DECISION_MODEL_PREFIXES = ("d1", "lfm3-d1", "lfm3_d1", "nimble", "tev1")


def is_decision_model(model_name: str) -> bool:
    """True for System One decision models (`d1`, `d1:2b`, `d1-omni`, `hf.co/LiquidAI/d1-2B-GGUF`…)."""
    low = (model_name or "").split("::", 1)[-1].strip().lower()
    if not low:
        return False
    base = low.rsplit("/", 1)[-1].split(":", 1)[0]
    if "decision" in base:
        return True
    return any(base == pfx or base.startswith(pfx + "-") or base.startswith(pfx + ".") or base.startswith(pfx + "_")
               for pfx in _DECISION_MODEL_PREFIXES)


def _clean_role_value(raw: Any) -> str:
    val = str(raw or "").strip()
    return val or "auto"


# Pre-0.13 callers asked for "primary", "fast" or "briefing" models. There is now
# exactly one generative model (the chat model); those names are aliases for it.
CHAT_ROLE_ALIASES = {"chat", "primary", "fast", "briefing"}


class AIProviderRegistry:
    """
    Universal Model & Provider Registry for Node v10.0:
      - Local & Remote Ollama servers (dynamically discovers ALL models via `/api/tags` & `/api/ps`)
      - Incoming Model Heartbeats (`POST /api/v1/models/ping`) from remote Ollama/worker rigs
      - Cloud API keys (OpenAI, Anthropic, Gemini, OpenRouter, Groq, custom OpenAI-compatible)
      - Per-model adaptive capability learning (`supports_thinking`, `supports_tools`)
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or settings.ai_providers_path)
        self._discovery_cache: List[Dict[str, Any]] = []
        self._provider_health: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: float = 0.0
        self._model_caps: Dict[str, Dict[str, bool]] = {}

    def _default_state(self) -> Dict[str, Any]:
        env_chat = settings.effective_chat_model
        env_decision = _clean_role_value(settings.decision_model)
        providers: List[Dict[str, Any]] = [
            {
                "id": "ollama-default",
                "name": "Ollama Server",
                "protocol": "ollama",
                "base_url": (settings.ollama_base_url or "http://localhost:11434").rstrip("/"),
                "api_key": "",
                "models": [],
                "enabled": True,
                "auto_discover": True,
                "source": "default",
                "last_ping_at": None,
            }
        ]
        # Auto-seed cloud providers if standard env vars are present
        env_cloud = [
            ("OPENAI_API_KEY", "openai", "OpenAI", "openai", "https://api.openai.com/v1", ["gpt-4.1-mini", "gpt-4o-mini"]),
            ("ANTHROPIC_API_KEY", "anthropic", "Anthropic Claude", "anthropic", "https://api.anthropic.com/v1", ["claude-sonnet-4-5", "claude-haiku-4-5"]),
            ("GEMINI_API_KEY", "gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", ["gemini-2.5-flash"]),
            ("OPENROUTER_API_KEY", "openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", ["openai/gpt-4.1-mini"]),
            ("GROQ_API_KEY", "groq", "Groq Cloud", "openai", "https://api.groq.com/openai/v1", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]),
        ]
        for env_var, pid, pname, proto, url, mods in env_cloud:
            val = os.environ.get(env_var, "").strip()
            if val:
                providers.append(
                    {
                        "id": pid,
                        "name": pname,
                        "protocol": proto,
                        "base_url": url,
                        "api_key": val,
                        "models": mods,
                        "enabled": True,
                        "auto_discover": proto == "openai" and pid in {"openai", "groq"},
                        "source": "env",
                        "last_ping_at": None,
                    }
                )

        return {
            "providers": providers,
            "routing": {
                "chat_model": env_chat,
                "decision_model": env_decision,
                "enable_web_search": True,
                "auto_agent_logging": True,
                "searxng_url": settings.searxng_url or "",
            },
            "capabilities_cache": {},
        }

    def load_state(self) -> Dict[str, Any]:
        default = self._default_state()
        if not self.path.exists():
            return default
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return default
            providers = raw.get("providers")
            if not isinstance(providers, list) or not providers:
                providers = default["providers"]
            # Keep default ollama base_url synced if unchanged and overridden by env
            for p in providers:
                if p.get("id") == "ollama-default" and p.get("source") == "default":
                    env_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
                    if env_url:
                        p["base_url"] = env_url.rstrip("/")
            routing = self._migrate_routing(default["routing"], raw.get("routing") or {})
            caps = raw.get("capabilities_cache") or {}
            if isinstance(caps, dict):
                self._model_caps.update(caps)
            return {
                "providers": providers,
                "routing": routing,
                "capabilities_cache": self._model_caps,
            }
        except Exception as exc:
            logger.warning("Failed reading %s: %s", self.path, exc)
            return default

    @staticmethod
    def _migrate_routing(default: Dict[str, Any], saved: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge saved routing over defaults, upgrading the pre-0.13 shape
        (`primary_model` / `fast_model` / `briefing_model`) to `chat_model`.
        A model the user picked in the UI still wins over the env default.
        """
        saved = dict(saved or {})
        legacy = [saved.pop(k, None) for k in ("primary_model", "fast_model", "briefing_model")]
        out = {**default, **saved}
        if "chat_model" not in saved:
            picked = next((str(v).strip() for v in legacy if v and str(v).strip().lower() != "auto"), "")
            if picked and not is_decision_model(picked):
                out["chat_model"] = picked
        if is_decision_model(str(out.get("chat_model") or "")):
            out["chat_model"] = "auto"
        out["chat_model"] = _clean_role_value(out.get("chat_model"))
        out["decision_model"] = _clean_role_value(out.get("decision_model"))
        return out

    def save_state(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        self.path.write_text(payload, encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass
        self._cache_ts = 0.0

    def public_state(self) -> Dict[str, Any]:
        state = self.load_state()
        masked_providers = []
        for p in state["providers"]:
            cp = dict(p)
            raw_key = cp.pop("api_key", "") or ""
            cp["has_api_key"] = bool(raw_key)
            cp["api_key_masked"] = _mask_key(raw_key)
            cp["health"] = self._provider_health.get(cp["id"], {"reachable": None})
            cp["reachable"] = bool(cp["health"].get("reachable"))
            mine = [d for d in self._discovery_cache if d.get("provider_id") == cp["id"]]
            # Chat-capable models only; decision models are listed separately so the
            # UI can offer them in the Decision picker and never in the Chat picker.
            cp["discovered_models"] = [d["model"] for d in mine if d.get("kind") != "decision"]
            cp["decision_models"] = [d["model"] for d in mine if d.get("kind") == "decision"]
            cp["configured_models"] = list(cp.get("models") or [])
            masked_providers.append(cp)
        return {
            "providers": masked_providers,
            "routing": state["routing"],
            "presets": CLOUD_PROVIDER_PRESETS,
            "discovered_models": self._discovery_cache,
        }

    def upsert_provider(
        self,
        *,
        provider_id: Optional[str],
        name: str,
        protocol: str,
        base_url: str,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        enabled: bool = True,
        auto_discover: bool = True,
        source: str = "user",
        last_ping_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = self.load_state()
        proto = (protocol or "ollama").strip().lower()
        if proto not in {"ollama", "openai", "anthropic"}:
            proto = "openai"
        clean_url = (base_url or "").strip().rstrip("/")
        pid = _slugify(provider_id or name)
        clean_models = [str(m).strip() for m in (models or []) if str(m).strip()]

        existing = next((p for p in state["providers"] if p.get("id") == pid), None)
        if existing is not None:
            existing["name"] = name.strip()
            existing["protocol"] = proto
            existing["base_url"] = clean_url
            if api_key is not None and api_key.strip() != "" and "••••" not in api_key:
                existing["api_key"] = api_key.strip()
            if models is not None:
                existing["models"] = clean_models
            existing["enabled"] = bool(enabled)
            existing["auto_discover"] = bool(auto_discover)
            if last_ping_at:
                existing["last_ping_at"] = last_ping_at
            target = existing
        else:
            target = {
                "id": pid,
                "name": name.strip(),
                "protocol": proto,
                "base_url": clean_url,
                "api_key": (api_key or "").strip() if api_key and "••••" not in api_key else "",
                "models": clean_models,
                "enabled": bool(enabled),
                "auto_discover": bool(auto_discover),
                "source": source,
                "last_ping_at": last_ping_at,
            }
            state["providers"].append(target)

        self.save_state(state)
        out = dict(target)
        raw_k = out.pop("api_key", "")
        out["has_api_key"] = bool(raw_k)
        out["api_key_masked"] = _mask_key(raw_k)
        return out

    def delete_provider(self, provider_id: str) -> bool:
        state = self.load_state()
        before = len(state["providers"])
        state["providers"] = [p for p in state["providers"] if p.get("id") != provider_id]
        if not state["providers"]:
            state["providers"] = self._default_state()["providers"]
        self.save_state(state)
        return len(state["providers"]) < before

    def update_routing(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        patch = dict(patch or {})
        # Old clients may still send primary_model; treat it as the chat model.
        if patch.get("chat_model") is None and patch.get("primary_model") is not None:
            patch["chat_model"] = patch["primary_model"]
        for k in ("chat_model", "decision_model", "enable_web_search", "auto_agent_logging", "searxng_url"):
            if k in patch and patch[k] is not None:
                state["routing"][k] = patch[k]
        if is_decision_model(str(state["routing"].get("chat_model") or "")):
            raise ValueError("A decision model can't be used as the chat model (it doesn't generate text).")
        state["routing"]["chat_model"] = _clean_role_value(state["routing"].get("chat_model"))
        state["routing"]["decision_model"] = _clean_role_value(state["routing"].get("decision_model"))
        self.save_state(state)
        return state["routing"]

    def decision_model_preference(self) -> str:
        """Routing override (UI) > DECISION_MODEL env > 'auto'."""
        try:
            val = str(self.load_state()["routing"].get("decision_model") or "").strip()
        except Exception:
            val = ""
        if val and val.lower() != "auto":
            return val.split("::", 1)[-1]
        env = (settings.decision_model or "").strip()
        return env if env and env.lower() != "auto" else "auto"

    def discovered_decision_models(self, base_url: Optional[str] = None) -> List[str]:
        """Decision models seen in the last discovery, optionally only on one server."""
        want = (base_url or "").rstrip("/")
        return [
            d["model"]
            for d in self._discovery_cache
            if d.get("kind") == "decision" and (not want or (d.get("base_url") or "").rstrip("/") == want)
        ]

    def default_ollama_base(self) -> str:
        """Base URL of the built-in Ollama provider (UI-editable), else OLLAMA_BASE_URL."""
        try:
            for p in self.load_state()["providers"]:
                if p.get("id") == "ollama-default" and p.get("base_url"):
                    return str(p["base_url"]).rstrip("/")
        except Exception:
            pass
        return (settings.ollama_base_url or "http://localhost:11434").rstrip("/")

    def register_model_ping(
        self,
        *,
        provider_id: Optional[str],
        provider_name: str,
        protocol: str,
        base_url: str,
        models: List[str],
        api_key: Optional[str] = None,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        pid = _slugify(provider_id or provider_name or base_url)
        provider = self.upsert_provider(
            provider_id=pid,
            name=provider_name or pid,
            protocol=protocol or "ollama",
            base_url=base_url,
            api_key=api_key,
            models=models,
            enabled=True,
            auto_discover=True,
            source="ping",
            last_ping_at=now_iso,
        )
        if capabilities and isinstance(capabilities, dict):
            state = self.load_state()
            caps = state.setdefault("capabilities_cache", {})
            for m in models:
                caps[m] = {**caps.get(m, {}), **capabilities}
            self._model_caps.update(caps)
            self.save_state(state)
        # Immediately seed discovery cache so pinged models are selectable even before next HTTP poll
        self._provider_health[pid] = {
            "reachable": True,
            "detail": f"Heartbeat received at {now_iso}",
            "model_count": len(models),
        }
        return provider

    def record_model_capability(self, model_name: str, key: str, supported: bool) -> None:
        bare = model_name.split("::", 1)[-1]
        entry = self._model_caps.setdefault(bare, {})
        if entry.get(key) == supported:
            return
        entry[key] = supported
        try:
            state = self.load_state()
            state.setdefault("capabilities_cache", {})[bare] = entry
            self.save_state(state)
        except Exception:
            pass

    def model_supports_thinking(self, model_name: str) -> bool:
        bare = (model_name or "").split("::", 1)[-1].lower()
        cached = self._model_caps.get((model_name or "").split("::", 1)[-1], {})
        if "supports_thinking" in cached:
            return bool(cached["supports_thinking"])
        # Known non-thinking model families default safely; Ollama retry still catches any edge case
        if any(tok in bare for tok in ("gemma", "llama3", "llama-3", "mistral", "phi", "nomic", "embed")):
            return False
        return True

    async def discover_all_models(self, force: bool = False) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if not force and self._discovery_cache and (now - self._cache_ts) < 20.0:
            return self._discovery_cache

        state = self.load_state()
        discovered: List[Dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=2.8) as client:
            for p in state["providers"]:
                pid = p["id"]
                if not p.get("enabled", True):
                    self._provider_health[pid] = {"reachable": False, "detail": "Disabled"}
                    continue

                proto = p.get("protocol", "ollama")
                base = (p.get("base_url") or "").rstrip("/")
                api_key = p.get("api_key") or ""
                configured_models = [str(m).strip() for m in (p.get("models") or []) if str(m).strip()]
                live_models: List[str] = []
                running_in_ram: set = set()
                reachable = False
                detail = ""

                if proto == "ollama" and base:
                    try:
                        tags_resp = await client.get(f"{base}/api/tags")
                        if tags_resp.status_code == 200:
                            reachable = True
                            data = tags_resp.json()
                            for m in data.get("models") or []:
                                mname = str(m.get("name") or "").strip()
                                if mname and "embed" not in mname.lower():
                                    live_models.append(mname)
                            try:
                                ps_resp = await client.get(f"{base}/api/ps")
                                if ps_resp.status_code == 200:
                                    for rm in ps_resp.json().get("models") or []:
                                        if rm.get("name"):
                                            running_in_ram.add(str(rm["name"]))
                            except Exception:
                                pass
                            detail = f"Online ({len(live_models)} model{'s' if len(live_models) != 1 else ''})"
                        else:
                            detail = f"HTTP {tags_resp.status_code}"
                    except Exception as exc:
                        detail = f"Unreachable ({exc.__class__.__name__})"
                        # If this node pinged recently, preserve its advertised models!
                        if p.get("source") == "ping" and configured_models:
                            reachable = True
                            live_models = list(configured_models)
                            detail = f"Registered via ping ({len(live_models)} models)"

                elif proto == "openai" and base:
                    has_creds = bool(api_key) or base.startswith("http://localhost") or base.startswith("http://127.") or base.startswith("http://100.") or base.startswith("http://192.168.") or base.startswith("http://10.")
                    if has_creds and p.get("auto_discover", False):
                        try:
                            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                            mr = await client.get(f"{base}/models", headers=headers)
                            if mr.status_code == 200:
                                reachable = True
                                raw_list = mr.json().get("data") or []
                                for item in raw_list[:60]:
                                    mid = str(item.get("id") or "").strip()
                                    if mid and not any(skip in mid.lower() for skip in ("embed", "whisper", "tts", "dall-e", "moderation", "audio", "realtime")):
                                        live_models.append(mid)
                                detail = f"Connected ({len(live_models)} models)"
                            else:
                                detail = f"HTTP {mr.status_code}"
                        except Exception as exc:
                            detail = f"Probe error ({exc.__class__.__name__})"
                    if has_creds and configured_models:
                        reachable = True
                        if not detail or "Probe" in detail:
                            detail = f"Configured ({len(configured_models)} models)"
                    elif not has_creds:
                        detail = "API key required"

                elif proto == "anthropic" and base:
                    if api_key:
                        reachable = True
                        live_models = configured_models or ["claude-sonnet-4-5", "claude-haiku-4-5"]
                        detail = f"API key configured ({len(live_models)} models)"
                    else:
                        detail = "API key required"

                merged_names: List[str] = []
                seen = set()
                for mname in live_models + configured_models:
                    if mname and mname not in seen:
                        seen.add(mname)
                        merged_names.append(mname)

                self._provider_health[pid] = {
                    "reachable": reachable,
                    "detail": detail,
                    "model_count": len(merged_names),
                }

                if reachable:
                    for mname in merged_names:
                        discovered.append(
                            {
                                "id": f"{pid}::{mname}",
                                "model": mname,
                                "provider_id": pid,
                                "provider_name": p.get("name", pid),
                                "protocol": proto,
                                "base_url": base,
                                "loaded_in_ram": mname in running_in_ram,
                                "size_rank": _estimate_param_rank(mname),
                                "supports_thinking": self.model_supports_thinking(mname),
                                "kind": "decision" if is_decision_model(mname) else "chat",
                            }
                        )

        self._discovery_cache = discovered
        self._cache_ts = now
        return discovered

    def _get_provider_with_secret(self, provider_id: str) -> Optional[Dict[str, Any]]:
        state = self.load_state()
        return next((p for p in state["providers"] if p.get("id") == provider_id), None)

    async def resolve_target(
        self,
        role: str = "chat",
        explicit_model: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve `{provider, model, composite_id}` for the chat model.

        Node runs two models: a **chat** model (generative: chat, tools, briefings,
        JSON extraction) and a **decision** model (`decision_gate`, System One).
        This resolves the chat model only. `role` accepts the legacy names
        `primary` / `fast` / `briefing` as aliases. Decision models are never
        returned here (they can't generate text).
        """
        if (role or "chat") not in CHAT_ROLE_ALIASES:
            logger.debug("resolve_target: unknown role %r treated as chat", role)
        discovered = [d for d in await self.discover_all_models(force=False) if d.get("kind") != "decision"]
        state = self.load_state()
        routing = state["routing"]

        desired = (explicit_model or "").strip()
        if not desired or desired.lower() == "auto":
            desired = (routing.get("chat_model") or "auto").strip()
        if desired and is_decision_model(desired):
            desired = "auto"

        def _attach_secret(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            prov = self._get_provider_with_secret(entry["provider_id"])
            if not prov:
                return None
            return {
                "composite_id": entry["id"],
                "model": entry["model"],
                "provider_id": entry["provider_id"],
                "provider_name": entry["provider_name"],
                "protocol": prov.get("protocol", "ollama"),
                "base_url": (prov.get("base_url") or "").rstrip("/"),
                "api_key": prov.get("api_key") or "",
                "supports_thinking": entry.get("supports_thinking", True),
            }

        # 1. Exact composite ID (`provider_id::model_name`) or bare model match
        if desired and desired.lower() != "auto":
            if "::" in desired:
                pid, bare_m = desired.split("::", 1)
                exact = next((d for d in discovered if d["id"] == desired), None)
                if exact:
                    return _attach_secret(exact)
                prov = self._get_provider_with_secret(pid)
                if prov and prov.get("enabled", True):
                    return {
                        "composite_id": desired,
                        "model": bare_m,
                        "provider_id": pid,
                        "provider_name": prov.get("name", pid),
                        "protocol": prov.get("protocol", "ollama"),
                        "base_url": (prov.get("base_url") or "").rstrip("/"),
                        "api_key": prov.get("api_key") or "",
                        "supports_thinking": self.model_supports_thinking(bare_m),
                    }
            dl = desired.lower()
            match = next(
                (d for d in discovered if d["model"].lower() == dl or d["model"].lower().startswith(dl)),
                None,
            )
            if match:
                return _attach_secret(match)

        # 2. Dynamic `auto` selection across ALL reporting / discovered models
        if not discovered:
            return None

        # Prefer a chat model already loaded in RAM, then the most capable one.
        ranked = sorted(
            discovered,
            key=lambda d: (
                0 if d.get("loaded_in_ram") else 1,
                -float(d.get("size_rank", 4.0)),
            ),
        )
        return _attach_secret(ranked[0])

    # ------------------------------------------------------------------
    # Unified Multi-Protocol Inference (Ollama + OpenAI + Anthropic)
    # ------------------------------------------------------------------
    async def chat_complete(
        self,
        *,
        messages: List[Dict[str, Any]],
        role: str = "chat",
        explicit_model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        enable_thinking: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 1200,
        timeout: float = 90.0,
    ) -> Dict[str, Any]:
        """
        Non-streaming unified chat call returning:
        `{"ok": bool, "content": str, "thinking": str, "tool_calls": list, "model": str, "provider": str, "error": str}`
        """
        target = await self.resolve_target(role=role, explicit_model=explicit_model)
        if not target:
            return {
                "ok": False,
                "content": "",
                "thinking": "",
                "tool_calls": [],
                "model": "offline",
                "provider": "none",
                "error": "No reachable AI model provider configured or online.",
            }

        proto = target["protocol"]
        if proto == "ollama":
            return await self._ollama_complete(
                target=target,
                messages=messages,
                tools=tools,
                enable_thinking=enable_thinking,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        if proto == "anthropic":
            return await self._anthropic_complete(
                target=target,
                messages=messages,
                tools=tools,
                enable_thinking=enable_thinking,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        return await self._openai_complete(
            target=target,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    async def _ollama_complete(
        self,
        *,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        enable_thinking: bool,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Dict[str, Any]:
        model_name = target["model"]
        base_url = target["base_url"]
        send_think = bool(enable_thinking and self.model_supports_thinking(model_name))

        def _build_payload(include_think: Optional[bool], include_tools: bool) -> Dict[str, Any]:
            body: Dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "num_ctx": settings.ollama_num_ctx,
                    "num_thread": settings.ollama_num_threads,
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            }
            if include_think is not None:
                body["think"] = include_think
            if include_tools and tools:
                body["tools"] = tools
            return body

        async with httpx.AsyncClient(timeout=timeout) as client:
            payload = _build_payload(send_think if send_think else (False if self.model_supports_thinking(model_name) else None), True)
            resp = await client.post(f"{base_url}/api/chat", json=payload)

            # Adaptive retry 1: model does not support `think` parameter (e.g. gemma4:e2b / older runners)
            if resp.status_code == 400 and "think" in payload:
                err_txt = (resp.text or "").lower()
                if "think" in err_txt or "unsupported" in err_txt or "invalid" in err_txt:
                    self.record_model_capability(model_name, "supports_thinking", False)
                    payload = _build_payload(None, True)
                    resp = await client.post(f"{base_url}/api/chat", json=payload)

            # Adaptive retry 2: model does not support native `tools` (fallback without tools)
            if resp.status_code == 400 and tools and "tools" in payload:
                self.record_model_capability(model_name, "supports_tools", False)
                payload = _build_payload(None, False)
                resp = await client.post(f"{base_url}/api/chat", json=payload)

            if resp.status_code != 200:
                return {
                    "ok": False,
                    "content": "",
                    "thinking": "",
                    "tool_calls": [],
                    "model": model_name,
                    "provider": target["provider_name"],
                    "error": f"Ollama HTTP {resp.status_code}: {resp.text[:240]}",
                }

            data = resp.json()
            msg = data.get("message") or {}
            content = str(msg.get("content") or "")
            thinking = str(msg.get("thinking") or "")
            # Also peel `<think>...</think>` blocks if emitted inline by QwQ/DeepSeek/Gemma variants
            if "<think>" in content and "</think>" in content:
                m = re.search(r"<think>([\s\S]*?)</think>", content)
                if m:
                    thinking = (thinking + "\n" + m.group(1)).strip()
                    content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()

            return {
                "ok": True,
                "content": content,
                "thinking": thinking,
                "tool_calls": msg.get("tool_calls") or [],
                "model": model_name,
                "provider": target["provider_name"],
                "error": "",
            }

    async def _openai_complete(
        self,
        *,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Dict[str, Any]:
        model_name = target["model"]
        base_url = target["base_url"]
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if target.get("api_key"):
            headers["Authorization"] = f"Bearer {target['api_key']}"

        # Normalize tool_calls arguments to JSON strings per OpenAI wire spec
        norm_msgs: List[Dict[str, Any]] = []
        for m in messages:
            cm = dict(m)
            if cm.get("role") == "tool" and "tool_call_id" not in cm:
                cm["tool_call_id"] = cm.get("name", "call_node_1")
            if "tool_calls" in cm and isinstance(cm["tool_calls"], list):
                fixed_tcs = []
                for idx, tc in enumerate(cm["tool_calls"]):
                    fn = dict(tc.get("function") or {})
                    if isinstance(fn.get("arguments"), dict):
                        fn["arguments"] = json.dumps(fn["arguments"])
                    fixed_tcs.append(
                        {
                            "id": tc.get("id") or f"call_node_{idx}",
                            "type": "function",
                            "function": fn,
                        }
                    )
                cm["tool_calls"] = fixed_tcs
            norm_msgs.append(cm)

        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": norm_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            if resp.status_code == 400 and tools:
                payload.pop("tools", None)
                resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)

            if resp.status_code != 200:
                return {
                    "ok": False,
                    "content": "",
                    "thinking": "",
                    "tool_calls": [],
                    "model": model_name,
                    "provider": target["provider_name"],
                    "error": f"{target['provider_name']} HTTP {resp.status_code}: {resp.text[:240]}",
                }

            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = str(msg.get("content") or "")
            thinking = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
            raw_tcs = msg.get("tool_calls") or []
            parsed_tcs = []
            for tc in raw_tcs:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    try:
                        args_obj = json.loads(raw_args)
                    except Exception:
                        args_obj = {}
                else:
                    args_obj = raw_args
                parsed_tcs.append(
                    {
                        "id": tc.get("id") or "call_1",
                        "function": {"name": fn.get("name", ""), "arguments": args_obj},
                    }
                )

            return {
                "ok": True,
                "content": content,
                "thinking": thinking,
                "tool_calls": parsed_tcs,
                "model": model_name,
                "provider": target["provider_name"],
                "error": "",
            }

    async def _anthropic_complete(
        self,
        *,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        enable_thinking: bool,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Dict[str, Any]:
        model_name = target["model"]
        base_url = target["base_url"]
        headers = {
            "Content-Type": "application/json",
            "x-api-key": target.get("api_key") or "",
            "anthropic-version": "2023-06-01",
        }
        sys_parts: List[str] = []
        anth_msgs: List[Dict[str, Any]] = []
        for m in messages:
            r = m.get("role", "user")
            if r == "system":
                sys_parts.append(str(m.get("content") or ""))
            elif r == "tool":
                anth_msgs.append(
                    {
                        "role": "user",
                        "content": f"[Tool Result — {m.get('name', 'tool')}]: {m.get('content', '')}",
                    }
                )
            else:
                txt = str(m.get("content") or "").strip() or "..."
                anth_msgs.append({"role": "assistant" if r == "assistant" else "user", "content": txt})

        payload: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": max(max_tokens, 1024),
            "temperature": temperature,
            "messages": anth_msgs or [{"role": "user", "content": "Hello"}],
        }
        if sys_parts:
            payload["system"] = "\n\n".join(sys_parts)
        if tools:
            anth_tools = []
            for t in tools:
                fn = t.get("function") or {}
                anth_tools.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                    }
                )
            payload["tools"] = anth_tools

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base_url}/messages", headers=headers, json=payload)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "content": "",
                    "thinking": "",
                    "tool_calls": [],
                    "model": model_name,
                    "provider": target["provider_name"],
                    "error": f"Anthropic HTTP {resp.status_code}: {resp.text[:240]}",
                }
            data = resp.json()
            text_chunks: List[str] = []
            think_chunks: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for blk in data.get("content") or []:
                btype = blk.get("type")
                if btype == "text":
                    text_chunks.append(str(blk.get("text") or ""))
                elif btype == "thinking":
                    think_chunks.append(str(blk.get("thinking") or ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": blk.get("id") or "toolu_1",
                            "function": {
                                "name": blk.get("name", ""),
                                "arguments": blk.get("input") or {},
                            },
                        }
                    )
            return {
                "ok": True,
                "content": "".join(text_chunks).strip(),
                "thinking": "\n".join(think_chunks).strip(),
                "tool_calls": tool_calls,
                "model": model_name,
                "provider": target["provider_name"],
                "error": "",
            }

    async def stream_turn(
        self,
        *,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        enable_thinking: bool = True,
        temperature: float = 0.65,
        timeout: float = 180.0,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Unified streaming turn across Ollama (`/api/chat` NDJSON) and Cloud APIs (`/chat/completions` SSE & Anthropic).
        Yields:
          - `{"type": "thinking", "delta": "..."}`
          - `{"type": "token", "delta": "..."}`
          - `{"type": "turn_complete", "content": "...", "thinking": "...", "tool_calls": [...]}`
        """
        proto = target.get("protocol", "ollama")
        model_name = target["model"]
        base_url = target["base_url"]

        if proto == "ollama":
            send_think: Optional[bool] = (
                bool(enable_thinking) if self.model_supports_thinking(model_name) else None
            )

            def _make_body(think_val: Optional[bool], use_tools: bool) -> Dict[str, Any]:
                b: Dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "stream": True,
                    "keep_alive": "30m",
                    "options": {
                        "num_ctx": settings.ollama_num_ctx,
                        "num_thread": settings.ollama_num_threads,
                        "temperature": temperature,
                    },
                }
                if think_val is not None:
                    b["think"] = think_val
                if use_tools and tools:
                    b["tools"] = tools
                return b

            body = _make_body(send_think, bool(tools) and self._model_caps.get(model_name, {}).get("supports_tools", True))
            content_parts: List[str] = []
            think_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []

            async with httpx.AsyncClient(timeout=timeout) as client:
                # Try streaming; if 400 due to `think` or `tools`, fall back cleanly without aborting!
                async with client.stream("POST", f"{base_url}/api/chat", json=body) as resp:
                    if resp.status_code == 400:
                        err_body = (await resp.aread()).decode("utf-8", errors="ignore").lower()
                        if "think" in body:
                            self.record_model_capability(model_name, "supports_thinking", False)
                        if "tool" in err_body and "tools" in body:
                            self.record_model_capability(model_name, "supports_tools", False)
                    else:
                        resp.raise_for_status()
                        async for raw_line in resp.aiter_lines():
                            if not raw_line:
                                continue
                            try:
                                chunk = json.loads(raw_line)
                            except Exception:
                                continue
                            msg = chunk.get("message") or {}
                            t_delta = msg.get("thinking") or ""
                            if t_delta:
                                think_parts.append(t_delta)
                                yield {"type": "thinking", "delta": t_delta}
                            c_delta = msg.get("content") or ""
                            if c_delta:
                                content_parts.append(c_delta)
                                yield {"type": "token", "delta": c_delta}
                            tcs = msg.get("tool_calls") or []
                            if tcs:
                                tool_calls.extend(tcs)

                # If first attempt hit 400 capability mismatch, immediately run clean fallback
                if not content_parts and not think_parts and not tool_calls and not self.model_supports_thinking(model_name):
                    fallback = await self._ollama_complete(
                        target=target,
                        messages=messages,
                        tools=tools if self._model_caps.get(model_name, {}).get("supports_tools", True) else None,
                        enable_thinking=False,
                        temperature=temperature,
                        max_tokens=1600,
                        timeout=timeout,
                    )
                    if fallback["ok"]:
                        if fallback["thinking"]:
                            yield {"type": "thinking", "delta": fallback["thinking"]}
                        if fallback["content"]:
                            yield {"type": "token", "delta": fallback["content"]}
                        yield {
                            "type": "turn_complete",
                            "content": fallback["content"],
                            "thinking": fallback["thinking"],
                            "tool_calls": fallback["tool_calls"],
                        }
                        return

            full_content = "".join(content_parts)
            full_think = "".join(think_parts)
            if "<think>" in full_content and "</think>" in full_content:
                m = re.search(r"<think>([\s\S]*?)</think>", full_content)
                if m:
                    full_think = (full_think + "\n" + m.group(1)).strip()
                    full_content = re.sub(r"<think>[\s\S]*?</think>", "", full_content).strip()

            yield {
                "type": "turn_complete",
                "content": full_content,
                "thinking": full_think,
                "tool_calls": tool_calls,
            }
            return

        # Cloud OpenAI / Anthropic: run complete turn and emit smooth progressive chunks
        res = await self.chat_complete(
            messages=messages,
            explicit_model=target["composite_id"],
            tools=tools,
            enable_thinking=enable_thinking,
            temperature=temperature,
            max_tokens=1800,
            timeout=timeout,
        )
        if not res["ok"]:
            raise RuntimeError(res.get("error") or "Cloud provider request failed")
        if res["thinking"]:
            yield {"type": "thinking", "delta": res["thinking"]}
        txt = res["content"] or ""
        step = 48
        for i in range(0, len(txt), step):
            yield {"type": "token", "delta": txt[i : i + step]}
        yield {
            "type": "turn_complete",
            "content": txt,
            "thinking": res["thinking"],
            "tool_calls": res["tool_calls"],
        }


ai_provider_registry = AIProviderRegistry()
