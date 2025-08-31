#!/usr/bin/env python3
"""
yamaha_razer_sdl.py

Yamaha DM3 RCP client + Razer Wireless Control Pod integration with SDL (pygame) UI.

Usage (framebuffer):
  sudo SDL_VIDEODRIVER=KMSDRM SDL_FBDEV=/dev/fb0 python3 sdlmixer.py

Dependencies:
  pip3 install evdev pygame

Notes:
 - Ensure this file is NOT named kivy.py (or pygame.py, evdev.py, etc.) to avoid shadowing packages.
 - You may need to run as root or add your user to 'video' and 'input' groups.
"""
import asyncio
import threading
import time
import re
from collections import deque
from typing import Optional, Callable, Deque
import os
import sys

# ---------------------- Yamaha RCP Mixer (async) ----------------------
class YamahaRCPMixer:
    DEFAULT_PORT = 49280
    MIN_LEVEL = -13800
    MAX_LEVEL = 1000

    def __init__(self, host: str, port: int = None, debug: bool = True):
        self.host = host
        self.port = port or self.DEFAULT_PORT
        self.debug = debug

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

        # queue of raw incoming lines produced by reader loop, consumed by dispatcher
        self._incoming_q: asyncio.Queue = asyncio.Queue()

        # FIFO of futures waiting for OK/ERROR responses to commands
        self._pending: Deque[asyncio.Future] = deque()

        # optional future waiting for a numeric reply (for get_volume)
        self._numeric_waiter: Optional[asyncio.Future] = None

        # background tasks
        self._reader_task: Optional[asyncio.Task] = None
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._set_worker_task: Optional[asyncio.Task] = None

        # shared state
        self.current_volume: Optional[int] = None
        self._volume_lock = asyncio.Lock()

        # coalescing desired volume
        self._desired_volume: Optional[int] = None
        self._desired_lock = asyncio.Lock()

        # callbacks (set by caller)
        self.on_volume_changed: Optional[Callable[[int], None]] = None
        self.on_connect: Optional[Callable[[], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

        self._connected = False
        self._stop = False

    # ----- connection lifecycle -----
    async def connect(self):
        if self._connected:
            return
        if self.debug:
            print(f"[Mixer] Connecting to {self.host}:{self.port}")
        try:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            self._connected = True
            self._stop = False
            # spawn tasks
            self._reader_task = asyncio.create_task(self._reader_loop(), name="mixer_reader")
            self._dispatcher_task = asyncio.create_task(self._dispatcher_loop(), name="mixer_dispatcher")
            self._set_worker_task = asyncio.create_task(self._set_worker(), name="mixer_set_worker")
            if self.on_connect:
                try:
                    self.on_connect()
                except Exception:
                    pass
            # probe initial volume
            try:
                await asyncio.sleep(0.05)
                vol = await self.get_volume()
                if self.debug:
                    print(f"[Mixer] initial volume {vol}")
            except Exception as e:
                if self.debug:
                    print(f"[Mixer] initial get failed: {e}")
        except Exception as e:
            self._connected = False
            raise

    async def disconnect(self):
        self._stop = True
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        for t in (self._reader_task, self._dispatcher_task, self._set_worker_task):
            if t:
                t.cancel()
        if self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception:
                pass

    # ----- low-level send/receive -----
    async def _reader_loop(self):
        """Read raw lines from the TCP stream and push into incoming_q"""
        try:
            while not self._stop:
                line = await self._reader.readline()
                if not line:
                    # EOF/connection closed
                    if self.debug:
                        print("[Mixer] connection closed by remote")
                    await self._incoming_q.put("__DISCONNECT__")
                    break
                txt = line.decode("utf-8", errors="ignore").rstrip("\r\n")
                if self.debug:
                    print(f"[Mixer RX] {txt}")
                await self._incoming_q.put(txt)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if self.debug:
                print(f"[Mixer reader error] {e}")
            await self._incoming_q.put("__DISCONNECT__")

    async def _dispatcher_loop(self):
        """Single consumer: handle notifications, OK/ERROR responses, numeric replies"""
        try:
            while not self._stop:
                line = await self._incoming_q.get()
                if line == "__DISCONNECT__":
                    self._connected = False
                    if self.on_disconnect:
                        try:
                            self.on_disconnect()
                        except Exception:
                            pass
                    # fail all pending
                    while self._pending:
                        fut = self._pending.popleft()
                        if not fut.done():
                            fut.set_exception(ConnectionError("Disconnected"))
                    # cancel numeric waiter
                    if self._numeric_waiter and not self._numeric_waiter.done():
                        self._numeric_waiter.set_exception(ConnectionError("Disconnected"))
                    break

                # NOTIFY messages
                if line.startswith("NOTIFY"):
                    # parse lines like:
                    # NOTIFY set MIXER:Current/St/Fader/Level 0 0 -600 "-6.00"
                    m = re.match(r'^NOTIFY\s+set\s+(\S+)\s+(\d+)\s+(\d+)\s+(-?\d+)', line)
                    if m:
                        path = m.group(1)
                        chan = int(m.group(2))   # interpret first number as channel index (0=left,1=right)
                        level = int(m.group(4))
                        if path.endswith("Fader/Level"):
                            if chan == 0:
                                # left channel => update current volume
                                async def _update():
                                    async with self._volume_lock:
                                        self.current_volume = level
                                    if self.on_volume_changed:
                                        try:
                                            self.on_volume_changed(level)
                                        except Exception:
                                            pass
                                asyncio.create_task(_update())
                            else:
                                if self.debug:
                                    print(f"[Mixer] NOTIFY right channel ignored (chan={chan})")
                        continue
                    else:
                        if self.debug:
                            print(f"[Mixer] Unparsed NOTIFY: {line}")
                        continue

                # OK / ERROR responses
                if line.startswith("OK") or line.startswith("ERROR"):
                    if self._pending:
                        fut = self._pending.popleft()
                        if not fut.done():
                            fut.set_result(line)
                    else:
                        if self.debug:
                            print("[Mixer] Received OK/ERROR but no pending command")
                    continue

                # numeric-like lines (bare number or quoted number)
                num_match = re.match(r'^\s*"?(-?\d+)(?:\.\d+)?\"?\s*$', line)
                if num_match:
                    try:
                        val = int(num_match.group(1))
                        if self._numeric_waiter and not self._numeric_waiter.done():
                            self._numeric_waiter.set_result(val)
                            async def _set_cur():
                                async with self._volume_lock:
                                    self.current_volume = val
                            asyncio.create_task(_set_cur())
                    except Exception:
                        pass
                    continue

                if self.debug:
                    print(f"[Mixer] Unhandled line: {line}")

        except asyncio.CancelledError:
            return

    async def _send_raw(self, cmd: str):
        if not self._connected or not self._writer:
            raise ConnectionError("Not connected to mixer")
        if self.debug:
            print(f"[Mixer TX] {cmd.rstrip()}")
        self._writer.write(cmd.encode("utf-8"))
        await self._writer.drain()

    async def _send_command(self, cmd: str, timeout: float = 2.0) -> str:
        """Send a command and wait for OK/ERROR. Returns the raw OK/ERROR line."""
        if not self._connected:
            raise ConnectionError("Not connected to mixer")
        fut = asyncio.get_running_loop().create_future()
        self._pending.append(fut)
        await self._send_raw(cmd if cmd.endswith("\n") else cmd + "\n")
        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res
        except Exception:
            if not fut.done():
                fut.cancel()
            raise

    # ----- Public API -----
    async def get_volume(self, timeout: float = 1.0) -> int:
        """Query the mixer for current main volume. Returns int (centi-dB)."""
        if not self._connected:
            raise ConnectionError("Not connected to mixer")
        loop = asyncio.get_running_loop()
        self._numeric_waiter = loop.create_future()
        await self._send_command("get MIXER:Current/St/Fader/Level 0 0\n")
        try:
            val = await asyncio.wait_for(self._numeric_waiter, timeout=timeout)
            async with self._volume_lock:
                self.current_volume = val
            return val
        except asyncio.TimeoutError:
            if self.current_volume is not None:
                return self.current_volume
            raise RuntimeError("Timeout waiting for mixer numeric response")
        finally:
            self._numeric_waiter = None

    async def set_volume(self, level: int):
        """Set the main fader to integer level (centi-dB). After success, current_volume updated and callback fires."""
        if not self._connected:
            raise ConnectionError("Not connected to mixer")
        level = int(level)
        if level < self.MIN_LEVEL:
            level = self.MIN_LEVEL
        if level > self.MAX_LEVEL:
            level = self.MAX_LEVEL
        cmd = f"set MIXER:Current/St/Fader/Level 0 0 {level}\n"
        res = await self._send_command(cmd)
        if res.startswith("OK"):
            async with self._volume_lock:
                self.current_volume = level
            if self.on_volume_changed:
                try:
                    self.on_volume_changed(level)
                except Exception:
                    pass
            return
        else:
            raise RuntimeError(f"Mixer returned error: {res}")

    async def _set_worker(self):
        """Background worker that coalesces rapid set requests (write only the latest requested)."""
        try:
            while not self._stop:
                await asyncio.sleep(0.06)
                async with self._desired_lock:
                    if self._desired_volume is None:
                        continue
                    target = self._desired_volume
                    self._desired_volume = None
                try:
                    await self.set_volume(target)
                except Exception as e:
                    if self.debug:
                        print(f"[Mixer set_worker] failed to set volume: {e}")
                    await asyncio.sleep(0.5)
                    try:
                        await self.connect()
                    except Exception:
                        await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    async def increment_volume(self, step: int = 100):
        async with self._volume_lock:
            current = self.current_volume if self.current_volume is not None else -6000
        new = current + step
        if new > self.MAX_LEVEL:
            new = self.MAX_LEVEL
        async with self._desired_lock:
            self._desired_volume = new

    async def decrement_volume(self, step: int = 100):
        async with self._volume_lock:
            current = self.current_volume if self.current_volume is not None else -6000
        new = current - step
        if new < self.MIN_LEVEL:
            new = self.MIN_LEVEL
        async with self._desired_lock:
            self._desired_volume = new

# ---------------------- Razer Knob Listener (evdev) ----------------------
try:
    import evdev
    from evdev import InputDevice, list_devices, ecodes
except Exception:
    evdev = None

class RazerKnobListener:
    DEVICE_NAME_PREFIX = "Razer Wireless Control Pod"
    def __init__(self, callback, debug: bool = True):
        """
        callback: async function accepting action string: 'volume_up','volume_down','mute','play_pause'
        """
        if evdev is None:
            raise RuntimeError("evdev is required (pip install evdev)")
        self.callback = callback
        self.debug = debug
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name="razer_knob")

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _find_device_path(self) -> Optional[str]:
        for path in list_devices():
            try:
                dev = InputDevice(path)
                if dev.name.startswith(self.DEVICE_NAME_PREFIX):
                    return path
            except Exception:
                continue
        return None

    async def _run(self):
        while not self._stop:
            path = await self._find_device_path()
            if path is None:
                if self.debug:
                    print("[Knob] waiting for Razer Wireless Control Pod...")
                await asyncio.sleep(1.0)
                continue
            if self.debug:
                print(f"[Knob] found device at {path}")
            try:
                dev = InputDevice(path)
                async for event in dev.async_read_loop():
                    if self._stop:
                        break
                    if event.type == ecodes.EV_KEY and event.value == 1:  # key down
                        key = ecodes.KEY.get(event.code, None)
                        if key is None:
                            continue
                        if self.debug:
                            print(f"[Knob RX] key {key}")
                        if key == 'KEY_VOLUMEUP':
                            await self.callback('volume_up')
                        elif key == 'KEY_VOLUMEDOWN':
                            await self.callback('volume_down')
                        elif key == 'KEY_MUTE':
                            await self.callback('mute')
                        elif key == 'KEY_PLAYPAUSE':
                            await self.callback('play_pause')
                        else:
                            pass
            except Exception as e:
                if self.debug:
                    print(f"[Knob] device error/disconnected: {e}")
                await asyncio.sleep(0.5)
                continue

# ---------------------- Thread-safe Controller State (main-thread readable) ----------------------
class ControllerState:
    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self.connected = False
        self.knob_connected = False
        self.volume = None  # centi-dB int or None
        self.last_update = 0.0
        self.debug = True
        self.muted = False

    def set_connected(self, v: bool):
        with self._lock:
            self.connected = v
            self.last_update = time.time()

    def set_knob(self, v: bool):
        with self._lock:
            self.knob_connected = v
            self.last_update = time.time()

    def set_volume(self, vol: Optional[int]):
        with self._lock:
            self.volume = vol
            self.last_update = time.time()

    def set_muted(self, m: bool):
        with self._lock:
            self.muted = m
            self.last_update = time.time()

    def snapshot(self):
        with self._lock:
            return {
                'connected': self.connected,
                'knob_connected': self.knob_connected,
                'volume': self.volume,
                'last_update': self.last_update,
                'muted': self.muted
            }

# ---------------------- Controller (ties mixer + knob) ----------------------
class Controller:
    def __init__(self, state: ControllerState, mixer: YamahaRCPMixer, debug: bool = True):
        self.state = state
        self.mixer = mixer
        self.debug = debug
        self.knob: Optional[RazerKnobListener] = None
        self._last_before_mute: Optional[int] = None

        # attach mixer callbacks
        self.mixer.on_volume_changed = self._on_volume_changed
        self.mixer.on_connect = self._on_connect
        self.mixer.on_disconnect = self._on_disconnect

    async def start(self):
        async def knob_cb(action: str):
            # set knob presence flag (best-effort)
            self.state.set_knob(True)
            if not self.mixer._connected:
                if self.debug:
                    print("[Controller] Ignoring knob input: mixer offline")
                return
            if action == 'volume_up':
                await self.mixer.increment_volume()
            elif action == 'volume_down':
                await self.mixer.decrement_volume()
            elif action == 'mute':
                await self.toggle_mute()
            elif action == 'play_pause':
                if self.debug:
                    print("[Controller] play/pause pressed (ignored)")

        self.knob = RazerKnobListener(callback=knob_cb, debug=self.debug)
        self.knob.start()

        while True:
            try:
                if not self.mixer._connected:
                    try:
                        await self.mixer.connect()
                    except Exception as e:
                        if self.debug:
                            print(f"[Controller] mixer connect failed: {e}")
                        await asyncio.sleep(2.0)
                        continue
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                try:
                    await self.mixer.disconnect()
                except Exception:
                    pass
                try:
                    await self.knob.stop()
                except Exception:
                    pass
                return

    async def toggle_mute(self):
        if not self.mixer._connected:
            return
        if not self.state.snapshot()['muted']:
            self._last_before_mute = self.mixer.current_volume
            try:
                await self.mixer.set_volume(self.mixer.MIN_LEVEL)
                self.state.set_muted(True)
            except Exception as e:
                if self.debug:
                    print(f"[Controller] mute failed: {e}")
        else:
            target = self._last_before_mute if self._last_before_mute is not None else -600
            try:
                await self.mixer.set_volume(target)
                self.state.set_muted(False)
            except Exception as e:
                if self.debug:
                    print(f"[Controller] unmute failed: {e}")

    def _on_volume_changed(self, new_level: int):
        if self.debug:
            print(f"[Controller] on_volume_changed {new_level}")
        self.state.set_volume(new_level)

    def _on_connect(self):
        if self.debug:
            print("[Controller] mixer connected")
        self.state.set_connected(True)
        async def probe():
            try:
                vol = await self.mixer.get_volume()
                self.state.set_volume(vol)
            except Exception:
                pass
        asyncio.create_task(probe())

    def _on_disconnect(self):
        if self.debug:
            print("[Controller] mixer disconnected")
        self.state.set_connected(False)

# ---------------------- Robust SDL (pygame) UI ----------------------
def run_pygame_ui(state: ControllerState):
    """
    Robust pygame UI: logs to /tmp/yamaha_razer_ui.log and auto-restarts on failure.
    """
    import traceback
    LOG = "/tmp/yamaha_razer_ui.log"

    def log(msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} {msg}\n"
        with open(LOG, "a") as fh:
            fh.write(line)

    os.environ.setdefault('SDL_VIDEODRIVER', os.environ.get('SDL_VIDEODRIVER', 'fbcon'))
    os.environ.setdefault('SDL_FBDEV', os.environ.get('SDL_FBDEV', '/dev/fb0'))

    log("UI start; SDL_VIDEODRIVER=%s SDL_FBDEV=%s" % (os.environ.get('SDL_VIDEODRIVER'), os.environ.get('SDL_FBDEV')))

    try:
        import pygame
    except Exception as e:
        log(f"pygame import failed: {e}")
        raise

    while True:
        try:
            log("Attempting pygame.init()")
            pygame.init()
            try:
                screen = pygame.display.set_mode((800, 480), 0, 16)
                log("Display opened (16-bit mode).")
            except Exception as e16:
                log(f"16-bit mode failed: {e16}; trying default mode.")
                screen = pygame.display.set_mode((800, 480))
                log("Display opened (default mode).")
            pygame.mouse.set_visible(False)
            pygame.display.set_caption("Mixer Volume")
            clock = pygame.time.Clock()
            try:
                font = pygame.font.Font(None, 36)
                small_font = pygame.font.Font(None, 24)
            except Exception:
                font = None
                small_font = None
                log("Font creation failed; continuing without TTF fonts.")

            BAR_X = 50
            BAR_Y = 220
            BAR_W = 700
            BAR_H = 60

            running = True
            log("UI main loop start.")
            while running:
                try:
                    for ev in pygame.event.get():
                        if ev.type != pygame.NOEVENT:
                            log(f"SDL event: {ev}")
                        if ev.type == pygame.QUIT:
                            log("Received SDL QUIT event; exiting ui loop.")
                            running = False

                    snap = state.snapshot()
                    screen.fill((0, 0, 0))

                    status_text = "Connected" if snap['connected'] else "Disconnected"
                    knob_text = "Knob connected" if snap['knob_connected'] else "Knob disconnected"
                    if font:
                        status_surf = font.render(f"Mixer: {status_text}", True, (255, 255, 255))
                        screen.blit(status_surf, (20, 10))
                        knob_surf = small_font.render(knob_text, True, (200, 200, 200))
                        screen.blit(knob_surf, (20, 50))

                    vol = snap['volume']
                    if vol is None:
                        vol_text = "Volume: --"
                        mapped = 0
                    else:
                        vol_text = f"Volume: {vol} (centi-dB)"
                        mapped = vol + 13800
                        if mapped < 0: mapped = 0
                        if mapped > 14800: mapped = 14800

                    if font:
                        vol_surf = font.render(vol_text, True, (255, 255, 0))
                        screen.blit(vol_surf, (20, 100))

                    fill_w = int((mapped / 14800.0) * BAR_W) if 14800 else 0
                    pygame.draw.rect(screen, (50, 50, 50), (BAR_X, BAR_Y, BAR_W, BAR_H))
                    pygame.draw.rect(screen, (0, 200, 0), (BAR_X, BAR_Y, fill_w, BAR_H))
                    pygame.draw.rect(screen, (255, 255, 255), (BAR_X, BAR_Y, BAR_W, BAR_H), 2)

                    if snap['muted'] and font:
                        mute_surf = font.render("MUTED", True, (255, 0, 0))
                        screen.blit(mute_surf, (600, 100))

                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap['last_update'] or time.time()))
                    if small_font:
                        footer = small_font.render(f"Updated: {ts}", True, (180, 180, 180))
                        screen.blit(footer, (20, 430))

                    pygame.display.flip()
                    clock.tick(30)

                except Exception as inner_e:
                    tb = traceback.format_exc()
                    log("UI inner loop exception:\n" + tb)
                    running = False
                    break

            log("UI loop exited; cleaning up and retrying.")
            try:
                pygame.quit()
            except Exception as e:
                log(f"pygame.quit failed: {e}")

        except Exception as e:
            import traceback as _tb
            tb = _tb.format_exc()
            log("UI startup exception:\n" + tb)

        # Sleep a bit before reinitializing UI
        log("UI will retry in 1s")
        time.sleep(1.0)

