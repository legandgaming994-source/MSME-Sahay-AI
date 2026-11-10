#!/usr/bin/env python3
"""Storage backends: one contract, two implementations (local disk, Azure Blob via the REAL SDK against Azurite).

Azurite is the official Azure Storage emulator (same REST protocol); it is not Azure itself, so authentication
(managed identity), network access and RBAC are NOT exercised here. Set REQUIRE_AZURITE=1 (CI does) to fail
instead of skip when the emulator is unavailable.

Run:  python -m tests.test_msme_storage
"""

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
CONN = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")   # Microsoft's published, public emulator credential


def find_azurite():
    for candidate in (os.environ.get("AZURITE_BIN"), shutil.which("azurite-blob"), "/tmp/azurite_dir/node_modules/.bin/azurite-blob"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def main() -> int:
    from tests._msme_db import reset_test_db

    db_path = reset_test_db("lambda_msme_storage_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "", "JWT_SECRET_KEY": "synthetic-storage"})
    from lambda_erp.database import get_db, setup
    setup(db_path)

    from msme.documents.extraction import ExtractedField, ExtractionOutcome
    from msme.documents.service import DocumentError, DocumentService
    from msme.documents.storage import LocalDocumentStorage, StorageError, key_for, storage_from_env

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    def raises(exc_type, fn, *a, **k):
        try:
            fn(*a, **k)
        except exc_type:
            return True
        except Exception:  # noqa: BLE001
            return False
        return False

    backends = [("local", LocalDocumentStorage(tempfile.mkdtemp(prefix="lambda_msme_store_")), None)]
    proc = None
    azurite = find_azurite()
    if azurite:
        data_dir = tempfile.mkdtemp(prefix="azurite_")
        proc = subprocess.Popen([azurite, "--silent", "--blobHost", "127.0.0.1", "--blobPort", "10000", "--location", data_dir,
                                 "--skipApiVersionCheck"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", 10000), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.5)
        from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
        from azure.storage.blob import ContainerClient
        from msme.documents.blob_storage import AzureBlobDocumentStorage
        container = ContainerClient.from_connection_string(CONN, "msme-documents")
        container.create_container()
        backends.append(("azure_blob (Azurite)", AzureBlobDocumentStorage(container, (ResourceExistsError,), (ResourceNotFoundError,)), container))
    elif os.environ.get("REQUIRE_AZURITE") == "1":
        print("FAIL Azurite is required (REQUIRE_AZURITE=1) but was not found")
        return 1
    else:
        print("SKIP azure_blob contract tests (Azurite not installed; npm install azurite)")

    try:
        for label, store, container in backends:
            tag = f"[{label}] "
            payload = b"%PDF-1.4\n% storage contract " + label.encode() + b"\n%%EOF\n"
            sha = hashlib.sha256(payload).hexdigest()
            key = key_for(sha, "application/pdf")
            check(tag + "healthcheck reports healthy with a short detail", store.healthcheck() == (True, "ok"), str(store.healthcheck()))
            check(tag + "missing object: exists is False and get raises StorageError", not store.exists(key) and raises(StorageError, store.get, key))
            store.put(key, payload)
            check(tag + "put/get round-trips the exact bytes", store.exists(key) and store.get(key) == payload)
            store.put(key, b"DIFFERENT BYTES MUST NOT OVERWRITE")
            check(tag + "put never overwrites existing content (content-addressed, idempotent)", store.get(key) == payload)
            with store.open_local(key) as path:
                check(tag + "open_local exposes a real file with the same bytes", Path(path).read_bytes() == payload)
                kept = Path(path)
            if label.startswith("azure"):
                check(tag + "the temporary download is removed afterwards", not kept.exists())
            for bad in ("../../etc/passwd", "ab/" + "0" * 63 + ".pdf", "AB/" + "a" * 64 + ".pdf", "ab/" + "a" * 64, "", "/abs/" + "a" * 64 + ".pdf", "ab/" + "a" * 64 + ".exe/../x"):
                ok = all(raises(StorageError, f, bad) for f in (store.get, lambda k: store.put(k, b"x"), store.exists, store.delete))
                if not ok:
                    check(tag + f"invalid key {bad!r} is refused by every operation", False)
                    break
            else:
                check(tag + "invalid or traversal keys are refused by every operation", True)
            errs = []
            race_data = b"%PDF-1.4\n% race " + label.encode() + b"\n%%EOF\n"
            race_key = key_for(hashlib.sha256(race_data).hexdigest(), "application/pdf")

            def race():
                try:
                    store.put(race_key, race_data)
                except Exception as exc:  # noqa: BLE001
                    errs.append(repr(exc))

            ts = [threading.Thread(target=race) for _ in range(8)]
            [t.start() for t in ts]; [t.join() for t in ts]
            check(tag + "8 concurrent identical puts all succeed and store the content once", not errs and store.get(race_key) == race_data, str(errs))
            big = b"%PDF-1.4\n" + os.urandom(5 * 1024 * 1024)
            big_key = key_for(hashlib.sha256(big).hexdigest(), "application/pdf")
            store.put(big_key, big)
            check(tag + "a 5 MB object round-trips", store.get(big_key) == big)
            store.delete(key); store.delete(key)
            check(tag + "delete is idempotent", not store.exists(key))

            # the document service on top of this backend
            cur = {"invoice_number": f"ST-{label[:3]}", "issue_date": "2026-06-10", "seller.name": "S", "subtotal": "100.00", "tax_amount": "18.00", "total_amount": "118.00"}

            class Fake:
                name, version = "fake", "1"

                def extract(self, path):
                    assert Path(path).exists() and Path(path).read_bytes()[:5] == b"%PDF-", "extractor must receive a real local file"
                    return ExtractionOutcome("extracted", "invoice", [ExtractedField(k, v, .9, 1, v, None, "verified") for k, v in cur.items()],
                                             extractor="fake", extractor_version="1")

            svc = DocumentService(Fake(), store)
            body = b"%PDF-1.4\n% service over " + label.encode() + b"\n%%EOF\n"
            doc, dup = svc.ingest("u", "s.pdf", body, "application/pdf")
            done = svc.process(doc["id"])
            check(tag + "document service ingests, hands the extractor a real file, and reads the original back",
                  not dup and done["status"] in ("extracted", "needs_review") and svc.read_original(doc["id"])[0] == body, str(done))
            skey = key_for(hashlib.sha256(body).hexdigest(), "application/pdf")
            if container is not None:
                container.get_blob_client(skey).upload_blob(b"tampered", overwrite=True)      # attacker edits the blob directly
            else:
                Path(store.root, skey).write_bytes(b"tampered")
            try:
                svc.read_original(doc["id"])
                tamper = False
            except DocumentError as exc:
                tamper = exc.code == "storage_corrupt"
            check(tag + "a blob modified behind the service's back is detected by the checksum, not trusted", tamper)

        # failure reporting must not leak endpoints or credentials
        if container is not None:
            from azure.storage.blob import ContainerClient
            bad = ContainerClient.from_connection_string(CONN, "does-not-exist")
            ok, detail = AzureBlobDocumentStorage(bad, (ResourceExistsError,), (ResourceNotFoundError,)).healthcheck()
            check("[azure_blob] healthcheck against a missing container fails with a secret-free detail",
                  not ok and "devstoreaccount1" not in detail and "127.0.0.1" not in detail and "AccountKey" not in detail, detail)
        class LeakyContainer:
            def get_container_properties(self):
                raise RuntimeError("GET https://prodacct.blob.core.windows.net/msme-documents?restype=container&sig=SECRETSIG "
                                   "AccountKey=SECRETKEY failed")
        from msme.documents.blob_storage import AzureBlobDocumentStorage as _Blob
        ok, detail = _Blob(LeakyContainer()).healthcheck()
        check("[azure_blob] a failure whose message holds a URL, SAS signature and key is reported without any of them",
              not ok and not any(x in detail for x in ("prodacct", "SECRETSIG", "SECRETKEY", "blob.core.windows.net", "sig=")) and "RuntimeError" in detail, detail)
        broken = LocalDocumentStorage("/proc/forbidden-lambda-msme")
        ok, detail = broken.healthcheck()
        check("[local] an unwritable directory fails the healthcheck without leaking the path", not ok and "/proc" not in detail, detail)

        # configuration errors
        check("unknown backend and missing account URL are refused at startup",
              raises(StorageError, storage_from_env, {"MSME_STORAGE_BACKEND": "s3"}) and raises(StorageError, storage_from_env, {"MSME_STORAGE_BACKEND": "azure_blob"}))
        if azurite:
            built = storage_from_env({"MSME_STORAGE_BACKEND": "azure_blob", "MSME_BLOB_CONNECTION_STRING": CONN, "MSME_BLOB_CONTAINER": "msme-documents"})
            check("the env-driven factory builds a working Azure backend", built.backend == "azure_blob" and built.healthcheck()[0])
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
