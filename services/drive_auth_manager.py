import threading
import subprocess
import time
import os
import pty
import select
import json
from threading import Lock

class DriveAuthManager:
    def __init__(self):
        self.state = {
            "colab_connected": False,
            "drive_mounted": False,
            "auth_required": True,
            "auth_in_progress": False,
            "oauth_url": None,
            "mount_path": "/content/gdrive",
            "message": "Google Drive chưa kết nối"
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

    def check_status(self):
        # Check colab sessions
        try:
            res = subprocess.run(["colab", "sessions"], capture_output=True, text=True, timeout=5)
            if "[subtitle]" in res.stdout:
                self._update_state(colab_connected=True)
                # Check if drive is mounted
                script_path = "/tmp/check_drive.py"
                with open(script_path, "w") as f:
                    f.write("import os; print('OK' if os.path.exists('/content/gdrive/MyDrive') else 'NO')")
                check = subprocess.run(["colab", "exec", "-s", "subtitle", "-f", script_path], capture_output=True, text=True, timeout=10)
                if "OK" in check.stdout:
                    self._update_state(drive_mounted=True, auth_required=False, message="Google Drive đã kết nối", auth_in_progress=False, oauth_url=None)
                else:
                    self._update_state(drive_mounted=False, auth_required=True, message="Google Drive chưa kết nối")
            else:
                self._update_state(colab_connected=False, drive_mounted=False, auth_required=True, message="Colab session chưa kết nối")
        except Exception as e:
            self._update_state(colab_connected=False, drive_mounted=False, auth_required=True, message=str(e))

    def start_auth(self):
        with self.lock:
            if self.state["auth_in_progress"]:
                return self.state
            
            self.state["auth_in_progress"] = True
            self.state["oauth_url"] = None
            self.state["message"] = "Đang khởi tạo Drive..."
            self.state["auth_required"] = True
            self.state["drive_mounted"] = False
        
        self.auth_thread = threading.Thread(target=self._run_auth_process, daemon=True)
        self.auth_thread.start()
        return self.get_status()
        
    def _run_auth_process(self):
        master, slave = pty.openpty()
        self.master_fd = master
        
        try:
            self.process = subprocess.Popen(
                ["colab", "drivemount", "-s", "subtitle", "/content/gdrive"],
                stdin=slave, stdout=slave, stderr=subprocess.STDOUT,
                close_fds=True
            )
            os.close(slave)
            
            output = ""
            start_time = time.time()
            
            while True:
                if time.time() - start_time > 600: # 10 min timeout
                    self._update_state(auth_in_progress=False, message="Phiên đăng nhập đã hết hạn.", oauth_url=None)
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
                            lines = output.split('\n')
                            for line in lines:
                                if "https://accounts.google.com/o/oauth2" in line:
                                    self._update_state(oauth_url=line.strip(), message="Đang chờ bạn cấp quyền Google Drive")
                                    
                        if "Press Enter after you have granted access" in chunk:
                            # We found the prompt.
                            # We don't press enter automatically.
                            # But wait, how does the frontend know?
                            # Frontend has "Tôi đã cấp quyền" button which triggers confirm_auth()
                            pass
                            
                        if "Mounted at /content/gdrive" in chunk:
                            self._update_state(drive_mounted=True, auth_in_progress=False, auth_required=False, oauth_url=None, message="Google Drive đã kết nối")
                            break
                    except OSError:
                        break
                        
        except Exception as e:
            self._update_state(auth_in_progress=False, message=f"Lỗi: {e}", oauth_url=None)
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait()
            try:
                os.close(master)
            except:
                pass
            if not self.state["drive_mounted"]:
                self._update_state(auth_in_progress=False, oauth_url=None)

    def confirm_auth(self):
        if self.master_fd and self.process and self.process.poll() is None:
            try:
                os.write(self.master_fd, b"\n")
                return {"ok": True}
            except:
                return {"ok": False, "error": "Không thể gửi Enter"}
        return {"ok": False, "error": "Process không hoạt động"}
        
    def cancel_auth(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self._update_state(auth_in_progress=False, oauth_url=None, message="Đã hủy")
        return {"ok": True}

auth_manager = DriveAuthManager()