# ---------------------- Main: start asyncio loop in background thread, run pygame in main thread ----------------------
def main():
    HOST = "172.16.0.2"
    PORT = 49280
    DEBUG = True

    state = ControllerState()
    state.debug = DEBUG

    stop_event = threading.Event()
    bg_exception_log = "/tmp/yamaha_razer_bg.log"

    # background thread: asyncio loop
    def bg_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        mixer = YamahaRCPMixer(HOST, PORT, debug=DEBUG)
        controller = Controller(state=state, mixer=mixer, debug=DEBUG)

        async def startup():
            task = asyncio.create_task(controller.start(), name="controller_main")
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            loop.run_until_complete(startup())
        except Exception:
            import traceback
            with open(bg_exception_log, "a") as fh:
                fh.write("=== Background exception ===\n")
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                traceback.print_exc(file=fh)
                fh.write("\n\n")
            print("Background thread exception (logged to {})".format(bg_exception_log))
        finally:
            try:
                loop.run_until_complete(mixer.disconnect())
            except Exception:
                pass
            try:
                loop.stop()
            except Exception:
                pass
            stop_event.set()

    t = threading.Thread(target=bg_thread, daemon=True)
    t.start()

    # Ensure SDL env defaults for framebuffer use if running on Pi
    os.environ.setdefault('SDL_VIDEODRIVER', os.environ.get('SDL_VIDEODRIVER', 'fbcon'))
    os.environ.setdefault('SDL_FBDEV', os.environ.get('SDL_FBDEV', '/dev/fb0'))

    # Run pygame UI in main thread
    try:
        run_pygame_ui(state)
    except Exception as e:
        print("UI failed, falling back to headless. See logs for details:", e)
        try:
            while not stop_event.is_set():
                snap = state.snapshot()
                print(f"[HEADLESS] connected={snap['connected']} knob={snap['knob_connected']} volume={snap['volume']} muted={snap['muted']}")
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("Interrupted by user")
    finally:
        print("Shutting down main thread, waiting for background thread to exit...")
        stop_event.set()
        t.join(timeout=2.0)
        print("Exited.")

if __name__ == "__main__":
    main()

