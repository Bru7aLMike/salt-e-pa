#!/usr/bin/env python3
"""Config and opt-in gate for the optional Jira module (Salt-e PA scaffold).

This module is DISABLED by default. Nothing here reads a credential or touches
the network at import time. The gate `is_enabled()` returns False unless the
user explicitly opts in, so a clone that never opts in has zero Jira code paths
executing and needs no credentials.

Resolution precedence for every NON-secret setting (base URL, project keys,
data dir, timezone) mirrors the rest of the scaffold:
  1. environment variable (the PA_* name noted per setting)
  2. the value in memory/workstream_config.yml (a `jira:` block, plus the
     shared paths/locale blocks)
  3. a repo-relative or neutral default

Credentials are the exception: the account email and API token are read from
environment variables ONLY. They have no config key and no default value, so a
secret never lands in a committed file.
"""
import os
import re
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

# ----- Config-file location ------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent          # modules/jira
REPO_ROOT = SCRIPT_DIR.parents[1]                      # repo root

_cfg_env = os.environ.get("PA_CONFIG_FILE")
CONFIG_FILE = (
    Path(_cfg_env).expanduser() if _cfg_env and _cfg_env.strip()
    else REPO_ROOT / "memory" / "workstream_config.yml"
)

_PLACEHOLDER_RE = re.compile(r"^\s*(?:\{\{[A-Z0-9_]+\}\}|<[A-Z0-9_]+>)\s*$")

_TRUE_TOKENS = {"1", "true", "yes", "on", "enabled"}


def _is_placeholder(value: Any) -> bool:
    """True if a config value is unset or a shipped angle/brace placeholder."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return not value.strip() or bool(_PLACEHOLDER_RE.match(value.strip()))


def _truthy(value: Any) -> bool:
    """Interpret a string/bool as an opt-in flag. Unknown values read as False."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_TOKENS


def _clean_list(value: Any) -> list[str]:
    """Normalize a config list, dropping placeholders and the literal 'none'."""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            s = str(item).strip()
            if s and s.lower() != "none" and not _is_placeholder(s):
                out.append(s)
        return out
    s = str(value).strip()
    if not s or s.lower() == "none" or _is_placeholder(s):
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _load_config_raw(path: Path) -> dict:
    """Best-effort YAML load of the config file. Empty dict on any error."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


_CONFIG_CACHE: dict | None = None


def _config() -> dict:
    """Lazily load and cache the config file. No credentials are read here."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = _load_config_raw(CONFIG_FILE)
    return _CONFIG_CACHE


def _jira_block() -> dict:
    block = _config().get("jira")
    return block if isinstance(block, dict) else {}


# ----- Opt-in gate ---------------------------------------------------------


def is_enabled() -> bool:
    """The single gate for every Jira code path.

    Off by default. On only when PA_JIRA_ENABLED is truthy, or the config
    `jira.enabled` key is set to a truthy value. A clone that touches neither
    gets False and never reaches a credential read or a network call.
    """
    env = os.environ.get("PA_JIRA_ENABLED")
    if env is not None and env.strip() != "":
        return _truthy(env)
    val = _jira_block().get("enabled")
    if isinstance(val, bool):
        return val
    if isinstance(val, str) and not _is_placeholder(val):
        return _truthy(val)
    return False


# ----- Non-secret settings (env -> config -> default) ----------------------


def get_base_url() -> str | None:
    """Jira base URL, e.g. https://your-domain.atlassian.net. None if unset."""
    raw = os.environ.get("PA_JIRA_BASE_URL")
    if not (raw and raw.strip()):
        cv = _jira_block().get("base_url")
        raw = cv if isinstance(cv, str) and not _is_placeholder(cv) else None
    if not (raw and raw.strip()):
        return None
    raw = raw.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return "https://" + raw.rstrip("/")


def get_projects() -> dict[str, str]:
    """Return {project_key: display_name}. Empty dict when nothing configured.

    Sources, in order: PA_JIRA_PROJECT_KEYS (comma-separated keys), a
    `jira.projects` mapping, a `jira.project_keys` list, then the shared
    top-level `jira_project_keys` list. Display name defaults to the key.
    """
    env = os.environ.get("PA_JIRA_PROJECT_KEYS")
    keys = _clean_list(env) if env else []
    if keys:
        return {k: k for k in keys}

    jira = _jira_block()
    mapping = jira.get("projects")
    if isinstance(mapping, dict):
        out: dict[str, str] = {}
        for k, v in mapping.items():
            ks = str(k).strip()
            if not ks or _is_placeholder(ks):
                continue
            name = str(v).strip() if v and not _is_placeholder(str(v)) else ks
            out[ks] = name
        if out:
            return out

    keys = _clean_list(jira.get("project_keys"))
    if keys:
        return {k: k for k in keys}

    keys = _clean_list(_config().get("jira_project_keys"))
    return {k: k for k in keys}


def get_data_dir() -> Path:
    """Directory for the module's own cache/log outputs."""
    env = os.environ.get("PA_JIRA_DATA_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    paths = _config().get("paths")
    if isinstance(paths, dict):
        wd = paths.get("working_dir")
        if isinstance(wd, str) and not _is_placeholder(wd):
            return Path(wd).expanduser() / "data"
    return REPO_ROOT / "data"


def _tz_offset_hours() -> float:
    env = os.environ.get("PA_TZ_OFFSET_HOURS")
    if env and env.strip():
        try:
            return float(env.strip())
        except ValueError:
            pass
    locale = _config().get("locale")
    if isinstance(locale, dict):
        cv = locale.get("utc_offset_hours")
        if cv is not None and not _is_placeholder(cv):
            try:
                return float(cv)
            except (TypeError, ValueError):
                pass
    return 0.0


def _tz_name() -> str:
    env = os.environ.get("PA_TZ_NAME")
    if env and env.strip():
        return env.strip()
    locale = _config().get("locale")
    if isinstance(locale, dict):
        cv = locale.get("tz_name")
        if isinstance(cv, str) and not _is_placeholder(cv):
            return cv.strip()
    return "UTC"


def local_tz() -> timezone:
    """Fixed-offset timezone used for all generated timestamps."""
    return timezone(timedelta(hours=_tz_offset_hours()), name=_tz_name())


def tz_label() -> str:
    """Short timezone label printed in generated files."""
    return _tz_name()


# ----- Credentials (environment only, no config, no default) ---------------


def get_account_email() -> str | None:
    """Atlassian account email for HTTP Basic auth. Environment only."""
    v = os.environ.get("PA_JIRA_EMAIL")
    return v.strip() if v and v.strip() else None


def get_api_token() -> str | None:
    """Atlassian API token for HTTP Basic auth. Environment only.

    Never returned in logs, cache files, or error text. Held only long enough
    to build the auth header.
    """
    v = os.environ.get("PA_JIRA_API_TOKEN")
    return v if v and v.strip() else None
