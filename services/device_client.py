from __future__ import annotations

import queue
import socket
import struct
import sys
import threading
import time
from typing import Optional, Callable


# Keepalive: detect half-open TCP (common ~15 min idle drop on Linux/NAT).
_KEEPIDLE_SEC = 60
_KEEPINTVL_SEC = 10
_KEEPCNT = 3


class DeviceClient:
    """TCP-клиент к серверу оборудования (device_host/device_port).

    Держит одно постоянное соединение для приёма событий и отправки команд
    через ту же сокет-сессию (без второго connect на каждую команду).

    Поддерживает:
      - USER:<rfid_code>
      - KV OBJECT:RFID_USER ... VALUE:<rfid_code>
      - KV OBJECT:RFID_KEY ... ROWS/COLS/VALUE (для on_slot_status)
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        on_user: Optional[Callable[[str], None]] = None,
        on_key: Optional[Callable[[str], None]] = None,
        on_slot_status: Optional[Callable[[int, int, str], None]] = None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.on_user = on_user
        self.on_key = on_key
        self.on_slot_status = on_slot_status
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._socket: Optional[socket.socket] = None
        self._socket_lock = threading.Lock()
        self._send_queue: queue.Queue[bytes] = queue.Queue()
        # incremental KV frame buffer
        self._kv_parts: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        with self._socket_lock:
            return self._socket is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="DeviceClient", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_socket()

    def send_payload(self, payload: bytes) -> bool:
        """Поставить данные в очередь отправки по постоянному соединению."""
        if self._stop.is_set() or not payload:
            return False
        try:
            self._send_queue.put_nowait(payload)
            return True
        except Exception:
            return False

    def send_kv(self, pairs: dict) -> bool:
        try:
            lines = [f"{str(k)}:{str(v)}" for k, v in pairs.items()]
            payload = ("\r\n".join(lines) + "\r\n").encode("utf-8")
            return self.send_payload(payload)
        except Exception:
            return False

    def _close_socket(self) -> None:
        with self._socket_lock:
            sock = self._socket
            self._socket = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    @staticmethod
    def _enable_keepalive(sock: socket.socket) -> None:
        """Enable OS TCP keepalive so dead peers are detected within ~2 min."""
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception as e:
            print(f"[DEVICE] SO_KEEPALIVE failed: {e}", flush=True)
            return
        if sys.platform.startswith("linux"):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _KEEPIDLE_SEC)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _KEEPINTVL_SEC)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPCNT)
                print(
                    f"[DEVICE] TCP keepalive on "
                    f"(idle={_KEEPIDLE_SEC}s intvl={_KEEPINTVL_SEC}s cnt={_KEEPCNT})",
                    flush=True,
                )
            except Exception as e:
                print(f"[DEVICE] TCP keepalive tune failed: {e}", flush=True)
        elif sys.platform == "win32":
            # onoff, keepalivetime(ms), keepaliveinterval(ms)
            try:
                sock.ioctl(
                    socket.SIO_KEEPALIVE_VALS,
                    struct.pack("III", 1, _KEEPIDLE_SEC * 1000, _KEEPINTVL_SEC * 1000),
                )
                print(
                    f"[DEVICE] TCP keepalive on "
                    f"(idle={_KEEPIDLE_SEC}s intvl={_KEEPINTVL_SEC}s)",
                    flush=True,
                )
            except Exception as e:
                print(f"[DEVICE] TCP keepalive tune failed: {e}", flush=True)

    def _flush_send_queue(self, sock: socket.socket) -> bool:
        """Send queued payloads. Returns False if the socket is broken."""
        while True:
            try:
                payload = self._send_queue.get_nowait()
            except queue.Empty:
                return True
            try:
                sock.sendall(payload)
                try:
                    preview = payload.decode("utf-8", errors="ignore").strip().replace("\r", " ")
                    print(f"[DEVICE] TX: {preview}", flush=True)
                except Exception:
                    pass
            except Exception as e:
                print(f"[DEVICE] TX failed, will reconnect: {e}", flush=True)
                return False

    def _run(self) -> None:
        while not self._stop.is_set():
            sock: socket.socket | None = None
            try:
                try:
                    print(f"[DEVICE] Connecting to {self.host}:{self.port} ...", flush=True)
                except Exception:
                    pass
                sock = socket.create_connection((self.host, self.port), timeout=2.0)
                self._enable_keepalive(sock)
                with self._socket_lock:
                    self._socket = sock
                try:
                    print("[DEVICE] Connected", flush=True)
                except Exception:
                    pass
                sock.settimeout(0.25)
                buf = b""
                while not self._stop.is_set():
                    if not self._flush_send_queue(sock):
                        break
                    if self._stop.is_set():
                        break
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError as e:
                        print(f"[DEVICE] RX error, will reconnect: {e}", flush=True)
                        break
                    if not chunk:
                        print("[DEVICE] Peer closed connection", flush=True)
                        break
                    buf += chunk
                    try:
                        text = buf.decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    lines = text.replace("\r", "\n").split("\n")
                    buf = (
                        lines.pop().encode("utf-8")
                        if lines and text and not text.endswith(("\r", "\n"))
                        else b""
                    )
                    for line in lines:
                        l = (line or "").strip()
                        if not l:
                            continue
                        try:
                            print(f"[DEVICE] RX: {l}", flush=True)
                        except Exception:
                            pass
                        self._dispatch_line(l)
            except Exception as e:
                if not self._stop.is_set():
                    print(f"[DEVICE] Session error: {e}", flush=True)
            finally:
                with self._socket_lock:
                    if self._socket is sock:
                        self._socket = None
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                try:
                    time.sleep(1.0)
                except Exception:
                    pass

    def _dispatch_line(self, line: str) -> None:
        up = line.upper()
        if up.startswith("USER:"):
            code = line.split(":", 1)[1].strip()
            if code and self.on_user:
                try:
                    self.on_user(code)
                except Exception:
                    pass
            return

        if up.startswith("KEY:"):
            code = line.split(":", 1)[1].strip()
            if code and self.on_key:
                try:
                    self.on_key(code)
                except Exception:
                    pass
            return

        _kv_cmds = ("GET_STATUS", "GET_STATE", "SWI_STATUS", "SET_STATE", "SCANNED", "STATUS")
        try:
            if up.startswith("OBJECT:"):
                self._kv_parts = {"OBJECT": line.split(":", 1)[1].strip()}
                return
            if ":" in line and self._kv_parts:
                k, v = line.split(":", 1)
                k = k.strip().upper()
                v = v.strip()
                if k in ("COMMAND", "ROWS", "COLS", "VALUE"):
                    self._kv_parts[k] = v
                obj = self._kv_parts.get("OBJECT")
                cmd = (self._kv_parts.get("COMMAND") or "").upper()
                if obj == "RFID_USER" and cmd in _kv_cmds and "VALUE" in self._kv_parts:
                    code = (self._kv_parts.get("VALUE", "") or "").strip()
                    if code and self.on_user:
                        try:
                            self.on_user(code)
                        except Exception:
                            pass
                    self._kv_parts = {}
                    return
                if obj == "RFID_KEY" and cmd in _kv_cmds and "VALUE" in self._kv_parts:
                    rx = self._kv_parts.get("ROWS")
                    ry = self._kv_parts.get("COLS")
                    val = self._kv_parts.get("VALUE", "")
                    if rx is not None and ry is not None and self.on_slot_status:
                        try:
                            self.on_slot_status(int(rx), int(ry), val)
                        except Exception:
                            pass
                    elif val and self.on_key:
                        try:
                            self.on_key(val.strip())
                        except Exception:
                            pass
                    self._kv_parts = {}
                    return
        except Exception:
            self._kv_parts = {}


def stop_device_client(app) -> None:
    client = getattr(app, "_device_client", None)
    if client is None:
        return
    try:
        client.stop()
    except Exception:
        pass
    thread = getattr(client, "_thread", None)
    if thread is not None and thread.is_alive():
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass
    app._device_client = None


def start_device_client(
    app,
    host: str,
    port: int,
    *,
    on_user: Optional[Callable[[str], None]] = None,
    on_key: Optional[Callable[[str], None]] = None,
    on_slot_status: Optional[Callable[[int, int, str], None]] = None,
) -> DeviceClient | None:
    """Остановить предыдущий клиент (если был) и поднять один экземпляр."""
    host = str(host or "").strip()
    if not host:
        return None
    stop_device_client(app)
    client = DeviceClient(
        host=host,
        port=int(port),
        on_user=on_user,
        on_key=on_key,
        on_slot_status=on_slot_status,
    )
    app._device_client = client
    client.start()
    return client
