#!/usr/bin/env python3
"""render.yaml: spec-derived lint plus policy and code cross-checks.

This is NOT Render's own validator. Render validates Blueprints server-side (`render blueprints validate`, needs
`render login`), which cannot run in CI without credentials. This test encodes what was verified from Render's published
Blueprint reference (https://render.com/docs/blueprint-spec) and adds checks no generic validator can make: every
environment variable must really be read by the code, the health-check route must exist, secrets must not be literals.

Run:  python -m tests.test_render_blueprint
"""

import os
import re
import stat
import sys
import tempfile
import warnings
from pathlib import Path

import yaml

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

REGIONS = {"oregon", "ohio", "virginia", "frankfurt", "singapore"}
ROOT_KEYS = {"services", "databases", "envVarGroups", "projects", "previews", "ungrouped"}
SERVICE_KEYS = {"type", "name", "runtime", "repo", "branch", "region", "plan", "rootDir", "buildFilter", "dockerfilePath",
                "dockerContext", "dockerCommand", "registryCredential", "image", "startCommand", "buildCommand",
                "preDeployCommand", "healthCheckPath", "autoDeployTrigger", "autoDeploy", "maxShutdownDelaySeconds",
                "numInstances", "scaling", "disk", "envVars", "domains", "headers", "routes", "previews", "buildPlan",
                "initialDeployHook", "schedule", "ipAllowList", "maintenanceMode", "pullRequestPreviewsEnabled", "plan"}
DB_KEYS = {"name", "plan", "region", "postgresMajorVersion", "databaseName", "user", "ipAllowList", "diskSizeGB",
           "readReplicas", "highAvailability", "previewPlan", "previewDiskSizeGB", "projectId", "environmentId"}
DISK_KEYS = {"name", "mountPath", "sizeGB"}
ENV_SOURCES = ("value", "generateValue", "sync", "fromDatabase", "fromService", "fromGroup")
RESERVED_MOUNTS = ("/opt/render", "/home", "/etc")
COMPUTE_PLAN = re.compile(r"^\d+(\.\d+)?c-\d+(g|mb)$")          # Render's current compute naming, e.g. 1c-2g, 0.5c-1g
SECRET_KEY = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN)", re.I)
PLATFORM_ENV = {"PORT", "PYTHONUNBUFFERED"}                       # not read by application code by name


