"""
Configuration for the Applicant Agent.

Reads app/config/applicant_agent.yaml, cached, with a per-flag environment
override in the shape the rest of the service already uses
(APPLICANT_AGENT_<NAME>). Same mechanism as app/services/verification_config.py
and app/agents/los/config.py -- one convention, not a fourth one.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "applicant_agent.yaml"


def config_path() -> Path:
    return Path(os.getenv("APPLICANT_AGENT_CONFIG_PATH") or _DEFAULT_PATH)


def _load() -> dict[str, Any]:
    global _CACHE

    if _CACHE is not None:
        return _CACHE

    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        path = config_path()
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            logger.error("Applicant Agent config not found at %s", path)
            loaded = {}
        except yaml.YAMLError as exc:
            logger.error("Applicant Agent config is not valid YAML: %s", exc)
            loaded = {}
        _CACHE = loaded
        return _CACHE


def reload() -> None:
    """Drop the cache. For tests and for an explicit reconfiguration."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def _section(name: str) -> dict[str, Any]:
    return _load().get(name, {}) or {}


def _flag(section: str, name: str, default: bool) -> bool:
    """Environment wins over YAML; YAML wins over the built-in default."""
    override = (os.getenv(f"APPLICANT_AGENT_{name.upper()}") or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False
    return bool(_section(section).get(name, default))


# -- agent ------------------------------------------------------------------

def agent_name() -> str:
    return str(_section("agent").get("name") or "Applicant Agent")


def enabled() -> bool:
    return _flag("agent", "enabled", True)


def llm_enabled() -> bool:
    return _flag("agent", "llm_enabled", True)


def llm_for_simple_intents() -> bool:
    return _flag("agent", "llm_for_simple_intents", False)


def temperature() -> float:
    try:
        return float(_section("agent").get("temperature", 0.1))
    except (TypeError, ValueError):
        return 0.1


# -- workflow ---------------------------------------------------------------

def workflow_states() -> list[str]:
    states = _section("workflow").get("states")
    if isinstance(states, list) and states:
        return [str(s) for s in states]
    return [
        "APPLICATION_CREATED", "DOCUMENT_COLLECTION",
        "BASIC_DOCUMENT_VERIFICATION", "READY_FOR_CPA",
    ]


# -- document checklist -----------------------------------------------------

def products() -> list[str]:
    """
    Every product with a declared checklist, including `default`.

    Used to answer taxonomy-wide questions -- what does slot X accept
    anywhere -- without a caller having to guess the product names.
    """
    documents = _section("documents")
    return [str(key).upper() if key != "default" else "default"
            for key in documents]


def checklist_for(product: str | None) -> list[dict[str, Any]]:
    """
    The document checklist for a product, normalised.

    Returns one entry per slot: {"slot", "accepts", "mandatory"}. Required and
    optional slots come back in the same list with a flag, because the
    checklist a FOS is shown includes both -- an optional document that has
    been collected should be visible, and one that has not should not be
    mistaken for a blocker.

    Falls back to `default` when the product is unset or unknown, so a case
    that has not chosen a product still has something to be measured against
    rather than appearing complete by accident.
    """
    documents = _section("documents")
    key = (product or "").strip().upper()
    config = (documents.get(key) if key in documents else None)
    if config is None:
        config = documents.get("default", {}) or {}

    entries: list[dict[str, Any]] = []
    for mandatory, group in ((True, "required"), (False, "optional")):
        for item in (config.get(group) or []):
            # A bare string is accepted as shorthand for a slot that accepts
            # only the type of the same name.
            if isinstance(item, str):
                slot, accepts = item.upper(), [item.upper()]
            else:
                slot = str(item.get("slot") or "").upper()
                accepts = [str(a).upper() for a in (item.get("accepts") or [])]
                if slot and not accepts:
                    accepts = [slot]
            if slot and accepts:
                entries.append(
                    {"slot": slot, "accepts": accepts, "mandatory": mandatory}
                )
    return entries


def document_types() -> list[str]:
    """
    The FOS-stage taxonomy: every type that may be uploaded or selected.

    Naming a type here does NOT make it required anywhere. A product becomes
    subject to a type only by listing a slot that accepts it.
    """
    declared = _load().get("document_types")
    if isinstance(declared, list) and declared:
        return [str(t).upper() for t in declared]
    return ["PAN", "DRIVING_LICENCE", "PASSPORT", "VOTER_ID", "BANK_STATEMENT"]


# -- readiness --------------------------------------------------------------

def readiness_rules() -> dict[str, bool]:
    section = _section("readiness")
    return {
        "require_applicant_fields": bool(section.get("require_applicant_fields", True)),
        "require_application_fields": bool(section.get("require_application_fields", True)),
        "require_all_documents": bool(section.get("require_all_documents", True)),
        "require_documents_verified": bool(section.get("require_documents_verified", True)),
        "block_on_review": bool(section.get("block_on_review", True)),
    }


# -- permissions ------------------------------------------------------------

def permissions_enforced() -> bool:
    return _flag("permissions", "enforce", True)


def read_all_scope() -> str:
    return str(_section("permissions").get("read_all_scope") or "los.read")


def read_scopes() -> dict[str, str]:
    return dict(_section("permissions").get("read", {}) or {})


def write_scopes() -> dict[str, str]:
    return dict(_section("permissions").get("write", {}) or {})


def denied_capabilities() -> list[str]:
    return [str(x) for x in (_section("permissions").get("denied") or [])]


# -- routing ----------------------------------------------------------------

def routing_table() -> dict[str, dict[str, str]]:
    return dict(_section("routing") or {})


# -- timeouts ---------------------------------------------------------------

def _seconds(name: str, default: float) -> float:
    override = os.getenv(f"APPLICANT_AGENT_{name.upper()}")
    raw = override if override is not None else _section("timeouts").get(name, default)
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return default


def tool_timeout_seconds() -> float:
    return _seconds("tool_seconds", 5.0)


def llm_timeout_seconds() -> float:
    return _seconds("llm_seconds", 1.5)


def total_timeout_seconds() -> float:
    return _seconds("total_seconds", 30.0)


def snapshot() -> dict[str, Any]:
    """Every resolved setting. Reported at startup, not guessed at."""
    return {
        "name": agent_name(),
        "enabled": enabled(),
        "llm_enabled": llm_enabled(),
        "llm_for_simple_intents": llm_for_simple_intents(),
        "permissions_enforced": permissions_enforced(),
        "workflow_states": workflow_states(),
        "readiness_rules": readiness_rules(),
        "routes": sorted(routing_table()),
        "tool_timeout_seconds": tool_timeout_seconds(),
        "llm_timeout_seconds": llm_timeout_seconds(),
    }


__all__ = [
    "agent_name", "checklist_for", "denied_capabilities", "enabled",
    "llm_enabled", "llm_for_simple_intents", "llm_timeout_seconds",
    "permissions_enforced", "read_all_scope", "read_scopes",
    "readiness_rules", "reload", "routing_table", "snapshot", "temperature",
    "tool_timeout_seconds", "total_timeout_seconds", "workflow_states",
    "write_scopes",
]


def concise_responses() -> bool:
    """
    Whether an answer carries only the case fields it is about.

    On by default. Off restores the earlier behaviour of returning the whole
    case on every action, which some existing caller may still be relying on.
    """
    return _flag("agent", "concise_responses", True)
