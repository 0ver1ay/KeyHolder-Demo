from __future__ import annotations

import os
import socket
import sys
import tkinter as tk
from configparser import ConfigParser
from tkinter import ttk


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5200
DEFAULT_FEEDBACK_PORT = 7778


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def load_rfid_defaults() -> tuple[str, int]:
    """Читает host/port из config.cfg (как UserApp)."""
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    cfg_path = os.path.join(_project_root(), "config.cfg")
    if not os.path.isfile(cfg_path):
        return host, port
    try:
        parser = ConfigParser()
        parser.read(cfg_path, encoding="utf-8")
        if parser.has_section("rfid"):
            host = parser.get("rfid", "host", fallback=host).strip() or host
            port = int(parser.get("rfid", "port", fallback=str(port)).strip())
    except Exception:
        pass
    return host, port


class _FeedbackServer:
    def __init__(self, host: str, port: int, on_line) -> None:
        import threading
        self._host = host
        self._port = int(port)
        self._on_line = on_line
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()
        self._thr = __import__("threading").Thread(target=self._serve, daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
        finally:
            self._sock = None

    def _serve(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self._host, self._port))
            s.listen(8)
            s.settimeout(0.5)
            self._sock = s
        except Exception:
            self._sock = None
            return
        while not self._stop.is_set():
            try:
                try:
                    conn, _ = s.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        data = conn.recv(1024)
                        if not data:
                            continue
                        line = data.decode("utf-8", errors="ignore").strip()
                        try:
                            self._on_line(line)
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception:
                continue
        try:
            s.close()
        except Exception:
            pass


def send_line(host: str, port: int, line: str) -> tuple[bool, str]:
    """Отправить одну строку на TCP-приёмник UserApp. Возвращает (ok, сообщение)."""
    host = (host or DEFAULT_HOST).strip()
    try:
        port = int(port)
    except Exception:
        return False, f"Некорректный порт: {port!r}"
    payload = (line.strip() + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=2.0) as s:
            s.sendall(payload)
        return True, f"OK -> {host}:{port}  {line.strip()}"
    except ConnectionRefusedError:
        return False, f"ОТКАЗ: {host}:{port} — UserApp не слушает (запустите main_user.py?)"
    except TimeoutError:
        return False, f"ТАЙМАУТ: {host}:{port}"
    except OSError as exc:
        return False, f"ОШИБКА: {host}:{port} — {exc}"
    except Exception as exc:
        return False, f"ОШИБКА: {exc}"


def probe_port(host: str, port: int) -> tuple[bool, str]:
    host = (host or DEFAULT_HOST).strip()
    try:
        port = int(port)
    except Exception:
        return False, f"Некорректный порт: {port!r}"
    try:
        with socket.create_connection((host, port), timeout=1.5):
            pass
        return True, f"Порт {host}:{port} открыт — можно отправлять USER:/KEY:"
    except ConnectionRefusedError:
        return False, f"Порт {host}:{port} закрыт — запустите UserApp (main_user.py)"
    except Exception as exc:
        return False, f"Порт {host}:{port}: {exc}"


class RfidSimulatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RFID Simulator — KeyHolder")
        self.geometry("720x580")

        default_host, default_port = load_rfid_defaults()
        self.host_var = tk.StringVar(value=default_host)
        self.port_var = tk.StringVar(value=str(default_port))

        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        conn_row = ttk.Frame(frm)
        conn_row.pack(fill=tk.X, pady=4)
        ttk.Label(conn_row, text="Host:").pack(side=tk.LEFT)
        ttk.Entry(conn_row, width=16, textvariable=self.host_var).pack(side=tk.LEFT, padx=6)
        ttk.Label(conn_row, text="Port:").pack(side=tk.LEFT)
        ttk.Entry(conn_row, width=8, textvariable=self.port_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(conn_row, text="Проверить", command=self.test_connection).pack(side=tk.LEFT, padx=8)

        self.status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.status_var, wraplength=680).pack(fill=tk.X, pady=4)

        hint = (
            "Отправка: USER:<rfid> и KEY:<rfid> на порт из config.cfg [rfid] "
            f"(сейчас {default_host}:{default_port}). UserApp должен быть запущен."
        )
        ttk.Label(frm, text=hint, wraplength=680).pack(fill=tk.X, pady=(0, 6))

        users_row = ttk.LabelFrame(frm, text="Users (seed: user1 / user2)")
        users_row.pack(fill=tk.X, pady=8)
        users = {
            "user1": "1111",
            "user2": "2222",
        }
        for name, code in users.items():
            ttk.Button(
                users_row,
                text=f"{name} ({code})",
                command=lambda c=code: self.send_user(c),
            ).pack(side=tk.LEFT, padx=4, pady=4)

        keys_row = ttk.LabelFrame(frm, text="Keys (seed: K-001 … K-003)")
        keys_row.pack(fill=tk.X, pady=8)
        keys = {
            "K-001": "001",
            "K-002": "002",
            "K-003": "003",
        }
        for name, code in keys.items():
            ttk.Button(
                keys_row,
                text=f"{name} ({code})",
                command=lambda c=code: self.send_key(c),
            ).pack(side=tk.LEFT, padx=4, pady=4)

        lock_row = ttk.LabelFrame(frm, text="Cabinet Lock")
        lock_row.pack(fill=tk.X, pady=8)
        ttk.Button(lock_row, text="Открыть общий замок", command=self.send_lock_open).pack(
            side=tk.LEFT, padx=4, pady=4
        )

        manual = ttk.LabelFrame(frm, text="Manual")
        manual.pack(fill=tk.X, pady=8)
        self.input_var = tk.StringVar(value="")
        ttk.Entry(manual, textvariable=self.input_var, width=24).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(manual, text="Send USER", command=self.send_user_manual).pack(side=tk.LEFT, padx=4)
        ttk.Button(manual, text="Send KEY", command=self.send_key_manual).pack(side=tk.LEFT, padx=4)

        fb_row = ttk.LabelFrame(frm, text="Feedback listener (app → simulator)")
        fb_row.pack(fill=tk.X, pady=8)
        self.fb_port_var = tk.StringVar(value=str(DEFAULT_FEEDBACK_PORT))
        ttk.Label(fb_row, text="Port:").pack(side=tk.LEFT)
        ttk.Entry(fb_row, width=8, textvariable=self.fb_port_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(fb_row, text="Start", command=self.start_feedback).pack(side=tk.LEFT, padx=4)
        ttk.Button(fb_row, text="Stop", command=self.stop_feedback).pack(side=tk.LEFT, padx=4)

        log_frame = ttk.LabelFrame(frm, text="Log (отправка и feedback)")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=8)
        self.log_text = tk.Text(log_frame, height=14)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=sb.set)

        self._fb_server: _FeedbackServer | None = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.start_feedback()
        except Exception:
            pass
        self.after(300, self.test_connection)

    def _get_conn(self) -> tuple[str, int]:
        host = self.host_var.get().strip() or DEFAULT_HOST
        try:
            port = int(self.port_var.get().strip())
        except Exception:
            port = DEFAULT_PORT
        return host, port

    def _log(self, line: str) -> None:
        try:
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
        except Exception:
            pass

    def _send(self, line: str) -> None:
        host, port = self._get_conn()
        ok, msg = send_line(host, port, line)
        prefix = "TX" if ok else "ERR"
        self._log(f"[{prefix}] {msg}")
        self.status_var.set(msg)

    def test_connection(self) -> None:
        host, port = self._get_conn()
        ok, msg = probe_port(host, port)
        self.status_var.set(msg)
        self._log(f"[{'OK' if ok else 'ERR'}] {msg}")

    def send_user(self, code: str) -> None:
        self._send(f"USER:{code}")

    def send_key(self, code: str) -> None:
        self._send(f"KEY:{code}")

    def send_lock_open(self) -> None:
        self._send("LOCK:OPEN")

    def send_user_manual(self) -> None:
        code = self.input_var.get().strip()
        if code:
            self.send_user(code)

    def send_key_manual(self) -> None:
        code = self.input_var.get().strip()
        if code:
            self.send_key(code)

    def start_feedback(self) -> None:
        try:
            port = int(self.fb_port_var.get().strip() or str(DEFAULT_FEEDBACK_PORT))
        except Exception:
            port = DEFAULT_FEEDBACK_PORT
        if self._fb_server is not None:
            self._fb_server.stop()
        self._fb_server = _FeedbackServer(DEFAULT_HOST, port, self._append_feedback_line)
        self._fb_server.start()
        self._log(f"[FB] listening on {DEFAULT_HOST}:{port}")

    def stop_feedback(self) -> None:
        if self._fb_server is not None:
            self._fb_server.stop()
            self._log("[FB] stopped")

    def _append_feedback_line(self, line: str) -> None:
        try:
            self.after(0, lambda: self._log(f"[RX] {line}"))
        except Exception:
            pass

    def _on_close(self) -> None:
        try:
            self.stop_feedback()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = RfidSimulatorApp()
    app.mainloop()
