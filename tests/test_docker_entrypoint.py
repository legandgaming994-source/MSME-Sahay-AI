#!/usr/bin/env python3
"""docker-entrypoint.sh: ownership repair on a root-owned persistent disk, then privilege drop.

Needs root (to create a throwaway user and root-owned directories). Skips cleanly when not root unless
REQUIRE_ROOT_TESTS=1 (CI runs it with sudo).

Run:  sudo python -m tests.test_docker_entrypoint
"""

import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPT = str(Path(__file__).resolve().parent.parent / "docker-entrypoint.sh")
USER = "msmeapptest"


def run(env_extra, *cmd, as_user=None, timeout=20):
    env = {"PATH": os.environ["PATH"], "APP_USER": USER, **env_extra}
    argv = [SCRIPT, *cmd]
    if as_user:
        argv = ["setpriv", f"--reuid={as_user}", f"--regid={pwd.getpwnam(as_user).pw_gid}", "--init-groups", "--", *argv]
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)


def main() -> int:
    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    if os.geteuid() != 0 or not shutil.which("setpriv"):
        if os.environ.get("REQUIRE_ROOT_TESTS") == "1":
            print("FAIL root and setpriv are required (REQUIRE_ROOT_TESTS=1)")
            return 1
        print("SKIP entrypoint tests (need root and setpriv)")
        return 0

    subprocess.run(["useradd", "-r", "-M", "-s", "/usr/sbin/nologin", USER], capture_output=True)
    uid = pwd.getpwnam(USER).pw_uid
    disk = tempfile.mkdtemp(prefix="fake-render-disk-")        # root-owned, mode 0755, like a freshly mounted Render disk
    os.chmod(disk, 0o755)
    try:
        env = {"MSME_STORAGE_DIR": f"{disk}/msme-documents", "LAMBDA_ERP_UPLOAD_DIR": f"{disk}/chat-uploads",
               "LAMBDA_ERP_DB": "postgresql://u:p@db.internal/x"}
        # sanity: the unprivileged user really cannot write the root-owned disk (the problem being solved)
        denied = subprocess.run(["setpriv", f"--reuid={USER}", "--regid", str(pwd.getpwnam(USER).pw_gid), "--init-groups", "--", "sh", "-c", f"touch {disk}/probe"],
                                capture_output=True, text=True)
        check("premise: an unprivileged process cannot write the root-owned disk", denied.returncode != 0)

        r = run(env, "sh", "-c", 'echo "uid=$(id -u)"; touch "$MSME_STORAGE_DIR/doc" "$LAMBDA_ERP_UPLOAD_DIR/up" && echo writable')
        check("started as root: the app command runs as the unprivileged user, not root", f"uid={uid}" in r.stdout and "uid=0" not in r.stdout, r.stdout + r.stderr)
        check("started as root: the configured data directories are created, owned by the app user and writable", "writable" in r.stdout and r.returncode == 0, r.stdout + r.stderr)
        st = os.stat(f"{disk}/msme-documents")
        check("data directories are private (0700) and owned by the app user", st.st_uid == uid and (st.st_mode & 0o777) == 0o700, oct(st.st_mode))

        os.chown(f"{disk}/msme-documents/doc", 0, 0)           # a file left root-owned by an earlier run
        run(env, "true")
        check("misowned entries from a previous run are repaired on the next start", os.stat(f"{disk}/msme-documents/doc").st_uid == uid)

        sqlite_dir = f"{disk}/sqlite"
        r = run({"LAMBDA_ERP_DB": f"{sqlite_dir}/app.db"}, "sh", "-c", 'touch "$LAMBDA_ERP_DB" && echo ok')
        check("a SQLite file path gets its directory prepared too", "ok" in r.stdout and os.stat(sqlite_dir).st_uid == uid, r.stdout + r.stderr)
        check("a postgres URL is not mistaken for a directory", not any(p.name.startswith("postgresql") for p in Path(disk).iterdir()))

        for bad in ("relative/dir", "/tmp/x/../etc", "/srv/..", "uploads"):
            r = run({"MSME_STORAGE_DIR": bad}, "true")
            check(f"unsafe data directory {bad!r} is refused (exit 64) before anything runs", r.returncode == 64 and "refusing" in r.stderr, r.stderr)

        # already unprivileged: no privilege operations, command runs as-is
        os.chmod(disk, 0o777)
        r = run({"MSME_STORAGE_DIR": f"{disk}/as-user"}, "sh", "-c", 'echo "uid=$(id -u)"', as_user=USER)
        check("started unprivileged: the command simply runs (no chown/setpriv attempted)", f"uid={uid}" in r.stdout and r.returncode == 0, r.stdout + r.stderr)

        # exec semantics: signals reach the application and the exit code is preserved
        p = subprocess.Popen([SCRIPT, "sleep", "30"], env={"PATH": os.environ["PATH"], "APP_USER": USER}, stdout=subprocess.DEVNULL)
        time.sleep(1.0)
        comm = Path(f"/proc/{p.pid}/comm").read_text().strip()
        p.send_signal(signal.SIGTERM)
        code = p.wait(timeout=10)
        check("the entrypoint process BECOMES the application (exec), so SIGTERM reaches it directly", comm == "sleep" and code == -signal.SIGTERM, f"comm={comm} code={code}")
        r = run({}, "sh", "-c", "exit 7")
        check("the application's exit status is passed through", r.returncode == 7, str(r.returncode))
    finally:
        shutil.rmtree(disk, ignore_errors=True)
        subprocess.run(["userdel", USER], capture_output=True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
