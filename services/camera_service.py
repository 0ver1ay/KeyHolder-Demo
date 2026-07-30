from __future__ import annotations

from typing import Optional, Tuple
from io import BytesIO
from kivy.logger import Logger as KivyLogger


class CameraService:
    """Camera grabber based on kivy.core.camera with JPEG encoding via Pillow.

    - Uses Kivy's Core Camera provider to access the device camera.
    - Converts the current texture (RGBA) to JPEG bytes in-memory.
    """

    def __init__(self, device_index: int = 0, *, resolution: Tuple[int, int] = (640, 480), jpeg_quality: int = 80) -> None:
        self._device_index: int = int(device_index)
        self._resolution: Tuple[int, int] = (int(resolution[0]), int(resolution[1]))
        self._jpeg_quality: int = int(jpeg_quality)
        self._camera = None  # lazy-initialized kivy.core.camera.Camera
        self._pil = None  # cached Pillow module

    def open(self) -> bool:
        """Open/start the camera if not already started. Returns True on success."""
        # If already opened, keep it running
        if self._camera is not None:
            try:
                KivyLogger.debug("CameraService: already open (index=%s)" % self._device_index)
            except Exception:
                pass
            return True
        try:
            from kivy.core.camera import Camera as CoreCamera  # type: ignore
            self._camera = CoreCamera(index=self._device_index, resolution=self._resolution, stopped=True)
            try:
                self._camera.start()
            except Exception:
                # fallback: ensure object cleared on failure
                self._camera = None
                try:
                    KivyLogger.error("CameraService: start() failed (index=%s)" % self._device_index)
                except Exception:
                    pass
                return False
            try:
                KivyLogger.info("CameraService: started (index=%s, resolution=%sx%s)" % (self._device_index, self._resolution[0], self._resolution[1]))
            except Exception:
                pass
            return True
        except Exception:
            self._camera = None
            try:
                KivyLogger.exception("CameraService: exception while opening (index=%s)" % self._device_index)
            except Exception:
                pass
            return False

    def is_open(self) -> bool:
        return self._camera is not None

    def read_jpeg_bytes(self) -> Optional[bytes]:
        """Grab the latest frame as JPEG bytes. Returns None if not available."""
        if not self.is_open() and not self.open():
            try:
                KivyLogger.debug("CameraService: camera not open and cannot open")
            except Exception:
                pass
            return None
        try:
            tex = getattr(self._camera, "texture", None)
            if tex is None:
                try:
                    KivyLogger.debug("CameraService: no texture yet")
                except Exception:
                    pass
                return None
            # Acquire raw RGBA pixel buffer
            pixels = getattr(tex, "pixels", None)
            if not pixels:
                try:
                    KivyLogger.debug("CameraService: empty pixels buffer")
                except Exception:
                    pass
                return None
            try:
                size = getattr(tex, "size", None)
                if not size or len(size) != 2:
                    try:
                        KivyLogger.debug("CameraService: bad texture size")
                    except Exception:
                        pass
                    return None
                width, height = int(size[0]), int(size[1])
            except Exception:
                return None
            # Lazy import Pillow
            if self._pil is None:
                try:
                    from PIL import Image as PILImage  # type: ignore
                    self._pil = PILImage
                except Exception:
                    try:
                        KivyLogger.exception("CameraService: Pillow import failed")
                    except Exception:
                        pass
                    return None
            PILImage = self._pil
            try:
                img = PILImage.frombytes("RGBA", (width, height), pixels)
                img = img.convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=self._jpeg_quality, optimize=True)
                return buf.getvalue()
            except Exception:
                try:
                    KivyLogger.exception("CameraService: encode failed")
                except Exception:
                    pass
                return None
        except Exception:
            try:
                KivyLogger.exception("CameraService: unexpected error")
            except Exception:
                pass
            return None

    def close(self) -> None:
        try:
            if self._camera is not None:
                try:
                    self._camera.stop()
                finally:
                    self._camera = None
        except Exception:
            self._camera = None
        try:
            KivyLogger.info("CameraService: camera closed")
        except Exception:
            pass


