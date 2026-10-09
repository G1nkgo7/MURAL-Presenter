"""Shared provider credential health without sensitive persistent data.

Each credential gets anonymous, immutable markers below the configured shared
resource directory. Atomic creation makes a 401/403 quarantine sticky across
hosts. Retry-After records are independent deadline markers, so concurrent
writers cannot overwrite a longer cooldown with a shorter one.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Callable, Iterable, Mapping

from .config import ProviderSettings
from .recovery import ProviderFailure


@dataclass(frozen=True)
class ProviderHealth:
    """Current shared eligibility for one anonymous provider credential."""

    quarantined: bool = False
    cooldown_remaining_s: float = 0.0
    cooldown_until: float = 0.0


class ProviderHealthStore:
    """Read and write anonymous, cross-host-safe health markers.

    This deliberately does not use ``flock``: ResourceSlots documents that it is
    not a distributed lock on the shared filesystem. Generic retry cooldowns stay
    local to a session. Only an explicit provider ``Retry-After`` becomes shared.
    Auth state is never automatically cleared; ``reset`` is an operator action.
    """

    _IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
    _COOLDOWN = re.compile(r"([0-9]{1,20})-[0-9a-f]{32}\.cooldown\Z")

    def __init__(self, directory: str | Path, *, wall_clock: Callable[[], float] = time.time) -> None:
        self.root = Path(directory) / "provider-health-v1"
        self.root.mkdir(parents=True, exist_ok=True)
        self.wall_clock = wall_clock

    @staticmethod
    def identity(settings: ProviderSettings, credential: str) -> str:
        """Hash actual provider contract plus a stable credential digest.

        Channel aliases are intentionally absent: their order and labels are not
        credential identity, while base URL, model, and API format are.
        """
        material = {
            "api_format": settings.api_format,
            "base_url": settings.base_url,
            "credential_digest": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
            "model": settings.model,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def snapshot(self, identities: Iterable[str]) -> Mapping[str, ProviderHealth]:
        """Read one lock-free eligibility snapshot for the current scheduling pass."""
        return {identity: self.status(identity) for identity in dict.fromkeys(identities)}

    def status(self, identity: str) -> ProviderHealth:
        """Read immutable markers; reads never block on writers or mutate state."""
        root = self._credential_root(identity)
        quarantined = (root / "auth-quarantined").is_file()
        cooldown_until = 0.0
        cooldowns = root / "cooldowns"
        try:
            entries = tuple(cooldowns.iterdir())
        except FileNotFoundError:
            entries = ()
        for path in entries:
            match = self._COOLDOWN.fullmatch(path.name)
            if match is not None:
                cooldown_until = max(cooldown_until, int(match.group(1)) / 1_000_000_000)
        return ProviderHealth(
            quarantined=quarantined,
            cooldown_remaining_s=max(0.0, cooldown_until - self.wall_clock()),
            cooldown_until=cooldown_until,
        )

    def record_failure(self, identity: str, failure: ProviderFailure) -> ProviderHealth:
        """Atomically record permanent auth rejection or an explicit Retry-After."""
        root = self._credential_root(identity)
        if failure.status_code in {401, 403}:
            root.mkdir(exist_ok=True)
            self._create_once(root / "auth-quarantined")
        elif (failure.retryable and failure.kind != "provider_protocol_error"
              and failure.retry_after_s is not None and failure.retry_after_s > 0):
            cooldowns = root / "cooldowns"
            cooldowns.mkdir(parents=True, exist_ok=True)
            until_ns = int((self.wall_clock() + failure.retry_after_s) * 1_000_000_000)
            self._create_once(cooldowns / f"{until_ns}-{uuid.uuid4().hex}.cooldown")
        return self.status(identity)

    def reset(self, identity: str) -> None:
        """Explicitly clear only an auth quarantine after access is repaired.

        Nothing calls this automatically. The caller supplies the anonymous identity
        computed by this process, not a raw key or provider URL.
        """
        (self._credential_root(identity) / "auth-quarantined").unlink(missing_ok=True)

    def _credential_root(self, identity: str) -> Path:
        if not self._IDENTITY.fullmatch(identity):
            raise ValueError("provider health identity is invalid")
        return self.root / identity

    @staticmethod
    def _create_once(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
