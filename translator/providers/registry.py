"""
providers/registry.py — CRUD + persistence + validation orchestration for the
Provider -> Connection -> ModelEntry tree.

Makes no HTTP calls itself: every network operation is delegated to
ProviderClient. Returns result objects, never prints — the TUI renders them
(01_ARCHITECTURE.md §6).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .. import config
from ..vault.vault import Vault, empty_payload
from .client import ListModelsResult, PingResult, ProviderClient, normalize_base_url
from .models import REASONING_CONTROLS, Connection, ModelEntry, Provider, Strategy
from .ratelimit import RateLimitPreset

# Manage-provider batch tests use light concurrency; enough to be quick,
# low enough not to hammer a small local endpoint.
BATCH_TEST_WORKERS = 4


@dataclass
class ValidationResult:
    """Outcome of validating a (base_url, api_key) pair."""
    is_valid: bool
    error: str = ""
    models: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    # True when /models looks unimplemented: the caller should offer manual
    # model entry rather than rejecting the connection (03_PROVIDER_SYSTEM §3).
    needs_manual_model: bool = False


@dataclass
class ModelTestResult:
    model_id: str
    is_ok: bool
    latency_ms: float | None = None
    error: str = ""


class RegistryError(Exception):
    """Invalid CRUD request (duplicate name, unknown id, empty field, ...)."""


class ProviderRegistry:
    """
    In-memory owner of the provider tree, persisted through the Vault.

    Every mutating method persists immediately when the registry is unlocked,
    so a crash mid-session can't silently lose a provider the user just added.
    """

    def __init__(self, vault: Vault | None = None) -> None:
        self._vault = vault or Vault()
        self._password: str | None = None
        self.providers: list[Provider] = []
        self._loaded = False

    # ── Vault lifecycle ───────────────────────────────────────────────────────

    @property
    def is_unlocked(self) -> bool:
        return self._loaded

    @property
    def vault_exists(self) -> bool:
        return self._vault.exists()

    def unlock(self, password: str) -> bool:
        """Load the tree. False = wrong password / corrupt vault."""
        payload = self._vault.load(password)
        if payload is None:
            return False
        self.providers = [Provider.from_dict(p) for p in payload.get("providers", [])]
        self._password = password
        self._loaded = True
        return True

    def lock(self) -> None:
        self.providers = []
        self._password = None
        self._loaded = False

    def destroy_vault(self) -> bool:
        """Wipe the vault file and reset to a first-run state."""
        removed = self._vault.destroy()
        self.lock()
        return removed

    def save(self) -> None:
        if not self._loaded or self._password is None:
            raise RegistryError("registry is locked; unlock before saving")
        payload = empty_payload()
        payload["providers"] = [p.to_dict() for p in self.providers]
        self._vault.save(payload, self._password)

    def change_password(self, old_password: str, new_password: str) -> bool:
        if not self._vault.change_password(old_password, new_password):
            return False
        self._password = new_password
        return True

    # ── Lookup ────────────────────────────────────────────────────────────────

    def get_provider(self, provider_id: str) -> Provider:
        provider = next((p for p in self.providers if p.id == provider_id), None)
        if provider is None:
            raise RegistryError(f"no provider with id {provider_id!r}")
        return provider

    def get_connection(self, provider_id: str, connection_id: str) -> Connection:
        connection = self.get_provider(provider_id).find_connection(connection_id)
        if connection is None:
            raise RegistryError(f"no connection with id {connection_id!r}")
        return connection

    def get_model(self, provider_id: str, connection_id: str, model_id: str) -> ModelEntry:
        model = self.get_connection(provider_id, connection_id).find_model(model_id)
        if model is None:
            raise RegistryError(f"no model {model_id!r} on this connection")
        return model

    def find_provider_by_name(self, name: str) -> Provider | None:
        target = (name or "").strip().lower()
        return next((p for p in self.providers if p.name.strip().lower() == target), None)

    def client_for(self, provider_id: str, connection_id: str) -> ProviderClient:
        provider = self.get_provider(provider_id)
        connection = self.get_connection(provider_id, connection_id)
        return ProviderClient(provider.base_url, connection.api_key)

    # ── Validation (shared by add-provider and add-connection) ────────────────

    @staticmethod
    def validate_connection(
        base_url: str,
        api_key: str | None,
        probe_first_model: bool = True,
    ) -> ValidationResult:
        """
        Structural validation of a connection: can we reach the endpoint and
        does it behave like an OpenAI-compatible API?

        1. Try /models. Success with >=1 model = structurally valid, and we
           optionally ping the first model for a free latency reading.
        2. If /models looks unimplemented (404/405/501/empty), don't hard-fail:
           report needs_manual_model so the caller can ask for a model id and
           validate via ping instead (03_PROVIDER_SYSTEM.md §3).
        """
        base_url = normalize_base_url(base_url)
        if not base_url:
            return ValidationResult(is_valid=False, error="Base URL is required.")

        client = ProviderClient(base_url, api_key)
        listing: ListModelsResult = client.list_models()

        if listing.is_ok and listing.models:
            latency = None
            if probe_first_model:
                ping = client.ping_model(listing.models[0])
                latency = ping.latency_ms if ping.is_ok else None
            return ValidationResult(is_valid=True, models=listing.models, latency_ms=latency)

        if listing.is_missing_endpoint:
            return ValidationResult(
                is_valid=False,
                needs_manual_model=True,
                error=(
                    listing.error
                    or f"{base_url}/models returned no models."
                ),
            )

        return ValidationResult(is_valid=False, error=listing.error)

    @staticmethod
    def validate_with_model(base_url: str, api_key: str | None, model_id: str) -> ValidationResult:
        """Manual-model fallback path: validate by pinging a user-typed model id."""
        base_url = normalize_base_url(base_url)
        if not (model_id or "").strip():
            return ValidationResult(is_valid=False, error="Model ID is required.")

        ping = ProviderClient(base_url, api_key).ping_model(model_id.strip())
        if ping.is_ok:
            return ValidationResult(
                is_valid=True, models=[model_id.strip()], latency_ms=ping.latency_ms
            )
        return ValidationResult(is_valid=False, error=ping.error)

    # ── Provider CRUD ─────────────────────────────────────────────────────────

    def add_provider(
        self,
        name: str,
        base_url: str,
        connection_name: str,
        api_key: str | None,
        validation: ValidationResult,
    ) -> Provider:
        """
        Persist a new Provider with its first Connection.

        The caller must have already run validate_connection/validate_with_model
        and pass the result in: an invalid connection is never saved
        (03_PROVIDER_SYSTEM.md §2 step 5).
        """
        name = (name or "").strip()
        if not name:
            raise RegistryError("Provider name is required.")
        if self.find_provider_by_name(name):
            raise RegistryError(f"A provider named {name!r} already exists.")
        if not validation.is_valid:
            raise RegistryError("Refusing to save an unvalidated connection.")

        provider = Provider(name=name, base_url=normalize_base_url(base_url))
        connection = Connection(
            name=(connection_name or "").strip() or "Default",
            api_key=(api_key or "").strip() or None,
            is_valid=True,
        )
        provider.connections.append(connection)
        self.providers.append(provider)
        self.save()
        return provider

    def rename_provider(self, provider_id: str, new_name: str) -> Provider:
        new_name = (new_name or "").strip()
        if not new_name:
            raise RegistryError("Provider name is required.")
        existing = self.find_provider_by_name(new_name)
        if existing and existing.id != provider_id:
            raise RegistryError(f"A provider named {new_name!r} already exists.")
        provider = self.get_provider(provider_id)
        provider.name = new_name
        self.save()
        return provider

    def set_base_url(self, provider_id: str, base_url: str) -> Provider:
        provider = self.get_provider(provider_id)
        provider.base_url = normalize_base_url(base_url)
        self.save()
        return provider

    def set_strategy(self, provider_id: str, strategy: Strategy) -> Provider:
        if strategy not in ("round_robin", "fallback"):
            raise RegistryError(f"unknown strategy {strategy!r}")
        provider = self.get_provider(provider_id)
        provider.strategy = strategy
        self.save()
        return provider

    def set_rate_limit_scope(self, provider_id: str, scope: str) -> Provider:
        """How per-connection limits combine: 'independent' or 'shared' (UET_UX §3)."""
        if scope not in ("independent", "shared"):
            raise RegistryError(f"unknown rate-limit scope {scope!r}")
        provider = self.get_provider(provider_id)
        provider.rate_limit_scope = scope   # type: ignore[assignment]
        self.save()
        return provider

    def set_connection_rate_limit(
        self, provider_id: str, connection_id: str, preset
    ):
        """Store (or clear, with None) a connection's own rate limit (UET_UX §3)."""
        connection = self.get_connection(provider_id, connection_id)
        connection.rate_limit = preset
        self.save()
        return connection

    def delete_provider(self, provider_id: str) -> None:
        provider = self.get_provider(provider_id)
        self.providers.remove(provider)
        self.save()

    # ── Connection CRUD ───────────────────────────────────────────────────────

    def add_connection(
        self,
        provider_id: str,
        name: str,
        api_key: str | None,
        validation: ValidationResult,
    ) -> Connection:
        provider = self.get_provider(provider_id)
        if not validation.is_valid:
            raise RegistryError("Refusing to save an unvalidated connection.")

        name = (name or "").strip()
        if not name:
            raise RegistryError("Connection name is required.")
        if any(c.name.strip().lower() == name.lower() for c in provider.connections):
            raise RegistryError(f"A connection named {name!r} already exists.")

        inherited_models: list[ModelEntry] = []
        source_conn = next((c for c in provider.connections if c.models), None)
        if source_conn:
            for m in source_conn.models:
                inherited_models.append(ModelEntry.from_dict(m.to_dict()))

        connection = Connection(
            name=name,
            api_key=(api_key or "").strip() or None,
            is_valid=True,
            models=inherited_models,
        )
        provider.connections.append(connection)
        self.save()
        return connection

    def revalidate_connection(self, provider_id: str, connection_id: str) -> ValidationResult:
        provider = self.get_provider(provider_id)
        connection = self.get_connection(provider_id, connection_id)
        result = self.validate_connection(
            provider.base_url, connection.api_key, probe_first_model=False
        )
        connection.is_valid = result.is_valid
        connection.validation_error = result.error
        self.save()
        return result

    def delete_connection(self, provider_id: str, connection_id: str) -> None:
        provider = self.get_provider(provider_id)
        connection = self.get_connection(provider_id, connection_id)
        provider.connections.remove(connection)
        self.save()

    # ── Model management ──────────────────────────────────────────────────────

    def fetch_models(self, provider_id: str, connection_id: str) -> ListModelsResult:
        """Live /models listing. The caller picks which ones to actually add."""
        return self.client_for(provider_id, connection_id).list_models()

    def add_models(
        self,
        provider_id: str,
        connection_id: str,
        model_ids: list[str],
        preset: RateLimitPreset | None = None,
    ) -> list[ModelEntry]:
        """Add selected/manual model ids. Existing ids are skipped, not duplicated."""
        connection = self.get_connection(provider_id, connection_id)
        added: list[ModelEntry] = []
        for raw_id in model_ids:
            model_id = (raw_id or "").strip()
            if not model_id or connection.find_model(model_id):
                continue
            entry = ModelEntry(
                id=model_id,
                preset=preset or RateLimitPreset.default(),
            )
            connection.models.append(entry)
            added.append(entry)
        if added:
            self.save()
        return added

    def remove_model(self, provider_id: str, connection_id: str, model_id: str) -> None:
        connection = self.get_connection(provider_id, connection_id)
        model = connection.find_model(model_id)
        if model is None:
            raise RegistryError(f"no model {model_id!r} on this connection")
        connection.models.remove(model)
        self.save()

    def set_model_preset(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        preset: RateLimitPreset,
    ) -> ModelEntry:
        model = self.get_model(provider_id, connection_id, model_id)
        model.preset = preset
        self.save()
        return model

    def set_connection_preset(
        self,
        provider_id: str,
        connection_id: str,
        preset: RateLimitPreset,
    ) -> int:
        """Quick-mode 'apply to every model on this connection'. Returns count."""
        connection = self.get_connection(provider_id, connection_id)
        for model in connection.models:
            model.preset = RateLimitPreset.from_dict(preset.to_dict())
        self.save()
        return len(connection.models)

    def set_model_reasoning(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        supports_reasoning: bool,
        options: list[str] | None = None,
    ) -> ModelEntry:
        """
        The short form, kept for the callers that only know "does it reason?".

        `True` maps to control `effort` — the same behaviour the old flag drove
        (a `reasoning_effort` level). Anything richer goes through
        `set_model_reasoning_config`.
        """
        return self.set_model_reasoning_config(
            provider_id,
            connection_id,
            model_id,
            supported=supports_reasoning,
            control="effort" if supports_reasoning else "none",
            options=options,
        )

    def set_model_reasoning_config(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        supported: bool,
        control: str = "none",
        parameter_path: str = "",
        on_value=True,
        off_value=False,
        default_enabled: bool = False,
        options: list[str] | None = None,
    ) -> ModelEntry:
        """
        Store a model's full reasoning metadata (FEATURE-004 / ADR-003).

        Validation refuses only what the adapter could not honour, so nothing
        is silently half-saved: a `toggle` without a parameter path would send
        no parameter at all, which is indistinguishable from "off" and would be
        a lie on the screen.
        """
        control = (control or "none").strip().lower()
        if control not in REASONING_CONTROLS:
            raise RegistryError(f"unknown reasoning control {control!r}")
        if not supported:
            control = "none"
        if control == "toggle" and not str(parameter_path or "").strip():
            raise RegistryError(
                "A toggle model needs a parameter path "
                "(e.g. chat_template_kwargs.enable_thinking)."
            )

        model = self.get_model(provider_id, connection_id, model_id)
        model.reasoning_supported = bool(supported) and control != "none"
        model.reasoning_control = control            # type: ignore[assignment]
        model.reasoning_parameter_path = str(parameter_path or "").strip()
        model.reasoning_on_value = on_value
        model.reasoning_off_value = off_value
        model.reasoning_default = bool(default_enabled) and control in ("toggle", "effort")
        if control == "effort":
            model.reasoning_options = [
                str(value) for value in (options or ["low", "medium", "high"])
            ]
        else:
            model.reasoning_options = [str(value) for value in (options or [])]
        self.save()
        return model

    def set_model_prompt_format(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        prompt_format: str,
    ) -> ModelEntry:
        """
        Structured-output opt-in. Off by default: many OpenAI-compatible
        servers reject response_format (02_DATA_MODELS.md §7 item 3).
        """
        if prompt_format not in ("plain", "json"):
            raise RegistryError(f"unknown prompt format {prompt_format!r}")
        model = self.get_model(provider_id, connection_id, model_id)
        model.prompt_format = prompt_format  # type: ignore[assignment]
        self.save()
        return model

    def set_model_priority_rank(
        self,
        provider_id: str,
        connection_id: str,
        model_id: str,
        priority_rank: int,
    ) -> ModelEntry:
        """
        Set the priority failover rank (0 = None/normal, 1, 2, 3... = failover order).
        Ensures uniqueness across all active models when rank > 0.
        """
        rank = max(0, int(priority_rank))
        if rank > 0:
            for p in self.providers:
                for c in p.connections:
                    for m in c.models:
                        if m.id != model_id and m.priority_rank == rank:
                            raise RegistryError(
                                f"Priority Rank #{rank} is already assigned to {m.label}"
                            )
        model = self.get_model(provider_id, connection_id, model_id)
        model.priority_rank = rank
        self.save()
        return model

    def get_priority_models(self) -> list[tuple[Provider, Connection, ModelEntry]]:
        """
        Return all models configured with a priority rank > 0 across all providers,
        sorted in ascending order of priority rank (Priority #1, #2, #3...).
        """
        ranked: list[tuple[Provider, Connection, ModelEntry]] = []
        for p in self.providers:
            for c in p.connections:
                for m in c.models:
                    if getattr(m, "priority_rank", 0) > 0:
                        ranked.append((p, c, m))
        ranked.sort(key=lambda item: item[2].priority_rank)
        return ranked

    def test_models(
        self,
        provider_id: str,
        connection_id: str,
        model_ids: list[str],
        timeout: float = config.PING_TIMEOUT,
        on_result=None,
    ) -> list[ModelTestResult]:
        """
        Batch test: ping each model, record latency/status/raw error on the
        ModelEntry, and persist so Manage Provider can show last-known status
        without re-testing.

        `on_result` is an optional callback invoked per finished model so the
        TUI can update live; it receives a ModelTestResult.
        """
        connection = self.get_connection(provider_id, connection_id)
        client = self.client_for(provider_id, connection_id)
        targets = [m for m in (mid.strip() for mid in model_ids) if m]
        if not targets:
            return []

        def _run(model_id: str) -> tuple[str, PingResult]:
            return model_id, client.ping_model(model_id, timeout=timeout)

        results: list[ModelTestResult] = []
        workers = max(1, min(BATCH_TEST_WORKERS, len(targets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for model_id, ping in pool.map(_run, targets):
                entry = connection.find_model(model_id)
                if entry is not None:
                    entry.last_test_status = "ok" if ping.is_ok else "error"
                    entry.last_test_latency_ms = ping.latency_ms if ping.is_ok else None
                    entry.last_test_error = "" if ping.is_ok else ping.error
                result = ModelTestResult(
                    model_id=model_id,
                    is_ok=ping.is_ok,
                    latency_ms=ping.latency_ms if ping.is_ok else None,
                    error=ping.error,
                )
                results.append(result)
                if on_result is not None:
                    on_result(result)

        self.save()
        return results
