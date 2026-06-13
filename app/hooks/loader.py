from __future__ import annotations

"""
ManusClaw Hooks — Dynamic Hook Loader
=======================================
Loads hook configurations from YAML files, Python modules, and the
manusclaw config system, then registers them with a HookManager.

YAML configuration format (``hooks.yaml`` or ``~/.manusclaw/hooks.yaml``):

    hooks:
      - name: logging
        enabled: true
        priority: 200
        config:
          log_level: INFO
          max_arg_length: 300

      - name: security
        enabled: true
        priority: 10
        config:
          deny_threshold: HIGH
          scan_prompts: true

      - name: audit
        enabled: true
        config:
          audit_file: logs/audit.jsonl
          audit_db: false
          sanitize: true

      # Custom hook from a Python module
      - name: my_custom_hook
        class_path: mypackage.hooks.CustomHook
        enabled: true
        priority: 50
        config:
          custom_option: value

Loading sources (in priority order, last wins):
    1. Built-in defaults (empty — no hooks auto-loaded)
    2. ~/.manusclaw/hooks.yaml
    3. ./hooks.yaml (project-local)
    4. Explicit path passed to ``load_from_yaml``
    5. Programmatic registration via ``HookManager.register``
"""

import importlib
from pathlib import Path
from typing import Any, Optional

from app.hooks.base import HookBase
from app.hooks.builtin import AuditHook, LoggingHook, SecurityHook
from app.hooks.manager import HookManager
from app.hooks.types import HookEventType
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Built-in hook registry — maps short names to classes
# ──────────────────────────────────────────────────────────────────────────────

BUILTIN_HOOKS: dict[str, type[HookBase]] = {
    "logging":  LoggingHook,
    "security": SecurityHook,
    "audit":    AuditHook,
}


# ──────────────────────────────────────────────────────────────────────────────
# YAML support
# ──────────────────────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict on failure."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except ImportError:
        logger.warning("[HookLoader] PyYAML not installed — cannot load YAML config")
        return {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"[HookLoader] Failed to load YAML from {path}: {e}")
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Python module import
# ──────────────────────────────────────────────────────────────────────────────

