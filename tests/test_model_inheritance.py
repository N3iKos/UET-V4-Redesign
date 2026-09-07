"""
tests/test_model_inheritance.py — Test that new connections inherit models from existing connections in the same provider.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator.providers.models import ModelEntry, RateLimitPreset
from translator.providers.registry import ProviderRegistry, ValidationResult
from translator.vault.vault import Vault


def test_inherit_models_on_new_connection(tmp_path):
    vault = Vault(path=str(tmp_path / "vault.enc"))
    reg = ProviderRegistry(vault=vault)
    reg.unlock("testpass123")

    val = ValidationResult(is_valid=True)
    provider = reg.add_provider(
        name="Ollama Cloud",
        base_url="https://api.ollama.com",
        connection_name="API Key 1",
        api_key="key1",
        validation=val,
    )
    conn1 = provider.connections[0]
    reg.add_models(provider.id, conn1.id, ["gemma4:31b-cloud", "deepseek-v3"])

    # Verify conn1 has 2 models
    assert len(conn1.models) == 2

    # Now add API Key 2 to the same provider
    conn2 = reg.add_connection(
        provider_id=provider.id,
        name="API Key 2",
        api_key="key2",
        validation=val,
    )

    # conn2 must have inherited both models automatically!
    assert len(conn2.models) == 2
    conn2_model_ids = [m.id for m in conn2.models]
    assert conn2_model_ids == ["gemma4:31b-cloud", "deepseek-v3"]


def test_new_provider_does_not_inherit(tmp_path):
    vault = Vault(path=str(tmp_path / "vault.enc"))
    reg = ProviderRegistry(vault=vault)
    reg.unlock("testpass123")

    val = ValidationResult(is_valid=True)
    p1 = reg.add_provider(
        name="Provider 1",
        base_url="https://p1.com",
        connection_name="Conn 1",
        api_key="k1",
        validation=val,
    )
    reg.add_models(p1.id, p1.connections[0].id, ["model-1"])

    # Adding a completely new provider must NOT inherit models from p1
    p2 = reg.add_provider(
        name="Provider 2",
        base_url="https://p2.com",
        connection_name="Conn 2",
        api_key="k2",
        validation=val,
    )
    assert len(p2.connections[0].models) == 0
