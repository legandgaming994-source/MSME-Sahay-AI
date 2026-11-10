"""Azure Blob Storage backend for documents (gap P-1).

Same content-addressed contract as the local backend, so the service is unchanged:
  * keys are validated (`ab/<sha256>.<ext>`) before any request is made;
  * `put` never overwrites (identical content is already stored, so an existing blob is success);
  * the service re-verifies the SHA-256 on every read, so a modified blob is detected, not trusted;
  * `open_local` downloads to a private temporary directory that is removed afterwards.

The container client is injected: production uses the managed identity, tests use the Azurite emulator.
Authorisation is the platform's job: grant the workload identity 'Storage Blob Data Contributor' on the account.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .storage import StorageError, validate_key


class AzureBlobDocumentStorage:
    backend = "azure_blob"
    is_default_location = False

    def __init__(self, container, exists_errors: tuple = (), not_found_errors: tuple = ()):
        self._container = container
        self._exists_errors = tuple(exists_errors)
        self._not_found_errors = tuple(not_found_errors)

    def _blob(self, key: str):
        return self._container.get_blob_client(validate_key(key))

    def put(self, key: str, data: bytes) -> None:
        blob = self._blob(key)
        try:
            blob.upload_blob(data, overwrite=False, length=len(data))
        except self._exists_errors:
            return  # content-addressed: identical bytes are already stored

    def get(self, key: str) -> bytes:
        blob = self._blob(key)
        try:
            return blob.download_blob().readall()
        except self._not_found_errors as exc:
            raise StorageError("Stored document is missing") from exc

    def exists(self, key: str) -> bool:
        return bool(self._blob(key).exists())

    def delete(self, key: str) -> None:
        try:
            self._blob(key).delete_blob()
        except self._not_found_errors:
            pass

    @contextmanager
    def open_local(self, key: str) -> Iterator[Path]:
        data = self.get(key)
        with tempfile.TemporaryDirectory(prefix="msme-blob-") as directory:
            os.chmod(directory, 0o700)
            path = Path(directory) / key.rsplit("/", 1)[-1]
            path.write_bytes(data)
            os.chmod(path, 0o600)
            yield path

    def healthcheck(self) -> tuple[bool, str]:
        try:
            self._container.get_container_properties()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - never leak endpoints or credentials
            return False, f"blob container is not reachable ({type(exc).__name__})"


def azure_blob_from_env(env) -> AzureBlobDocumentStorage:
    try:
        from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
        from azure.storage.blob import ContainerClient
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise StorageError("azure_blob needs the optional dependencies: pip install 'lambda-erp[azure]'") from exc
    container_name = env.get("MSME_BLOB_CONTAINER") or "msme-documents"
    connection_string = env.get("MSME_BLOB_CONNECTION_STRING")
    if connection_string:
        container = ContainerClient.from_connection_string(connection_string, container_name)
    else:
        account_url = env.get("MSME_BLOB_ACCOUNT_URL")
        if not account_url:
            raise StorageError("MSME_BLOB_ACCOUNT_URL is required for the azure_blob backend")
        from azure.identity import DefaultAzureCredential
        container = ContainerClient(account_url, container_name, credential=DefaultAzureCredential())
    return AzureBlobDocumentStorage(container, (ResourceExistsError,), (ResourceNotFoundError,))