def _import_class(class_path: str) -> Optional[type[HookBase]]:
    """
    Import a hook class from a dotted module path.

    Args:
        class_path: Fully-qualified dotted path, e.g. ``"mypackage.hooks.CustomHook"``.
                    The last component is the class name; everything before it
                    is the module path.

    Returns:
        The imported class, or None if import failed.
    """
    try:
        module_path, _, class_name = class_path.rpartition(".")
        if not module_path:
            logger.error(
                f"[HookLoader] Invalid class_path '{class_path}' — "
                "must be in 'module.ClassName' format"
            )
            return None

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name, None)
        if cls is None:
            logger.error(
                f"[HookLoader] Class '{class_name}' not found in module '{module_path}'"
            )
            return None

        if not (isinstance(cls, type) and issubclass(cls, HookBase)):
            logger.error(
                f"[HookLoader] '{class_path}' is not a HookBase subclass"
            )
            return None

        return cls

    except ImportError as e:
        logger.error(f"[HookLoader] Cannot import module for '{class_path}': {e}")
        return None
    except Exception as e:
        logger.error(f"[HookLoader] Error loading class '{class_path}': {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Hook Loader
# ──────────────────────────────────────────────────────────────────────────────

class HookLoader:
    """
    Loads and instantiates hooks from configuration sources.

    Usage:
        loader = HookLoader()
        hooks = loader.load_from_yaml("hooks.yaml")
        manager = HookManager()
        manager.register_many(*hooks)
        await manager.initialize()

    The loader supports three hook sources:
        1. **Built-in hooks** — referenced by name (e.g. ``"logging"``).
        2. **Python modules** — referenced by ``class_path`` (e.g. ``"my_pkg.MyHook"``).
        3. **YAML config** — declarative hook configuration with per-hook settings.
    """

    def __init__(self, extra_builtins: Optional[dict[str, type[HookBase]]] = None) -> None:
        """
        Args:
            extra_builtins: Additional name→class mappings to register as
                            built-in hooks (e.g. from plugins).
        """
        self._registry: dict[str, type[HookBase]] = dict(BUILTIN_HOOKS)
        if extra_builtins:
            self._registry.update(extra_builtins)

    # ──────────────────────────────────────────────────────────────────────
    # Registry management
    # ──────────────────────────────────────────────────────────────────────

    def register_builtin(self, name: str, hook_class: type[HookBase]) -> None:
        """Register an additional built-in hook class by name."""
        self._registry[name] = hook_class

    def list_builtins(self) -> list[str]:
        """Return the names of all registered built-in hooks."""
        return list(self._registry.keys())

    # ──────────────────────────────────────────────────────────────────────
    # Single hook instantiation
    # ──────────────────────────────────────────────────────────────────────

    def _create_hook(
        self,
        name: str,
        class_path: Optional[str] = None,
        enabled: bool = True,
        priority: Optional[int] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> Optional[HookBase]:
        """
        Instantiate a single hook from its configuration.

        Resolution order:
            1. If ``class_path`` is given, import and instantiate.
            2. Otherwise, look up ``name`` in the built-in registry.
            3. If neither resolves, log a warning and return None.

        Args:
            name:        Hook identifier (used for built-in lookup).
            class_path:  Dotted path to a HookBase subclass.
            enabled:     Whether the hook should be active.
            priority:    Override the hook's default priority.
            config:      Additional keyword arguments to pass to ``hook.configure()``.

        Returns:
            A configured HookBase instance, or None on failure.
        """
        hook_class: Optional[type[HookBase]] = None

        # Resolve class
        if class_path:
            hook_class = _import_class(class_path)
        else:
            hook_class = self._registry.get(name)

        if hook_class is None:
            logger.warning(
                f"[HookLoader] Cannot resolve hook '{name}' "
                f"(class_path={class_path!r}, builtins={list(self._registry.keys())})"
            )
            return None

        # Instantiate
        try:
            hook = hook_class()
        except Exception as e:
            logger.error(f"[HookLoader] Failed to instantiate hook '{name}': {e}")
            return None

        # Apply configuration
        hook.name = name
        hook.enabled = enabled

        if priority is not None:
            hook.priority = priority

        if config:
            try:
                hook.configure(**config)
            except Exception as e:
                logger.error(
                    f"[HookLoader] Failed to configure hook '{name}' "
                    f"with config {config}: {e}"
                )

        return hook

    # ──────────────────────────────────────────────────────────────────────
    # YAML loading
    # ──────────────────────────────────────────────────────────────────────

    def load_from_yaml(self, path: str | Path) -> list[HookBase]:
        """
        Load hooks from a YAML configuration file.

        The file should have a top-level ``hooks`` key containing a list
        of hook definitions.  Each definition must have at least a ``name``
        key.  See the module docstring for the full format.

        Args:
            path: Path to the YAML file.

        Returns:
            List of instantiated HookBase objects (excluding failed entries).
        """
        yaml_path = Path(path)
        if not yaml_path.exists():
            logger.debug(f"[HookLoader] YAML config not found: {yaml_path}")
            return []

        data = _load_yaml(yaml_path)
        hook_defs = data.get("hooks", [])
        if not isinstance(hook_defs, list):
            logger.warning("[HookLoader] 'hooks' key must be a list in YAML config")
            return []

        return self._from_definitions(hook_defs)

    # ──────────────────────────────────────────────────────────────────────
    # Dict / list loading
    # ──────────────────────────────────────────────────────────────────────

    def load_from_config(self, config: dict[str, Any]) -> list[HookBase]:
        """
        Load hooks from a configuration dictionary.

        Expected structure::

            {
                "hooks": [
                    {"name": "logging", "enabled": true, ...},
                    {"name": "custom", "class_path": "pkg.Custom", ...},
                ]
            }

        Args:
            config: Configuration dictionary.

        Returns:
            List of instantiated hooks.
        """
        hook_defs = config.get("hooks", [])
        if not isinstance(hook_defs, list):
            logger.warning("[HookLoader] 'hooks' key must be a list in config dict")
            return []
        return self._from_definitions(hook_defs)

    # ──────────────────────────────────────────────────────────────────────
    # Auto-discovery from manusclaw home
    # ──────────────────────────────────────────────────────────────────────

    def load_from_default_locations(self) -> list[HookBase]:
        """
        Search standard manusclaw config locations for hook definitions.

        Locations searched (last wins):
            1. ``~/.manusclaw/hooks.yaml``
            2. ``./hooks.yaml`` (project-local)

        Returns:
            Merged list of hooks from all found config files.
        """
        import os

        home = Path(os.getenv("MANUSCLAW_HOME", str(Path.home() / ".manusclaw")))
        candidates = [
            home / "hooks.yaml",
            Path("hooks.yaml"),
        ]

        hooks: list[HookBase] = []
        for candidate in candidates:
            if candidate.exists():
                logger.debug(f"[HookLoader] Found hooks config: {candidate}")
                loaded = self.load_from_yaml(candidate)
                hooks.extend(loaded)

        return hooks

    # ──────────────────────────────────────────────────────────────────────
    # Convenience: load + register
    # ──────────────────────────────────────────────────────────────────────

    async def load_and_register(
        self,
        manager: HookManager,
        path: Optional[str | Path] = None,
    ) -> list[HookBase]:
        """
        Load hooks from the given path (or default locations) and register
        them with the provided HookManager, then call ``manager.initialize()``.

        Args:
            manager: The HookManager to register hooks with.
            path:    Optional YAML file path. If None, uses default locations.

        Returns:
            List of registered hooks.
        """
        if path:
            hooks = self.load_from_yaml(path)
        else:
            hooks = self.load_from_default_locations()

        if hooks:
            manager.register_many(*hooks)
            await manager.initialize()
            logger.info(
                f"[HookLoader] Loaded and registered {len(hooks)} hook(s): "
                f"{[h.name for h in hooks]}"
            )
        else:
            logger.debug("[HookLoader] No hooks found to register")

        return hooks

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _from_definitions(self, definitions: list[dict[str, Any]]) -> list[HookBase]:
        """
        Process a list of hook definition dicts and return instantiated hooks.
        """
        hooks: list[HookBase] = []
        for i, defn in enumerate(definitions):
            if not isinstance(defn, dict):
                logger.warning(f"[HookLoader] Hook definition #{i} is not a dict — skipping")
                continue

            name = defn.get("name", "")
            if not name:
                logger.warning(f"[HookLoader] Hook definition #{i} missing 'name' — skipping")
                continue

            hook = self._create_hook(
                name=name,
                class_path=defn.get("class_path"),
                enabled=defn.get("enabled", True),
                priority=defn.get("priority"),
                config=defn.get("config"),
            )

            if hook is not None:
                hooks.append(hook)
                logger.debug(f"[HookLoader] Loaded hook: {hook}")
            else:
                logger.warning(f"[HookLoader] Failed to load hook '{name}' — skipping")

        return hooks