def main() -> int:
    warnings.filterwarnings("ignore")
    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    spec = yaml.safe_load((ROOT / "render.yaml").read_text())
    services, databases = spec.get("services", []), spec.get("databases", [])
    check("only documented top-level keys", set(spec) <= ROOT_KEYS, str(set(spec) - ROOT_KEYS))
    web = next((s for s in services if s.get("type") == "web"), None)
    db = databases[0] if databases else None
    check("one web service and one database are defined", web is not None and db is not None and len(services) == 1 and len(databases) == 1)

    # ---------------- structure derived from the published reference ----------------
    check("service keys are all documented fields (typo guard)", set(web) <= SERVICE_KEYS, str(set(web) - SERVICE_KEYS))
    check("database keys are all documented fields (typo guard)", set(db) <= DB_KEYS, str(set(db) - DB_KEYS))
    check("docker runtime with an existing Dockerfile", web["runtime"] == "docker" and (ROOT / web["dockerfilePath"]).is_file())
    check("regions are valid and identical (private networking needs the same region)", web["region"] in REGIONS and web["region"] == db["region"], f"{web['region']} vs {db['region']}")
    check("plans use Render's compute naming and are paid", all(COMPUTE_PLAN.match(p["plan"]) for p in (web, db)) and web["plan"] != "free" and db["plan"] != "free", f"{web['plan']} {db['plan']}")
    check("postgresMajorVersion is a string", isinstance(db["postgresMajorVersion"], str))
    check("database disk size, when given, is 1 or a multiple of 5", "diskSizeGB" not in db or db["diskSizeGB"] == 1 or db["diskSizeGB"] % 5 == 0)
    check("autoDeployTrigger is a documented value", web["autoDeployTrigger"] in ("commit", "checksPass", "off"))
    check("maxShutdownDelaySeconds is within 1..300", isinstance(web["maxShutdownDelaySeconds"], int) and 1 <= web["maxShutdownDelaySeconds"] <= 300)
    check("healthCheckPath is an absolute path", web["healthCheckPath"].startswith("/"))
    disk = web["disk"]
    check("disk has exactly the documented keys and a positive integer size", set(disk) == DISK_KEYS and isinstance(disk["sizeGB"], int) and disk["sizeGB"] > 0)
    mount = disk["mountPath"]
    check("disk mount path is absolute and not a reserved path", mount.startswith("/") and mount != "/" and not any(mount == r or mount.startswith(r + "/") for r in RESERVED_MOUNTS), mount)

    envs = web["envVars"]
    by_key = {e["key"]: e for e in envs}
    check("env var keys are unique", len(by_key) == len(envs))
    check("every env var has exactly one source", all(sum(1 for s in ENV_SOURCES if s in e) == 1 for e in envs), str([e["key"] for e in envs if sum(1 for s in ENV_SOURCES if s in e) != 1]))
    check("sync, when present, is false (secrets are prompted for, never stored here)", all(e["sync"] is False for e in envs if "sync" in e))
    check("fromDatabase refers to a defined database and a documented property",
          all(e["fromDatabase"]["name"] == db["name"] and e["fromDatabase"]["property"] in ("connectionString", "host", "port", "user", "password", "database") for e in envs if "fromDatabase" in e))

    # ---------------- policy ----------------
    check("a persistent disk requires exactly one instance", web.get("numInstances") == 1 and "scaling" not in web)
    check("no secret-like variable carries a literal value", not [e["key"] for e in envs if SECRET_KEY.search(e["key"]) and "value" in e], str([e["key"] for e in envs if SECRET_KEY.search(e["key"]) and "value" in e]))
    check("the JWT signing secret is generated by Render", by_key["JWT_SECRET_KEY"].get("generateValue") is True)
    check("the first admin is seeded from prompted values (nobody can claim admin by registering first)",
          by_key["LAMBDA_ERP_ADMIN_EMAIL"].get("sync") is False and by_key["LAMBDA_ERP_ADMIN_PASSWORD"].get("sync") is False)
    check("demo seeding is off (the image defaults it ON)", by_key["LAMBDA_ERP_AUTO_DEMO"]["value"] == "0" and "LAMBDA_ERP_AUTO_DEMO=1" in (ROOT / "Dockerfile").read_text().replace(" ", ""))
    check("the database accepts no external connections (empty ipAllowList)", db.get("ipAllowList") == [])
    check("the app database URL comes from the managed database (private connection string)", by_key["LAMBDA_ERP_DB"]["fromDatabase"] == {"name": db["name"], "property": "connectionString"})
    check("documents and chat uploads both live on the persistent disk",
          by_key["MSME_STORAGE_DIR"]["value"].startswith(mount + "/") and by_key["LAMBDA_ERP_UPLOAD_DIR"]["value"].startswith(mount + "/")
          and by_key["MSME_STORAGE_DIR"]["value"] != by_key["LAMBDA_ERP_UPLOAD_DIR"]["value"])
    check("maker-checker is on by default", by_key["MSME_REQUIRE_DISTINCT_APPROVER"]["value"] == "1")
    check("deploys are gated on CI and the CI workflow actually runs on push",
          web["autoDeployTrigger"] == "checksPass" and re.search(r"(?m)^\s*push:", (ROOT / ".github/workflows/ci.yml").read_text()) is not None)
    check("migrations run in the app at startup, so no preDeployCommand is needed (it cannot reach the disk anyway)", "preDeployCommand" not in web)

    # ---------------- cross-checks against the code ----------------
    sources = "\n".join(p.read_text(errors="ignore") for d in ("api", "lambda_erp", "msme") for p in (ROOT / d).rglob("*.py"))
    docket_dir = None
    try:
        import docket
        docket_dir = Path(docket.__file__).parent
        sources += "\n" + "\n".join(p.read_text(errors="ignore") for p in docket_dir.rglob("*.py"))
    except ImportError:
        pass
    unread = [k for k in by_key if k not in PLATFORM_ENV and not (k.startswith("DOCKET_") and docket_dir is None) and k not in sources]
    check("every environment variable the Blueprint sets is actually read by the code (no silent no-ops)", not unread, str(unread))
    dockerfile = (ROOT / "Dockerfile").read_text()
    check("the container binds to $PORT (Render's default 10000 is set explicitly)", "${PORT}" in dockerfile and by_key["PORT"]["value"] == "10000")
    check("the image starts through the privilege-dropping entrypoint and no longer pins USER app",
          'ENTRYPOINT ["docker-entrypoint.sh"]' in dockerfile and not re.search(r"(?m)^USER\s+app", dockerfile) and (ROOT / "docker-entrypoint.sh").is_file()
          and "chmod 0755 /usr/local/bin/docker-entrypoint.sh" in dockerfile)
    check("the image installs the OCR engine", "tesseract-ocr" in dockerfile and 'pip install --no-cache-dir ".[msme]"' in dockerfile)

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="lambda_render_bp_")
    os.close(fd)
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "", "JWT_SECRET_KEY": "x", "OPENAI_API_KEY": "x"})
    from api.main import app
    check("the health-check path is a real route in the app", web["healthCheckPath"] in app.openapi()["paths"])

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
