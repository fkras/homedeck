''' HID access to the D200: frames in, frames out, errors raised.

Synchronous on purpose. UlanziD200 runs it on its own threads so a page
upload - a few hundred reports - never blocks the event loop.
'''
from __future__ import annotations

import threading
from typing import Iterable, List, Optional, Union

from .protocol import CommandCode, Frame, decode_report, encode_frame

D200_VENDOR_ID = 0x2207
D200_PRODUCT_ID = 0x0019

READ_SIZE = 1024
# Same read timeout as the official reader thread
READ_TIMEOUT_MS = 100


class TransportError(IOError):
    ''' The deck refused a read or write, or went away. '''


class DeviceNotFound(TransportError):
    pass


def _hid():
    try:
        import hid
    except ImportError as e:      # pragma: no cover - depends on the system
        raise TransportError('hidapi is not installed (pip install hidapi)') from e

    if not hasattr(hid, 'device'):  # pragma: no cover
        raise TransportError("the installed 'hid' module is not hidapi (pip uninstall hid; pip install hidapi)")

    return hid


class HidTransport:
    # How long close() waits for a read or write in flight before giving up
    # on freeing the handle (leaking it beats freeing it under hidapi's feet)
    CLOSE_TIMEOUT = 10

    def __init__(self, handle, *, info: Optional[dict] = None):
        self._handle = handle
        self.info = info or {}
        # hid_close() frees the device; it must never run while another thread
        # is inside hid_write() or hid_read_timeout() on it
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._aborted = threading.Event()

    @staticmethod
    def enumerate(vendor_id: int = D200_VENDOR_ID, product_id: int = D200_PRODUCT_ID) -> List[dict]:
        return list(_hid().enumerate(vendor_id, product_id))

    @classmethod
    def open(cls, vendor_id: int = D200_VENDOR_ID, product_id: int = D200_PRODUCT_ID, *,
             path: Optional[Union[str, bytes]] = None) -> 'HidTransport':
        hid = _hid()
        info = None
        if path is None:
            found = cls.enumerate(vendor_id, product_id)
            if not found:
                raise DeviceNotFound(f'no device {vendor_id:04x}:{product_id:04x}')
            info = min(found, key=cls._interface_rank)
            path = info['path']

        if isinstance(path, str):
            path = path.encode()

        handle = hid.device()
        try:
            handle.open_path(path)
        except (OSError, ValueError) as e:
            raise TransportError(f'could not open {path!r}: {e}') from e

        return cls(handle, info=info or {'path': path})

    @staticmethod
    def _interface_rank(info: dict):
        ''' The deck has two HID interfaces: 0, the vendor one ("ulanzi",
        usage page 0x0C) that carries the protocol, and 1, a keyboard ("HID
        Interface", usage page 0x01). Enumeration order differs by OS, so
        taking the first match - as hid_open() and strmdck did - can land on
        the keyboard. '''
        is_keyboard = info.get('usage_page') == 0x01 or info.get('interface_number') == 1
        return (is_keyboard, info.get('interface_number', 0))

    @property
    def closed(self) -> bool:
        return self._handle is None

    def abort(self):
        ''' Stop a frame being written at the next report, ahead of close(). '''
        self._aborted.set()

    def close(self):
        self.abort()
        got_write = self._write_lock.acquire(timeout=self.CLOSE_TIMEOUT)
        got_read = self._read_lock.acquire(timeout=self.CLOSE_TIMEOUT)
        try:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            if not (got_write and got_read):
                print('⚠️ D200 still busy after close timeout; leaving the handle open')
                return

            try:
                handle.close()
            except Exception:
                pass
        finally:
            if got_read:
                self._read_lock.release()
            if got_write:
                self._write_lock.release()

    def write_reports(self, reports: Iterable[bytes]):
        ''' Write prepared reports back to back, never interleaved with another
        frame. Raises TransportError on the first short or failed write. '''
        with self._write_lock:
            for report in reports:
                handle = self._handle
                if handle is None or self._aborted.is_set():
                    raise TransportError('device is closed')

                try:
                    written = handle.write(report)
                except (OSError, ValueError) as e:
                    raise TransportError(f'write failed: {e}') from e

                # cython-hidapi returns -1 rather than raising
                if written is None or written < 0:
                    raise TransportError(f'write failed: {self._error(handle)}')

    def send(self, protocol: int, payload: Union[bytes, str] = b'', *, command_code: int = CommandCode.REQUEST):
        self.write_reports(encode_frame(protocol, payload, command_code=command_code))

    def read_frame(self, timeout_ms: int = READ_TIMEOUT_MS) -> Optional[Frame]:
        ''' The next frame, or None if nothing arrived within `timeout_ms`. '''
        with self._read_lock:
            handle = self._handle
            if handle is None or self._aborted.is_set():
                raise TransportError('device is closed')

            try:
                data = handle.read(READ_SIZE, timeout_ms)
            except (OSError, ValueError) as e:
                raise TransportError(f'read failed: {e}') from e

        if not data:
            return None

        return decode_report(data)

    def probe(self) -> bool:
        ''' Whether the device still answers, without drawing anything. '''
        handle = self._handle
        if handle is None:
            return False

        try:
            handle.get_product_string()
        except Exception:
            return False

        return True

    @staticmethod
    def _error(handle) -> str:
        try:
            return handle.error() or 'unknown error'
        except Exception:
            return 'unknown error'
