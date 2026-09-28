import threading
import subprocess
import time
import os
import pty
import select
import json
from threading import Lock

COLAB_SESSION = os.getenv("COLAB_SESSION", "subtitle")

class DriveAuthManager:
    def __init__(self):
        self.state = {
            "colab_connected": False,
            "drive_mounted": False,
            "auth_required": True,
            "auth_in_progress": False,
            "oauth_url": None,
            "mount_path": "/content/gdrive",
            "message": "Google Drive chưa kết nối",
            "state": "idle",
            "error": None
        }
        self.lock = Lock()
        self.process = None
        self.master_fd = None
        self.auth_thread = None
        self.check_status()

    def _update_state(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                self.state[k] = v

    def get_status(self):
        with self.lock:
            # Maybe quick check if session is still alive
            return dict(self.state)

    def ensure_colab_session(self):
        """Ensure a Colab session exists and is ready, creating one if necessary."""
        # Check if session already exists via colab sessions command
        try:
            res = subprocess.run(["colab", "sessions"], capture_output=True, text=True, timeout=5)
            if f"[{COLAB_SESSION}]" in res.stdout:
                # Session exists, now check if it's ready
                if self._wait_for_session_ready(COLAB_SESSION):
                    return True
        except Exception:
            pass
        
        # Create new Colab session if not exists
        # Using the environment variable as the session name
        colab_session = COLAB_SESSION
        if not colab_session:
            colab_session = "subtitle"
        
        try:
            subprocess.run(["colab", "new", "-s", colab_session, "--gpu", "T4"], 
                          capture_output=True, text=True, timeout=120)
            # Wait for session to be ready after creation
            return self._wait_for_session_ready(colab_session)
        except subprocess.CalledProcessError:
            # Session creation failed
            return False
        except Exception:
            return False

    def _wait_for_session_ready(self, session_name, timeout=90):
        """Wait for a Colab session to be ready."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # Check if session exists
                res = subprocess.run(["colab", "sessions"], capture_output=True, text=True, timeout=5)
                if f"[{session_name}]" in res.stdout:
                    # Check if session is ready (idle or ready state)
                    status_res = subprocess.run(["colab", "status", "-s", session_name], 
                                              capture_output=True, text=True, timeout=5)
                    # Look for indicators that the session is ready
                    # This might need adjustment based on actual colab status output
                    if "idle" in status_res.stdout.lower() or "ready" in status_res.stdout.lower():
                        return True
                    # Also check if we can execute a simple command
                    exec_res = subprocess.run(["colab", "exec", "-s", session_name, "-f", "/tmp/test_ready.py"], 
                                            capture_output=True, text=True, timeout=10)
                    if exec_res.returncode == 0:
                        return True
            except Exception:
                pass
            time.sleep(2)  # Poll every 2 seconds
        return False

    def check_status(self):
        # Check colab sessions
        try:
            res = subprocess.run(["colab", "sessions"], capture_output=True, text=True, timeout=5)
            if f"[{COLAB_SESSION}]" in res.stdout:
                self._update_state(colab_connected=True)
                # Check if drive is mounted
                script_path = "/tmp/check_drive.py"
                with open(script_path, "w") as f:
                    f.write("import os; print('OK' if os.path.exists('/content/gdrive/MyDrive') else 'NO')")
                check = subprocess.run(["colab", "exec", "-s", COLAB_SESSION, "-f", script_path], capture_output=True, text=True, timeout=10)
                if "OK" in check.stdout:
                    # Only update state fields if not in an active auth flow
                    with self.lock:
                        if not self.state["auth_in_progress"]:
                            self._update_state(
                                drive_mounted=True, 
                                auth_required=False, 
                                message="Google Drive đã kết nối", 
                                auth_in_progress=False, 
                                oauth_url=None,
                                state="connected"
                            )
                        else:
                            # Just update the drive_mounted flag
                            self._update_state(drive_mounted=True)
                else:
                    with self.lock:
                        if not self.state["auth_in_progress"]:
                            self._update_state(
                                drive_mounted=False, 
                                auth_required=True, 
                                message="Google Drive chưa kết nối",
                                state="idle"
                            )
                        else:
                            self._update_state(drive_mounted=False)
            else:
                with self.lock:
                    if not self.state["auth_in_progress"]:
                        self._update_state(
                            colab_connected=False, 
                            drive_mounted=False, 
                            auth_required=True, 
                            message="Colab session chưa kết nối",
                            state="idle"
                        )
                    else:
                        self._update_state(colab_connected=False, drive_mounted=False)
        except Exception as e:
            with self.lock:
                if not self.state["auth_in_progress"]:
                    self._update_state(
                        colab_connected=False, 
                        drive_mounted=False, 
                        auth_required=True, 
                        message=str(e),
                        state="failed",
                        error=str(e)
                    )
                else:
                    self._update_state(colab_connected=False, drive_mounted=False, error=str(e))

    def start_auth(self):
        with self.lock:
            if self.state["auth_in_progress"]:
                return self.state
            
            # Ensure Colab session exists
            if not self.ensure_colab_session():
                self._update_state(
                    colab_connected=False,
                    drive_mounted=False,
                    auth_required=True,
                    state="failed",
                    message="Không thể tạo phiên Colab",
                    error="Không thể tạo phiên Colab"
                )
                return self.get_status()
            
            self.state["auth_in_progress"] = True
            self.state["state"] = "starting_colab"
            self.state["oauth_url"] = None
            self.state["message"] = "Đang khởi tạo Colab..."
            self.state["auth_required"] = True
            self.state["drive_mounted"] = False
            self.state["error"] = None
            
        self.auth_thread = threading.Thread(target=self._run_auth_process, daemon=True)
        self.auth_thread.start()
        return self.get_status()
        
    def _run_auth_process(self):
        master, slave = pty.openpty()
        self.master_fd = master
        
        try:
            # Use COLAB_SESSION environment variable instead of hardcoded "subtitle"
            drivemount_cmd = ["colab", "drivemount", "-s", COLAB_SESSION, "/content/gdrive"]
            self.process = subprocess.Popen(
                drivemount_cmd,
                stdin=slave, stdout=slave, stderr=subprocess.STDOUT,
                close_fds=True
            )
            os.close(slave)
            
            # Let the frontend know we're starting the drive auth process
            self._update_state(state="starting_drive_auth")
            
            output = ""
            start_time = time.time()
            
            while True:
                if time.time() - start_time > 600: # 10 min timeout
                    self._update_state(auth_in_progress=False, message="Phiên đăng nhập đã hết hạn.", oauth_url=None, state="failed", error="Timeout")
                    self.cancel_auth()
                    break
                    
                if self.process.poll() is not None:
                    break
                    
                r, _, _ = select.select([master], [], [], 1.0)
                if r:
                    try:
                        chunk = os.read(master, 1024).decode('utf-8', errors='ignore')
                        output += chunk
                        
                        if "https://accounts.google.com/o/oauth2" in chunk:
                            # Collect all lines containing the OAuth URL pattern
                            url_lines = []
                            for line in chunk.split('\n'):
                                if "https://accounts.google.com/o/oauth2" in line:
                                    url_lines.append(line.strip())
                            # Join all collected lines to reconstruct the full URL
                            full_url = "\n".join(url_lines)
                            self._update_state(state="waiting_oauth", oauth_url=full_url, message="Đang chờ bạn cấp quyền Google Drive")
                                
                        if "Press Enter after you have granted access" in chunk:
                            # We found the prompt.
                            # We don't press enter automatically.
                            # But wait, how does the frontend know?
                            # Frontend has "Tôi đã cấp quyền" button which triggers confirm_auth()
                            pass
                            
                        if "Mounted at /content/gdrive" in chunk:
                            self._update_state(state="connected", drive_mounted=True, auth_in_progress=False, auth_required=False, oauth_url=None, message="Google Drive đã kết nối")
                            break
                    except OSError:
                        break
                        
        except Exception as e:
            self._update_state(auth_in_progress=False, message=f"Lỗi: {e}", oauth_url=None, state="failed", error=str(e))
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait()
            try:
                os.close(master)
            except:
                pass
            if not self.state["drive_mounted"] and self.state["auth_in_progress"]:
                self._update_state(auth_in_progress=False, oauth_url=None, state="failed")

    def confirm_auth(self):
        if self.master_fd and self.process and self.process.poll() is None:
            try:
                os.write(self.master_fd, b"\n")
                # Let the frontend know we're now mounting
                self._update_state(state="mounting", message="Đang mount Google Drive...")
                return {"ok": True}
            except:
                return {"ok": False, "error": "Không thể gửi Enter"}
        return {"ok": False, "error": "Process không hoạt động"}
        
    def cancel_auth(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self._update_state(auth_in_progress=False, oauth_url=None, message="Đã hủy", state="idle")
        return {"ok": True}

auth_manager = DriveAuthManager()