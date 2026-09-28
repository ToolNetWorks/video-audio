import fcntl
import os
import pty
import re
import select
import subprocess
import termios
import threading
import time
from urllib.parse import parse_qs, urlparse

COLAB_SESSION = os.getenv("COLAB_SESSION", "subtitle")
COLAB_GPU = os.getenv("COLAB_GPU", "T4")
MOUNT_PATH = "/content/gdrive"

COLAB_NEW_TIMEOUT = 300
SESSION_READY_TIMEOUT = 120
OAUTH_TIMEOUT = 600
MOUNT_TIMEOUT = 180

# Full URL ends at whitespace; drivemount prints it on one (very long) line.
OAUTH_URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/\S+(?=\s)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DRIVEMOUNT_ATTEMPTS = 3

# States: idle, starting_colab, colab_ready, starting_drive_auth,
#         waiting_oauth, mounting, connected, failed
ACTIVE_STATES = {"starting_colab", "colab_ready", "starting_drive_auth", "waiting_oauth", "mounting"}


def _make_pty_controlling_tty():
    # colab drivemount reads "Press Enter" from /dev/tty, not stdin. Under systemd
    # there is no controlling terminal, so make the pty (fd 0) the child's one.
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _run(args, timeout):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as exc:
        return -2, str(exc)


def session_exists():
    code, out = _run(["colab", "sessions"], timeout=30)
    return code == 0 and f"[{COLAB_SESSION}]" in out


def session_ready():
    code, out = _run(["colab", "status", "-s", COLAB_SESSION], timeout=30)
    return code == 0 and f"[{COLAB_SESSION}]" in out and ("Status: IDLE" in out or "Status: BUSY" in out)


def drive_mounted_on_vm():
    script_path = "/tmp/check_drive.py"
    with open(script_path, "w") as f:
        f.write(f"import os; print('DRIVE_OK' if os.path.exists('{MOUNT_PATH}/MyDrive') else 'DRIVE_NO')")
    code, out = _run(["colab", "exec", "-s", COLAB_SESSION, "-f", script_path], timeout=60)
    return "DRIVE_OK" in out


