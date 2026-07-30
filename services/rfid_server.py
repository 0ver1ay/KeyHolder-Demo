from __future__ import annotations

import socket
import threading
from typing import Callable, Optional


class RfidServer:
    """Простой TCP-сервер для приёма строк вида:

    - USER:<rfid_code>
    - KEY:<rfid_code>
    - LOCK:OPEN / LOCK:CLOSE — событие открытия/закрытия общего замка

    По одному сообщению на соединение. После получения — соединение закрывается.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7777,
        *,
        mode: str = "both",
        on_user: Optional[Callable[[str], None]] = None,
        on_key: Optional[Callable[[str], None]] = None,
        on_lock_open: Optional[Callable[[], None]] = None,
        on_lock_close: Optional[Callable[[], None]] = None,
        on_slot_status: Optional[Callable[[int, int, str], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.mode = (mode or "both").lower()
        self.on_user = on_user
        self.on_key = on_key
        self.on_lock_open = on_lock_open
        self.on_lock_close = on_lock_close
        # KV responses: OBJECT:RFID_KEY COMMAND:GET_STATUS ROWS:x COLS:y VALUE:...
        self.on_slot_status = on_slot_status

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._serve, name="RfidServer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._sock.close()
                except Exception:
                    pass
        finally:
            self._sock = None

    def _serve(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(8)
            sock.settimeout(0.5)
            self._sock = sock
            try:
                print(f"[RFID TCP] Listening on {self.host}:{self.port} mode={self.mode}")
            except Exception:
                pass
        except Exception:
            self._sock = None
            return

        while not self._stop_event.is_set():
            try:
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                with conn:
                    try:
                        conn.settimeout(2.0)
                        data = conn.recv(4096)
                        if not data:
                            continue
                        line = data.decode("utf-8", errors="ignore").strip()
                        try:
                            print(f"[RFID TCP] RX: {line}")
                        except Exception:
                            pass
                        self._handle_line(line)
                    except Exception:
                        pass
            except Exception:
                # Continue serving despite individual errors
                continue

        try:
            sock.close()
        except Exception:
            pass

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        l = line.strip()
        up = l.upper()

        if self.mode in ("both", "user") and up.startswith("USER:"):
            code = l.split(":", 1)[1].strip()
            if code and self.on_user:
                try:
                    try:
                        print(f"[RFID TCP] Dispatch USER code={code}")
                    except Exception:
                        pass
                    self.on_user(code)
                except Exception:
                    pass
            return

        if self.mode in ("both", "key") and up.startswith("KEY:"):
            code = l.split(":", 1)[1].strip()
            if code and self.on_key:
                try:
                    try:
                        print(f"[RFID TCP] Dispatch KEY code={code}")
                    except Exception:
                        pass
                    self.on_key(code)
                except Exception:
                    pass
            return

        # События замка: LOCK:OPEN / LOCK:CLOSE
        if up == "LOCK:OPEN":
            if self.on_lock_open:
                try:
                    try:
                        print("[RFID TCP] Dispatch LOCK:OPEN")
                    except Exception:
                        pass
                    self.on_lock_open()
                except Exception:
                    pass
            return

        if up == "LOCK:CLOSE":
            if self.on_lock_close:
                try:
                    try:
                        print("[RFID TCP] Dispatch LOCK:CLOSE")
                    except Exception:
                        pass
                    self.on_lock_close()
                except Exception:
                    pass
            return

        # KV message parsing (multi-line in a single TCP payload)
        # Expected lines like:
        # OBJECT:RFID_KEY\r\nCOMMAND:GET_STATUS\r\nROWS:2\r\nCOLS:3\r\nVALUE:...\r\n
        try:
            parts = {}
            # support both CRLF and LF
            for raw in l.replace("\r", "\n").split("\n"):
                if not raw:
                    continue
                if ":" not in raw:
                    continue
                k, v = raw.split(":", 1)
                parts[k.strip().upper()] = v.strip()
            obj = parts.get("OBJECT")
            cmd = parts.get("COMMAND")
            # OBJECT:RFID_USER — map VALUE to on_user
            if obj == "RFID_USER" and cmd in ("GET_STATUS", "GET_STATE", "SWI_STATUS", "SET_STATE", "SCANNED", "STATUS"):
                code = (parts.get("VALUE", "") or "").strip()
                if code and self.on_user:
                    try:
                        print(f"[RFID TCP] Dispatch RFID_USER value={code}")
                    except Exception:
                        pass
                    try:
                        self.on_user(code)
                    except Exception:
                        pass
                return
            if obj == "RFID_KEY" and cmd in ("GET_STATUS", "GET_STATE", "SWI_STATUS", "SET_STATE", "SCANNED", "STATUS"):
                x = parts.get("ROWS")
                y = parts.get("COLS")
                val = parts.get("VALUE", "")
                if x is not None and y is not None and self.on_slot_status:
                    try:
                        self.on_slot_status(int(x), int(y), val)
                    except Exception:
                        pass
                elif val and self.on_key:
                    try:
                        self.on_key(val.strip())
                    except Exception:
                        pass
                return
        except Exception:
            pass

        # Fallback: если префикс не указан, маршрутизируем согласно mode
        if self.mode == "user":
            if self.on_user:
                try:
                    self.on_user(l)
                except Exception:
                    pass
        elif self.mode == "key":
            if self.on_key:
                try:
                    self.on_key(l)
                except Exception:
                    pass
        else:
            # both: по умолчанию считаем это USER-кодом
            if self.on_user:
                try:
                    self.on_user(l)
                except Exception:
                    pass


