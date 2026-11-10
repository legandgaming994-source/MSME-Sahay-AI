"""Private, content-addressed document storage.

Object keys are derived only from the SHA-256 of the content and a canonical extension, never from
user-supplied names, so path traversal through filenames is impossible by construction. Files are
written atomically with owner-only permissions. The interface is deliberately small so an object-store
implementation (gap P-1) can replace LocalDocumentStorage without touching callers.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol

_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{1,5}$")
_EXT_BY_MIME = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}


class StorageError(RuntimeError):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key_for(sha256: str, mime_type: str) -> str:
    ext = _EXT_BY_MIME.get(mime_type)
    if ext is None or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise StorageError("Unsupported storage type")
    return f"{sha256[:2]}/{sha256}.{ext}"


class DocumentStorage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def open_local(self, key: str): ...  # context manager yielding a filesystem Path
    def healthcheck(self) -> tuple[bool, str]: ...  # (ok, short secret-free detail); used by /api/ready


def validate_key(key: str) -> str:
    """Keys are content-addressed (`ab/<sha256>.<ext>`); anything else is refused by every backend."""
    if not _KEY.match(key or ""):
        raise StorageError("Invalid storage key")
    return key


class LocalDocumentStorage:
    backend = "local"

    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root or os.environ.get("MSME_STORAGE_DIR") or "/tmp/lambda-erp-msme-documents").resolve()

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise StorageError("Invalid storage key")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError("Invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        if path.exists():
            return  # content-addressed: identical bytes already stored
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError("Stored document is missing") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    @contextmanager
    def open_local(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        if not path.exists():
            raise StorageError("Stored document is missing")
        yield path

    def healthcheck(self) -> tuple[bool, str]:
        """Writable? Creates the root if needed, writes and removes a probe file."""
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".health-")
            os.close(fd)
            os.unlink(tmp)
            return True, "ok"
        except OSError as exc:
            return False, f"storage directory is not writable ({type(exc).__name__})"

    @property
    def is_default_location(self) -> bool:
        """True when running on the ephemeral default path (data would not survive a redeploy)."""
        return not os.environ.get("MSME_STORAGE_DIR")


def storage_from_env(environ=None) -> DocumentStorage:
    """Build the configured backend.

    MSME_STORAGE_BACKEND = local (default) | azure_blob
      local:       MSME_STORAGE_DIR (must be persistent, private storage in production)
      azure_blob:  MSME_BLOB_ACCOUNT_URL + MSME_BLOB_CONTAINER (default 'msme-documents'); authenticates with the
                   workload's managed identity (DefaultAzureCredential; set AZURE_CLIENT_ID for a user-assigned one).
                   MSME_BLOB_CONNECTION_STRING is honoured for emulators/development only.
    """
    env = os.environ if environ is None else environ
    backend = (env.get("MSME_STORAGE_BACKEND") or "local").strip().lower()
    if backend == "local":
        return LocalDocumentStorage()
    if backend == "azure_blob":
        from .blob_storage import azure_blob_from_env
        return azure_blob_from_env(env)
    raise StorageError(f"Unknown MSME_STORAGE_BACKEND {backend!r} (expected 'local' or 'azure_blob')")


@lru_cache(maxsize=1)
def get_storage() -> DocumentStorage:
    """Process-wide storage shared by the document service and the readiness checks."""
    return storage_from_env()
