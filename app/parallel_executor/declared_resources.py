"""
Declared Resources Module
=========================
Tools declare what resources they need (file paths, terminal sessions, network
endpoints, etc.) so the ParallelToolExecutor can schedule them safely.

Every declaration carries a *mode* — READ (shared, non-mutating) or WRITE
(exclusive, mutating).  The lock manager uses these modes to decide which
tool calls can run concurrently and which must be serialised.

Design goals
------------
* Immutable value objects — ``ResourceDeclaration`` is frozen.
* Convenient factory helpers on ``DeclaredResources`` for common patterns.
* Conflict detection between two sets of declarations (used by the scheduler).
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, Sequence


# ──────────────────────────────────────────────────────────────────────────────
# Access mode
# ──────────────────────────────────────────────────────────────────────────────

class AccessMode(enum.Enum):
    """Lock mode for a declared resource.

    * **READ**  — multiple readers may hold the resource simultaneously.
    * **WRITE** — exclusive access; no other reader or writer may overlap.
    """

    READ = "READ"
    WRITE = "WRITE"


# ──────────────────────────────────────────────────────────────────────────────
# Resource declaration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ResourceDeclaration:
    """A single resource requirement declared by a tool.

    Parameters
    ----------
    resource_type:
        Category of the resource (e.g. ``"file"``, ``"terminal"``,
        ``"network"``).  This is purely informational for logging and
        debugging; the lock manager only cares about *resource_id*.
    resource_id:
        Globally-unique identifier for the specific resource instance.
        For files this would be the normalised absolute path; for a
        terminal it might be the session UUID, etc.
    mode:
        Whether the tool intends to read or write the resource.
    """

    resource_type: str
    resource_id: str
    mode: AccessMode

    # ── derived helpers ──────────────────────────────────────────────────

    @property
    def is_write(self) -> bool:
        """Return ``True`` if this declaration requests write access."""
        return self.mode is AccessMode.WRITE

    @property
    def is_read(self) -> bool:
        """Return ``True`` if this declaration requests read access."""
        return self.mode is AccessMode.READ

    @property
    def lock_key(self) -> str:
        """Stable, hashable key used by the lock manager internally.

        The key is built from the *resource_type* and *resource_id* so that
        two declarations referring to the same logical resource always map to
        the same lock, even if they were created independently.
        """
        raw = f"{self.resource_type}::{self.resource_id}"
        # Use a short SHA-256 digest to avoid problematic chars in dict keys
        # while keeping the full 256-bit collision resistance.
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ── dunder helpers ───────────────────────────────────────────────────

    def __str__(self) -> str:  # pragma: no cover
        mode_char = "W" if self.is_write else "R"
        return f"{self.resource_type}:{self.resource_id}({mode_char})"

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ResourceDeclaration({self.resource_type!r}, "
            f"{self.resource_id!r}, {self.mode!r})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Declared resources collection
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DeclaredResources:
    """An immutable, deduplicated collection of :class:`ResourceDeclaration`
    objects associated with a single tool invocation.

    Create instances via the constructor or the convenience factory helpers
    (:meth:`file_resource`, :meth:`terminal_resource`, etc.).
    """

    declarations: FrozenSet[ResourceDeclaration] = field(default_factory=frozenset)

    # ── constructors / factory helpers ────────────────────────────────────

    def __init__(self, declarations: Iterable[ResourceDeclaration] = ()) -> None:
        """Accept any iterable of declarations; duplicates are dropped."""
        object.__setattr__(self, "declarations", frozenset(declarations))

    @staticmethod
    def empty() -> DeclaredResources:
        """Return an empty resource set (no resources required)."""
        return DeclaredResources()

    # -- common patterns ---------------------------------------------------

    @staticmethod
    def file_resource(path: str, mode: AccessMode = AccessMode.READ) -> DeclaredResources:
        """Declare access to a single file.

        Parameters
        ----------
        path:
            Absolute or relative file path.  The path is **normalised**
            (``os.path.normpath``) so that ``./foo`` and ``foo`` are
            treated as the same resource.
        mode:
            READ (default) or WRITE.
        """
        import os
        normalised = os.path.normpath(os.path.abspath(path))
        return DeclaredResources([ResourceDeclaration("file", normalised, mode)])

    @staticmethod
    def terminal_resource(
        session_id: str,
        mode: AccessMode = AccessMode.WRITE,
    ) -> DeclaredResources:
        """Declare access to a terminal session.

        Terminals are almost always WRITE because interactive sessions are
        inherently stateful — even reading the buffer can race with output.
        """
        return DeclaredResources([ResourceDeclaration("terminal", session_id, mode)])

    @staticmethod
    def network_resource(
        host: str,
        port: int | None = None,
        mode: AccessMode = AccessMode.READ,
    ) -> DeclaredResources:
        """Declare access to a network endpoint.

        Parameters
        ----------
        host:
            Hostname or IP address.
        port:
            Optional port number.  If omitted the resource ID is just the
            host, meaning any connection to that host is treated as the
            same resource.
        mode:
            READ (e.g. HTTP GET) or WRITE (e.g. POST / state-mutating call).
        """
        resource_id = f"{host}:{port}" if port is not None else host
        return DeclaredResources([ResourceDeclaration("network", resource_id, mode)])

    # ── set-like operations ──────────────────────────────────────────────

    def union(self, other: DeclaredResources) -> DeclaredResources:
        """Return a new :class:`DeclaredResources` that is the union of
        *self* and *other*."""
        return DeclaredResources(self.declarations | other.declarations)

    def intersection(self, other: DeclaredResources) -> DeclaredResources:
        """Return declarations that appear in both sets (by *lock_key*)."""
        other_keys = {d.lock_key for d in other.declarations}
        return DeclaredResources(
            d for d in self.declarations if d.lock_key in other_keys
        )

    # ── query helpers ────────────────────────────────────────────────────

    @property
    def is_empty(self) -> bool:
        """``True`` if no resources are declared."""
        return len(self.declarations) == 0

    @property
    def write_declarations(self) -> Sequence[ResourceDeclaration]:
        """All declarations requesting WRITE access."""
        return tuple(d for d in self.declarations if d.is_write)

    @property
    def read_declarations(self) -> Sequence[ResourceDeclaration]:
        """All declarations requesting READ access."""
        return tuple(d for d in self.declarations if d.is_read)

    @property
    def has_writes(self) -> bool:
        """``True`` if any declaration requests WRITE access."""
        return any(d.is_write for d in self.declarations)

    @property
    def lock_keys(self) -> set[str]:
        """The set of lock keys for all declarations."""
        return {d.lock_key for d in self.declarations}

    # ── conflict detection ───────────────────────────────────────────────

    def conflicts_with(self, other: DeclaredResources) -> bool:
        """Return ``True`` if the two resource sets cannot be held
        simultaneously.

        Two sets conflict if **any** of the following is true for a shared
        resource key:

        1. Either set requests WRITE on that key.
        2. Both sets reference the same key, regardless of mode (because
           even two READ-locked resources might be upgraded later — we are
           conservative and treat same-key with any WRITE as conflict,
           but pure READ-READ on the same key is safe).

        More precisely:
        * READ  + READ  → no conflict
        * READ  + WRITE → conflict
        * WRITE + READ  → conflict
        * WRITE + WRITE → conflict
        """
        # Fast path: if either set is empty, no conflict possible.
        if self.is_empty or other.is_empty:
            return False

        self_by_key: dict[str, ResourceDeclaration] = {
            d.lock_key: d for d in self.declarations
        }
        other_by_key: dict[str, ResourceDeclaration] = {
            d.lock_key: d for d in other.declarations
        }

        # Find overlapping keys
        common_keys = set(self_by_key.keys()) & set(other_by_key.keys())
        for key in common_keys:
            sd = self_by_key[key]
            od = other_by_key[key]
            # Conflict if either side is a WRITE
            if sd.is_write or od.is_write:
                return True

        return False

    # ── dunder helpers ───────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.declarations)

    def __contains__(self, item: ResourceDeclaration) -> bool:
        return item in self.declarations

    def __iter__(self):
        return iter(self.declarations)

    def __bool__(self) -> bool:
        return not self.is_empty

    def __str__(self) -> str:  # pragma: no cover
        decls = ", ".join(str(d) for d in sorted(self.declarations, key=lambda d: d.lock_key))
        return f"DeclaredResources([{decls}])"

    def __repr__(self) -> str:  # pragma: no cover
        return f"DeclaredResources({self.declarations!r})"
