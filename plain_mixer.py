#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DM3 (Yamaha RCP) <-> Razer Wireless Control Pod
- Robust reconnects (generation IDs, queue purge, reader join, single op retry)
- No coroutines; single-threaded main loop + one reader thread for the mixer
- Debug prints everything TX/RX by default
"""

import socket
import threading
import time
import re
from queue import Queue, Empty
from typing import Callable, Optional, Deque, List
from collections import deque

# ===================== Mixer Client =====================

class MixerError(Exception):
    pass

class YamahaDM3Mixer:
    """
    TCP client for Yamaha DM3 (RCP-like) mixer.

    Commands:
      - GET Left fader: "get MIXER:Current/St/Fader/Level 0 0\\n"
      - SET Left fader: "set MIXER:Current/St/Fader/Level 0 0 {level}\\n"
        level is int (dB * 100) in [-13800, 1000].

    Replies:
      - OK <echo...> [level ["-6.00"]] or ERROR <text>
      - NOTIFY set MIXER:Current/St/Fader/Level <ch> 0 <level> ["-6.00"]
        We only act on ch==0 (Left). Ignore ch==1 (Right).
    """
    MIN_LEVEL = -13800
    MAX_LEVEL = 1000

    _OK_RE = re.compile(
        r'^OK\s+(get|set)\s+MIXER:Current/St/Fader/Level\s+(\d+)\s+\d+(?:\s+(-?\d+)(?:\s+"[^"]*")?)?\s*$'
    )
    _NOTIFY_RE = re.compile(
        r'^NOTIFY\s+set\s+MIXER:Current/St/Fader/Level\s+(\d+)\s+\d+\s+(-?\d+)(?:\s+"[^"]*")?\s*$'
    )
    _ERROR_RE = re.compile(r'^ERROR\b(.*)$')

    def __init__(
        self,
        host: str,
        port: int,
        *,
        debug: bool = True,
        on_volume_changed: Optional[Callable[[int], None]] = None,
        on_connected: Optional[Callable[[], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        connect_timeout: float = 0.5
    ):
        self.host = host
        self.port = port
        self.debug = debug

        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._resp_q: Queue = Queue()
        self._cmd_lock = threading.Lock()
        self._buf = b""
        self._connected = False
        self._connect_timeout = connect_timeout

        self._current_volume: Optional[int] = None  # cached last-known
        self._on_volume_changed = on_volume_changed
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        # Robustness: connection generation to ignore stale messages
        self._conn_id = 0
        self._reader_join_timeout = 0.3

    # ---- Public API ----

    def connect(self) -> bool:
        """Establish a fresh connection and reader; clears old state. Also queries and caches volume."""
        self._stop_reader_sync()  # stop any existing reader, purge old state
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
            s.settimeout(self._connect_timeout)
            s.connect((self.host, self.port))
            s.settimeout(None)

            with self._cmd_lock:
                self._sock = s
                self._buf = b""
                self._clear_queue(self._resp_q)
                self._stop_evt.clear()
                self._conn_id += 1
                cid = self._conn_id

                self._reader = threading.Thread(
                    target=self._reader_loop, args=(cid,), name=f"MixerReader#{cid}", daemon=True
                )
                self._reader.start()
                self._connected = True

            if self.debug:
                print(f"[mixer] Connected to {self.host}:{self.port} (gen {self._conn_id})")
            if self._on_connected:
                self._on_connected()

            # Immediately query cached volume
            try:
                self.get_volume(timeout=1.0)
            except Exception as e:
                if self.debug:
                    print(f"[mixer] Warning: initial get_volume failed: {e}")

            return True
        except Exception as e:
            if self.debug:
                print(f"[mixer] Connect failed: {e}")
            self._connected = False
            self._sock = None
            return False

    def disconnect(self):
        """Close connection and stop reader."""
        self._stop_reader_sync()
        if self.debug:
            print("[mixer] Disconnected")
        if self._on_disconnected:
            self._on_disconnected()

    def is_connected(self) -> bool:
        return self._connected

    def get_cached_volume(self) -> Optional[int]:
        """Return last known volume without talking to the mixer."""
        return self._current_volume

    def get_volume(self, *, timeout: float = 1.0) -> int:
        """Ask the mixer for current Left fader level; updates cache; returns level."""
        return self._op_with_retry(lambda: self._get_volume_once(timeout=timeout))

    def set_volume(self, level: int, *, timeout: float = 1.0) -> int:
        """Clamp and set Left fader level (int dB*100). Fires volume_changed after success."""
        level = int(max(self.MIN_LEVEL, min(self.MAX_LEVEL, int(level))))
        return self._op_with_retry(lambda: self._set_volume_once(level, timeout=timeout))

    def increment_volume(self, step: int = 100, *, timeout: float = 1.0) -> int:
        """Increase volume by step (default 1.00 dB)."""
        def do():
            cur = self._current_volume
            if cur is None:
                cur = self._get_volume_once(timeout=timeout)
            return self._set_volume_once(int(cur) + int(step), timeout=timeout)
        return self._op_with_retry(do)

    def decrement_volume(self, step: int = 100, *, timeout: float = 1.0) -> int:
        """Decrease volume by step (default 1.00 dB)."""
        def do():
            cur = self._current_volume
            if cur is None:
                cur = self._get_volume_once(timeout=timeout)
            return self._set_volume_once(int(cur) - int(step), timeout=timeout)
        return self._op_with_retry(do)

    # ---- Internal ops (single-shot) ----

    def _get_volume_once(self, *, timeout: float) -> int:
        self._require_connected()
        with self._cmd_lock:
            self._send_line("get MIXER:Current/St/Fader/Level 0 0")
            op, ch, lvl = self._wait_for_ok(expected_op="get", timeout=timeout)
            if ch != 0 or lvl is None:
                raise MixerError("Unexpected GET response")
            self._update_volume(lvl, source="get")
            return lvl

    def _set_volume_once(self, level: int, *, timeout: float) -> int:
        self._require_connected()
        level = int(max(self.MIN_LEVEL, min(self.MAX_LEVEL, level)))
        with self._cmd_lock:
            self._send_line(f"set MIXER:Current/St/Fader/Level 0 0 {level}")
            op, ch, lvl = self._wait_for_ok(expected_op="set", timeout=timeout)
            if ch != 0:
                raise MixerError("Unexpected SET response channel")
            final = level if lvl is None else int(lvl)
            self._update_volume(final, source="set")
            return final

    # ---- Retry wrapper ----

    def _op_with_retry(self, fn):
        try:
            return fn()
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError, MixerError) as e:
            if self.debug:
                print(f"[mixer] op failed ({e}); attempting reconnect+retry")
            self.connect()
            return fn()

    # ---- Reader & parsing ----

    def _reader_loop(self, conn_id: int):
        try:
            while not self._stop_evt.is_set():
                try:
                    chunk = self._sock.recv(4096) if self._sock else b""
                    if not chunk:
                        break
                    self._buf += chunk
                    while b"\n" in self._buf:
                        line, self._buf = self._buf.split(b"\n", 1)
                        self._handle_line(line.decode("utf-8", errors="replace").strip(), conn_id)
                except (OSError, ConnectionError):
                    break
        finally:
            # Only mark disconnected if this is still the active generation
            if self._connected and conn_id == self._conn_id:
                self._connected = False
                if self.debug:
                    print(f"[mixer] Reader stopped / connection lost (gen {conn_id})")
                if self._on_disconnected:
                    self._on_disconnected()

    def _handle_line(self, line: str, conn_id: int):
        if self.debug:
            print(f"<< {line}")
        m = self._OK_RE.match(line)
        if m:
            op = m.group(1)
            ch = int(m.group(2))
            lvl = m.group(3)
            level_val = int(lvl) if lvl is not None else None
            self._resp_q.put({"gen": conn_id, "type": "ok", "op": op, "ch": ch, "level": level_val})
            return
        m = self._ERROR_RE.match(line)
        if m:
            self._resp_q.put({"gen": conn_id, "type": "error", "text": m.group(1).strip()})
            return
        m = self._NOTIFY_RE.match(line)
        if m:
            ch = int(m.group(1))
            lvl = int(m.group(2))
            if ch == 0:
                # Left channel only; cache & callback
                self._update_volume(lvl, source="notify")
            return
        # Unknown lines ignored

    def _wait_for_ok(self, *, expected_op: str, timeout: float):
        deadline = time.time() + timeout
        cur_gen = self._conn_id
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for response")
            try:
                msg = self._resp_q.get(timeout=remaining)
            except Empty:
                raise TimeoutError("Timed out waiting for response")

            # Ignore stale messages from previous generations
            if msg.get("gen") != cur_gen:
                continue
            if msg.get("type") == "ok" and msg.get("op") == expected_op:
                return msg["op"], msg["ch"], msg.get("level")
            if msg.get("type") == "error":
                raise MixerError(msg.get("text", "ERROR from mixer"))

    # ---- Helpers ----

    def _require_connected(self):
        if not self._connected or not self._sock:
            raise MixerError("Mixer not connected")

    def _send_line(self, line: str):
        if not self._sock:
            raise MixerError("Socket not available")
        data = (line + "\n").encode("ascii")
        if self.debug:
            print(f">> {line}")
        try:
            self._sock.sendall(data)
        except Exception as e:
            raise BrokenPipeError(e)

    def _update_volume(self, new_level: int, *, source: str):
        prev = self._current_volume
        self._current_volume = int(new_level)
        if self.debug:
            print(f"[mixer] volume {source}: {prev} -> {self._current_volume}")
        if self._on_volume_changed:
            self._on_volume_changed(self._current_volume)

    def _stop_reader_sync(self):
        """Stop & join reader, close socket, reset flags for a clean generation rollover."""
        self._stop_evt.set()
        sock = self._sock
        self._sock = None
        if sock:
            try:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                sock.close()
            except Exception:
                pass
        t = self._reader
        self._reader = None
        if t and t.is_alive():
            t.join(timeout=self._reader_join_timeout)
        self._connected = False
        self._clear_queue(self._resp_q)
        self._buf = b""

    @staticmethod
    def _clear_queue(q: Queue):
        try:
            while True:
                q.get_nowait()
        except Empty:
            pass


# ===================== Razer Knob (evdev) =====================

class RazerKnob:
    """
    Watches for a device whose name starts with 'Razer Wireless Control Pod'.
    When connected, reads EV_KEY events and invokes key_callback(str) with one of:
        'volume_up', 'volume_down', 'mute', 'play_pause'
    Auto-reconnects if the device disappears.
    """
    DEVICE_PREFIX = "Razer Wireless Control Pod"

    def __init__(self, *, key_callback: Optional[Callable[[str], None]] = None, debug: bool = True):
        self._key_cb = key_callback
        self.debug = debug
        self._dev = None
        self._status = "searching"  # 'searching' | 'connected'
        self._last_connect_attempt = 0.0

    def get_status(self) -> str:
        return self._status

    def set_key_callback(self, cb: Callable[[str], None]):
        self._key_cb = cb

    def _maybe_connect(self):
        now = time.time()
        if now - self._last_connect_attempt < 0.5:
            return
        self._last_connect_attempt = now
        try:
            from evdev import InputDevice, list_devices
            for path in list_devices():
                try:
                    dev = InputDevice(path)
                    name = dev.name or ""
                    if name.startswith(self.DEVICE_PREFIX):
                        self._dev = dev
                        try:
                            self._dev.grab()
                        except Exception:
                            pass
                        self._dev.set_blocking(False)
                        self._status = "connected"
                        if self.debug:
                            print(f"[knob] Connected to {name} at {path}")
                        return
                except Exception:
                    continue
        except Exception as e:
            if self.debug:
                print(f"[knob] Scan error: {e}")
        # remain searching

    def _disconnect(self):
        if self._dev:
            try:
                try:
                    self._dev.ungrab()
                except Exception:
                    pass
                self._dev.close()
            except Exception:
                pass
        self._dev = None
        if self._status != "searching":
            self._status = "searching"
            if self.debug:
                print("[knob] Disconnected; will keep searching")

    def poll(self):
        """
        Non-blocking. Should be called frequently by the main loop.
        Emits callbacks for any keypresses observed.
        """
        if self._dev is None:
            self._maybe_connect()
            return

        try:
            from evdev import categorize, ecodes
            while True:
                try:
                    event = self._dev.read_one()
                except OSError:
                    self._disconnect()
                    break
                if event is None:
                    break
                if event.type != ecodes.EV_KEY:
                    continue
                key = categorize(event)
                if key.keystate != key.key_down:  # only on key down
                    continue
                mapped = self._map_ecode_to_action(key.scancode)
                if mapped and self._key_cb:
                    if self.debug:
                        print(f"[knob] key: {mapped}")
                    self._key_cb(mapped)
        except Exception:
            self._disconnect()

    @staticmethod
    def _map_ecode_to_action(scancode: int) -> Optional[str]:
        try:
            from evdev import ecodes
        except Exception:
            return None
        mapping = {
            ecodes.KEY_VOLUMEUP: 'volume_up',
            ecodes.KEY_VOLUMEDOWN: 'volume_down',
            ecodes.KEY_MUTE: 'mute',
            ecodes.KEY_PLAYPAUSE: 'play_pause',
            ecodes.KEY_PLAY: 'play_pause',
            ecodes.KEY_PAUSE: 'play_pause',
        }
        return mapping.get(scancode)


# ===================== Glue / Controller =====================

class KnobMixerController:
    """
    Ties Razer knob to Yamaha mixer with specified behavior.

    Main loop:
      1) Poll knob and collect key events.
      2) If mixer not connected: try connect (connect() already uses 0.5s timeout).
      3) If connected: act on collected events; otherwise drop them.
      On any (re)connect, we query current volume.

    Mute logic:
      - 'mute' when unmuted: save current level, set to MIN_LEVEL, muted=True.
      - 'mute' when muted: restore saved level, muted=False.
      - 'volume_up' while muted: restore saved level first, then increment.
      - If a NOTIFY shows the mixer has increased while muted, auto-unmute.
    """

    def __init__(
        self,
        mixer_host: str = "172.16.0.2",
        mixer_port: int = 49280,
        *,
        step: int = 100,   # 1.00 dB
        debug: bool = True,
        on_volume_changed: Optional[Callable[[int], None]] = None,
        on_mixer_connected: Optional[Callable[[], None]] = None,
        on_mixer_disconnected: Optional[Callable[[], None]] = None,
    ):
        self.debug = debug
        self.step = step
        self._pending_keys: Deque[str] = deque()
        self._muted = False
        self._saved_volume: Optional[int] = None

        def _mixer_vol_changed(new_level: int):
            # Unmute if volume rose while muted (or generally not at hard-mute)
            if self._muted and new_level > YamahaDM3Mixer.MIN_LEVEL:
                if self.debug:
                    print("[controller] NOTIFY indicates increase while muted; unmuting")
                self._muted = False
            if on_volume_changed:
                on_volume_changed(new_level)

        def _on_conn():
            if self.debug:
                print("[controller] mixer (re)connected, refreshing volume")
            if on_mixer_connected:
                on_mixer_connected()

        self.mixer = YamahaDM3Mixer(
            mixer_host,
            mixer_port,
            debug=debug,
            on_volume_changed=_mixer_vol_changed,
            on_connected=_on_conn,
            on_disconnected=on_mixer_disconnected,
        )

        # Knob forwards key events into our queue
        self.knob = RazerKnob(
            key_callback=self._enqueue_key_event,
            debug=debug
        )

    # ---- UI-facing getters (no I/O) ----
    def get_last_known_volume(self) -> Optional[int]:
        return self.mixer.get_cached_volume()

    def get_mixer_status(self) -> str:
        return "connected" if self.mixer.is_connected() else "disconnected"

    def get_knob_status(self) -> str:
        return self.knob.get_status()

    # ---- Main loop ----
    def _enqueue_key_event(self, name: str):
        self._pending_keys.append(name)

    def run(self, *, poll_interval: float = 0.02):
        """
        Blocking loop. Ctrl-C to exit.
        """
        try:
            while True:
                # 1) Poll knob and collect events
                self.knob.poll()
                events: List[str] = []
                while self._pending_keys:
                    events.append(self._pending_keys.popleft())

                # 2) Ensure mixer connection (0.5s connect timeout is inside mixer.connect)
                if not self.mixer.is_connected():
                    self.mixer.connect()

                # 3) Process events if connected; else drop them
                if self.mixer.is_connected():
                    for ev in events:
                        self._handle_event(ev)
                else:
                    if events and self.debug:
                        print(f"[controller] Dropping {len(events)} events (mixer not connected)")

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.mixer.disconnect()

    # ---- Event handling ----
    def _handle_event(self, ev: str):
        if ev == 'volume_up':
            if self._muted:
                self._restore_and_unmute()
            try:
                self.mixer.increment_volume(self.step)
            except Exception as e:
                if self.debug:
                    print(f"[controller] increment failed: {e}")

        elif ev == 'volume_down':
            if self._muted:
                # Spec: do nothing on vol-down while muted
                return
            try:
                self.mixer.decrement_volume(self.step)
            except Exception as e:
                if self.debug:
                    print(f"[controller] decrement failed: {e}")

        elif ev == 'mute':
            if not self._muted:
                cur = self.mixer.get_cached_volume()
                if cur is None:
                    try:
                        cur = self.mixer.get_volume()
                    except Exception:
                        cur = None
                self._saved_volume = cur
                self._muted = True
                try:
                    self.mixer.set_volume(YamahaDM3Mixer.MIN_LEVEL)
                except Exception as e:
                    if self.debug:
                        print(f"[controller] mute failed: {e}")
            else:
                self._restore_and_unmute()

        elif ev == 'play_pause':
            if self.debug:
                print("[controller] play/pause pressed (no action)")

    def _restore_and_unmute(self):
        target = self._saved_volume
        if target is None:
            # Safe fallback if we never captured a volume
            target = -3000  # -30.00 dB
        try:
            self.mixer.set_volume(int(target))
            self._muted = False
        except Exception as e:
            if self.debug:
                print(f"[controller] unmute/restore failed: {e}")


# ===================== Example usage =====================

if __name__ == "__main__":
    def ui_volume_changed(v: int):
        print(f"[ui] Volume now: {v/100:.2f} dB")

    def ui_connected():
        print("[ui] Mixer connected")

    def ui_disconnected():
        print("[ui] Mixer disconnected")

    controller = KnobMixerController(
        mixer_host="172.16.0.2",
        mixer_port=49280,
        step=100,  # 1.00 dB per tick
        debug=True,
        on_volume_changed=ui_volume_changed,
        on_mixer_connected=ui_connected,
        on_mixer_disconnected=ui_disconnected,
    )

    print("Running main loop. Press Ctrl-C to exit.")
    controller.run()
