"""Content-addressed recording and replay for external calls.

Replay is the default. Set ``ADJ_RECORD=1`` only while intentionally making live
calls and refreshing fixtures. A fixture key depends only on the named surface
and its normalized request, so identical inputs always select the same file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

FORMAT_VERSION = 1
_SURFACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]*\Z")
_SENSITIVE_KEYS = {
    "anthropic_api_key",
    "api_key",
    "authorization",
    "cookie",
    "openai_api_key",
    "password",
    "secret",
    "set_cookie",
    "xai_api_key",
}
_SENSITIVE_VALUE_PREFIXES = ("bearer ", "xai-", "sk-ant-", "sk-proj-")


class FixtureMissError(FileNotFoundError):
    """Raised when replay mode cannot find the requested fixture."""


class FixtureCorruptError(ValueError):
    """Raised when a fixture no longer matches its recorded checksums."""


class FixtureSecretError(ValueError):
    """Raised before a request containing credential material can be cached."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"fixture values must be JSON-compatible, got {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_secrets(value: Any, path: str = "request") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in _SENSITIVE_KEYS:
                raise FixtureSecretError(f"credential field cannot be cached at {path}.{key}")
            _reject_secrets(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and value.lower().startswith(_SENSITIVE_VALUE_PREFIXES):
        raise FixtureSecretError(f"credential-like value cannot be cached at {path}")


class FixtureStore:
    """Record or replay one external surface by content hash."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        record: bool | None = None,
        reuse_existing: bool | None = None,
    ):
        configured_root = root or os.environ.get("ADJ_FIXTURE_DIR") or "fixtures/api"
        self.root = Path(configured_root)
        self.record = os.environ.get("ADJ_RECORD") == "1" if record is None else record
        self.reuse_existing = (
            os.environ.get("ADJ_REUSE_FIXTURES") == "1"
            if reuse_existing is None
            else reuse_existing
        )

    def request_hash(self, surface: str, request: Any) -> str:
        self._validate_surface(surface)
        normalized_request = _jsonable(request)
        _reject_secrets(normalized_request)
        return _sha256(
            {
                "format_version": FORMAT_VERSION,
                "request": normalized_request,
                "surface": surface,
            }
        )

    def fixture_path(self, surface: str, request: Any) -> Path:
        digest = self.request_hash(surface, request)
        return self.root / surface / f"{digest}.json"

    def call(
        self,
        surface: str,
        request: Any,
        invoke: Callable[[], Any] | None = None,
    ) -> Any:
        """Return a live recorded response or a replayed response.

        ``invoke`` is required only in record mode. Replay mode never calls it.
        The normalized live response is returned so record and replay expose the
        same JSON-compatible value to downstream code.
        """

        normalized_request = _jsonable(request)
        path = self.fixture_path(surface, normalized_request)
        if self.record:
            if self.reuse_existing and path.is_file():
                return self._replay(path, surface, normalized_request)
            if invoke is None:
                raise ValueError("record mode requires an invoke callable")
            normalized_response = _jsonable(invoke())
            document = {
                "format_version": FORMAT_VERSION,
                "request": normalized_request,
                "request_sha256": path.stem,
                "response": normalized_response,
                "response_sha256": _sha256(normalized_response),
                "surface": surface,
            }
            self._write_atomic(path, document)
            return normalized_response
        return self._replay(path, surface, normalized_request)

    @staticmethod
    def _validate_surface(surface: str) -> None:
        if not _SURFACE_PATTERN.fullmatch(surface):
            raise ValueError(
                "surface must start with a lowercase letter or digit and contain only "
                "lowercase letters, digits, dots, underscores, or hyphens"
            )

    @staticmethod
    def _write_atomic(path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                dir=path.parent,
                encoding="utf-8",
                prefix=f".{path.stem}.",
                suffix=".tmp",
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _replay(path: Path, surface: str, request: Any) -> Any:
        if not path.is_file():
            raise FixtureMissError(
                f"fixture missing for {surface}: {path}. Set ADJ_RECORD=1 to record it live."
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FixtureCorruptError(f"cannot read fixture {path}: {error}") from error

        expected_request_hash = _sha256(
            {
                "format_version": FORMAT_VERSION,
                "request": request,
                "surface": surface,
            }
        )
        required = {
            "format_version",
            "request",
            "request_sha256",
            "response",
            "response_sha256",
            "surface",
        }
        if not isinstance(document, dict) or not required.issubset(document):
            raise FixtureCorruptError(f"fixture has an invalid shape: {path}")
        if document["format_version"] != FORMAT_VERSION:
            raise FixtureCorruptError(f"fixture format version changed: {path}")
        if document["surface"] != surface or document["request"] != request:
            raise FixtureCorruptError(f"fixture request does not match its path: {path}")
        if (
            document["request_sha256"] != expected_request_hash
            or path.stem != expected_request_hash
        ):
            raise FixtureCorruptError(f"fixture request hash does not match: {path}")
        if document["response_sha256"] != _sha256(document["response"]):
            raise FixtureCorruptError(f"fixture response hash does not match: {path}")
        return document["response"]