class DriveAuthManager:
    def __init__(self):
        # RLock: helpers may be called while the lock is already held.
        self.lock = threading.RLock()
        self.state = {
            "state": "idle",
            "colab_connected": False,
            "drive_mounted": False,
            "auth_in_progress": False,
            "auth_required": True,
            "oauth_url": None,
            "mount_path": MOUNT_PATH,
            "colab_session": COLAB_SESSION,
            "message": "Google Drive chưa kết nối",
            "error": None,
        }
        self.process = None
        self.master_fd = None
        self.worker = None

    def _update_state(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)
            self.state["auth_in_progress"] = self.state["state"] in ACTIVE_STATES
            self.state["auth_required"] = not self.state["drive_mounted"]

    def _fail(self, error, message=None):
        print(f"[drive_auth] failed: {error}", flush=True)
        self._update_state(
            state="failed",
            oauth_url=None,
            error=error,
            message=message or f"Lỗi: {error}",
        )

    def is_active(self):
        with self.lock:
            return self.state["state"] in ACTIVE_STATES

    def get_status(self):
        with self.lock:
            return dict(self.state)

    def check_status(self):
        """Refresh colab/drive flags from the VM. Never touches an active flow."""
        if self.is_active():
            return self.get_status()
        colab_ok = session_exists()
        drive_ok = colab_ok and drive_mounted_on_vm()
        with self.lock:
            if self.state["state"] in ACTIVE_STATES:
                return dict(self.state)
            if drive_ok:
                self._update_state(state="connected", colab_connected=True, drive_mounted=True,
                                   oauth_url=None, error=None, message="Google Drive đã kết nối")
            else:
                keep_failed = self.state["state"] == "failed"
                self._update_state(
                    state="failed" if keep_failed else "idle",
                    colab_connected=colab_ok,
                    drive_mounted=False,
                    oauth_url=None,
                    message=self.state["message"] if keep_failed else (
                        "Google Drive chưa kết nối" if colab_ok else "Colab session chưa kết nối"),
                )
            return dict(self.state)

    def start_auth(self):
        """Start the background flow and return immediately."""
        with self.lock:
            if self.state["state"] in ACTIVE_STATES:
                return {"ok": True, **self.state}
            self._update_state(state="starting_colab", oauth_url=None, error=None,
                               drive_mounted=False, message="Đang tạo phiên Colab...")
            self.worker = threading.Thread(target=self._run_flow, daemon=True)
            self.worker.start()
            return {"ok": True, **self.state}

    def _run_flow(self):
        try:
            if not self._ensure_colab_session():
                return
            if drive_mounted_on_vm():
                self._update_state(state="connected", drive_mounted=True, oauth_url=None,
                                   message="Google Drive đã kết nối")
                return
            for attempt in range(1, DRIVEMOUNT_ATTEMPTS + 1):
                # drivemount intermittently dies with "Connection was lost"
                # before printing the OAuth URL; that is safe to retry.
                if self._run_drivemount(last_attempt=attempt == DRIVEMOUNT_ATTEMPTS) != "retry":
                    return
                print(f"[drive_auth] drivemount attempt {attempt} lost before OAuth URL, retrying", flush=True)
                time.sleep(3)
        except Exception as exc:
            self._fail(str(exc))

    def _ensure_colab_session(self):
        if session_exists():
            print(f"[drive_auth] session '{COLAB_SESSION}' exists", flush=True)
        else:
            print(f"[drive_auth] creating session '{COLAB_SESSION}' ({COLAB_GPU})", flush=True)
            code, out = _run(["colab", "new", "-s", COLAB_SESSION, "--gpu", COLAB_GPU], timeout=COLAB_NEW_TIMEOUT)
            print(f"[drive_auth] colab new rc={code}: {out.strip()[-300:]}", flush=True)
            if code != 0:
                self._fail(f"colab new thất bại: {out.strip()[-300:]}", "Không thể tạo phiên Colab")
                return False

        deadline = time.time() + SESSION_READY_TIMEOUT
        while time.time() < deadline:
            if session_ready():
                self._update_state(state="colab_ready", colab_connected=True, message="Colab đã sẵn sàng")
                return True
            time.sleep(3)
        self._fail("Phiên Colab không sẵn sàng (timeout)", "Colab không sẵn sàng")
        return False

    def _run_drivemount(self, last_attempt=True):
        self._update_state(state="starting_drive_auth", message="Đang khởi tạo xác thực Google Drive...")
        master, slave = pty.openpty()
        self.master_fd = master
        buf = ""
        try:
            self.process = subprocess.Popen(
                ["colab", "drivemount", "-s", COLAB_SESSION, MOUNT_PATH],
                stdin=slave, stdout=slave, stderr=subprocess.STDOUT, close_fds=True,
                preexec_fn=_make_pty_controlling_tty,
            )
            os.close(slave)
            started = time.time()
            phase = None
            logged = set()
            while True:
                st = self.get_status()["state"]
                if st not in ACTIVE_STATES:
                    return  # cancelled
                if st == "mounting" and phase != "mounting":
                    started = time.time()
                phase = st
                limit = MOUNT_TIMEOUT if st == "mounting" else OAUTH_TIMEOUT
                if time.time() - started > limit:
                    if st == "mounting":
                        break  # verify on the VM below
                    self._fail("Hết thời gian chờ", "Phiên đăng nhập đã hết hạn.")
                    return
                if self.process.poll() is not None:
                    # Drain whatever is left, then decide by checking the VM.
                    buf += self._drain(master)
                    break
                r, _, _ = select.select([master], [], [], 1.0)
                if not r:
                    continue
                try:
                    buf += os.read(master, 8192).decode("utf-8", errors="ignore")
                except OSError:
                    break
                if st == "starting_drive_auth":
                    m = OAUTH_URL_RE.search(buf)
                    if m:
                        url = m.group(0)
                        hint = parse_qs(urlparse(url).query).get("login_hint", [None])[0]
                        print(f"[drive_auth] oauth url captured ({len(url)} chars, account={hint})", flush=True)
                        msg = "Trong vòng 2 phút: bấm Đăng nhập Google Drive, cấp quyền, rồi bấm Tôi đã cấp quyền"
                        if hint:
                            msg += f" (dùng tài khoản {hint})"
                        self._update_state(state="waiting_oauth", oauth_url=url, message=msg)
                for marker in ("Authorizing VM", "Credentials propagated", "Error propagating"):
                    if marker in buf and marker not in logged:
                        logged.add(marker)
                        line = next((l for l in buf.splitlines() if marker in l), marker)
                        print(f"[drive_auth] {ANSI_RE.sub('', line).strip()[:300]}", flush=True)
                if "Mounted at" in buf:
                    break

            buf = ANSI_RE.sub("", buf)
            print(f"[drive_auth] drivemount ended rc={self.process.poll()} tail={buf.strip()[-1500:]!r}", flush=True)
            if not last_attempt and self.get_status()["state"] == "starting_drive_auth" and "Mounted at" not in buf:
                return "retry"
            if drive_mounted_on_vm():
                self._update_state(state="connected", drive_mounted=True, colab_connected=True,
                                   oauth_url=None, error=None, message="Google Drive đã kết nối")
            elif self.is_active():
                self._fail(buf.strip().splitlines()[-1] if buf.strip() else "drivemount kết thúc nhưng Drive chưa mount",
                           "Mount Google Drive thất bại")
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            try:
                os.close(master)
            except OSError:
                pass
            self.master_fd = None

    @staticmethod
    def _drain(fd):
        data = ""
        while True:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                return data
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                return data
            if not chunk:
                return data
            data += chunk.decode("utf-8", errors="ignore")

    def confirm_auth(self):
        with self.lock:
            if self.state["state"] != "waiting_oauth":
                return {"ok": False, "error": f"Không ở trạng thái chờ cấp quyền ({self.state['state']})"}
            if not (self.master_fd and self.process and self.process.poll() is None):
                return {"ok": False, "error": "Process drivemount không hoạt động"}
            try:
                os.write(self.master_fd, b"\n")
            except OSError as exc:
                return {"ok": False, "error": f"Không thể gửi Enter: {exc}"}
            self._update_state(state="mounting", message="Đang mount Google Drive...")
            return {"ok": True, **self.state}

    def cancel_auth(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
            self._update_state(state="idle", oauth_url=None, error=None, message="Đã hủy")
            return {"ok": True, **self.state}


auth_manager = DriveAuthManager()
