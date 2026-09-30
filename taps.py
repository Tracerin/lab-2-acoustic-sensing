"""
Live audio gesture detection using microphone FFT controlling macOS App Switcher:
  - Double Tap: Opens switcher (holds Cmd+Tab) / Confirms selection (releases Cmd)
  - Tap: Advances forward to next app (Tab)
  - Drag: Moves backward to previous app (Left Arrow)
"""

import ctypes
from ctypes import c_void_p, c_uint32, c_uint64, c_bool
import queue
import time
import numpy as np
import sounddevice as sd
from scipy.signal import get_window

# -----------------------------
# macOS CoreGraphics Setup
# -----------------------------
cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

# Explicitly declare 64-bit parameter and return types to prevent pointer truncation segfaults
cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint32, c_bool]
cg.CGEventCreateKeyboardEvent.restype = c_void_p

cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
cg.CGEventSetFlags.restype = None

cg.CGEventPost.argtypes = [c_uint32, c_void_p]
cg.CGEventPost.restype = None

cg.CFRelease.argtypes = [c_void_p]
cg.CFRelease.restype = None

VK_COMMAND = 0x37
VK_TAB = 0x30
VK_LEFT_ARROW = 0x7B
kCGEventFlagMaskCommand = 0x00100000
kCGHIDEventTap = 0


class MacAppSwitcher:
    def __init__(self):
        self.is_active = False

    def _post_key(self, keycode, down, flags=0):
        event = cg.CGEventCreateKeyboardEvent(None, keycode, down)
        if not event:
            return
        if flags:
            cg.CGEventSetFlags(event, flags)
        cg.CGEventPost(kCGHIDEventTap, event)
        cg.CFRelease(event)

    def open_switcher(self):
        """Holds Command and taps Tab once."""
        self._post_key(VK_COMMAND, True)
        time.sleep(0.01)
        self._post_key(VK_TAB, True, flags=kCGEventFlagMaskCommand)
        self._post_key(VK_TAB, False, flags=kCGEventFlagMaskCommand)
        self.is_active = True

    def next_app(self):
        """Advances forward while Command is held."""
        if not self.is_active:
            return
        self._post_key(VK_TAB, True, flags=kCGEventFlagMaskCommand)
        self._post_key(VK_TAB, False, flags=kCGEventFlagMaskCommand)

    def prev_app(self):
        """Moves backward using Left Arrow while Command is held."""
        if not self.is_active:
            return
        self._post_key(VK_LEFT_ARROW, True, flags=kCGEventFlagMaskCommand)
        self._post_key(VK_LEFT_ARROW, False, flags=kCGEventFlagMaskCommand)

    def select_app(self):
        """Releases Command to focus the chosen application."""
        if not self.is_active:
            return
        self._post_key(VK_COMMAND, False)
        self.is_active = False


switcher = MacAppSwitcher()


# -----------------------------
# Action Callbacks
# -----------------------------
def on_double_tap():
    if not switcher.is_active:
        print(">>> [DOUBLE TAP] Opening App Switcher")
        switcher.open_switcher()
    else:
        print(">>> [DOUBLE TAP] Confirming Selection")
        switcher.select_app()

def on_tap():
    if switcher.is_active:
        print(">>> [TAP] Next App")
        switcher.next_app()

def on_drag_start():
    if switcher.is_active:
        print(">>> [DRAG] Previous App")
        switcher.prev_app()

def on_drag_end():
    pass


# -----------------------------
# Audio & FFT Configuration
# -----------------------------
SAMPLE_RATE = 48_000
BLOCK_SIZE = 1024
CHANNELS = 1
FFT_SIZE = 4096
INPUT_DEVICE = 0

# Tap Parameters
TAP_LOW_FREQ = (0, 200)
TAP_MIN_DB = -40.0
TAP_DEBOUNCE_TIME = 0.08
TAP_DRAG_LOCKOUT_TIME = 0.22
DOUBLE_TAP_MAX_DELAY = 0.40

# Drag Parameters
DRAG_LOW_FREQ = (0, 200)
DRAG_LOW_DB_RANGE = (-80.0, -40.0)
DRAG_HIGH_FREQ = (3000, 5000)
DRAG_HIGH_DB_RANGE = (-100.0, -80.0)
DRAG_REQUIRED_WINDOWS = 2
DRAG_DEBOUNCE_TIME = 0.30
DRAG_RELEASE_TOLERANCE_FRAMES = 2


# -----------------------------
# Gesture State Machine
# -----------------------------
class AudioGestureDetector:
    def __init__(self, sample_rate, fft_size, window_weights):
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.window_weights = window_weights
        self.window_sum = np.sum(window_weights)
        self.freq_bins = np.fft.rfftfreq(fft_size, 1 / sample_rate)

        self.audio_buffer = np.zeros(fft_size, dtype=np.float32)

        self.idx_tap = self._get_bin_indices(TAP_LOW_FREQ)
        self.idx_drag_low = self._get_bin_indices(DRAG_LOW_FREQ)
        self.idx_drag_high = self._get_bin_indices(DRAG_HIGH_FREQ)

        self.pending_first_tap_time = None
        self.last_tap_event_time = 0.0
        self.tap_held = False

        self.drag_consecutive_windows = 0
        self.drag_dropped_frames = 0
        self.last_drag_end_time = 0.0
        self.is_dragging = False

    def _get_bin_indices(self, freq_range):
        low_idx = np.searchsorted(self.freq_bins, freq_range[0])
        high_idx = np.searchsorted(self.freq_bins, freq_range[1])
        return low_idx, max(low_idx + 1, high_idx)

    def compute_spectrum_db(self, block):
        self.audio_buffer = np.roll(self.audio_buffer, -len(block))
        self.audio_buffer[-len(block):] = block

        block_centered = self.audio_buffer - np.mean(self.audio_buffer)
        spectrum = np.fft.rfft(block_centered * self.window_weights)

        magnitude = np.abs(spectrum) / self.window_sum
        return 20 * np.log10(np.maximum(magnitude, 1e-10))

    def process_frame(self, block):
        now = time.time()
        spectrum_db = self.compute_spectrum_db(block)

        # 1. Feature Extraction
        tap_band_db = np.max(spectrum_db[self.idx_tap[0]:self.idx_tap[1]])
        drag_low_db = np.mean(spectrum_db[self.idx_drag_low[0]:self.idx_drag_low[1]])
        drag_high_db = np.mean(spectrum_db[self.idx_drag_high[0]:self.idx_drag_high[1]])

        # 2. Tap & Double Tap Logic
        tap_condition_active = tap_band_db > TAP_MIN_DB

        if tap_condition_active:
            if not self.tap_held and (now - self.last_tap_event_time) > TAP_DEBOUNCE_TIME:
                self.tap_held = True
                self.last_tap_event_time = now

                self.drag_consecutive_windows = 0
                self.drag_dropped_frames = 0
                if self.is_dragging:
                    self.is_dragging = False
                    self.last_drag_end_time = now
                    on_drag_end()

                if self.pending_first_tap_time is None:
                    self.pending_first_tap_time = now
                else:
                    if (now - self.pending_first_tap_time) <= DOUBLE_TAP_MAX_DELAY:
                        on_double_tap()
                        self.pending_first_tap_time = None
                    else:
                        on_tap()
                        self.pending_first_tap_time = now
        else:
            self.tap_held = False

        if self.pending_first_tap_time is not None:
            if (now - self.pending_first_tap_time) > DOUBLE_TAP_MAX_DELAY:
                on_tap()
                self.pending_first_tap_time = None

        # 3. Drag Logic
        in_tap_lockout = (now - self.last_tap_event_time) < TAP_DRAG_LOCKOUT_TIME
        in_drag_debounce = (now - self.last_drag_end_time) < DRAG_DEBOUNCE_TIME

        drag_condition_active = (
            not in_tap_lockout and
            (DRAG_LOW_DB_RANGE[0] <= drag_low_db < DRAG_LOW_DB_RANGE[1]) and
            (DRAG_HIGH_DB_RANGE[0] <= drag_high_db < DRAG_HIGH_DB_RANGE[1])
        )

        if drag_condition_active:
            self.drag_dropped_frames = 0
            if not self.is_dragging and not in_drag_debounce:
                self.drag_consecutive_windows += 1
                if self.drag_consecutive_windows >= DRAG_REQUIRED_WINDOWS:
                    self.is_dragging = True
                    on_drag_start()
        else:
            self.drag_consecutive_windows = 0
            if self.is_dragging:
                self.drag_dropped_frames += 1
                if self.drag_dropped_frames >= DRAG_RELEASE_TOLERANCE_FRAMES:
                    self.is_dragging = False
                    self.drag_dropped_frames = 0
                    self.last_drag_end_time = now
                    on_drag_end()


# -----------------------------
# Streaming Pipeline
# -----------------------------
audio_queue = queue.Queue(maxsize=100)

def audio_callback(indata, outdata, frames, time_info, status):
    if status:
        print(status)
    try:
        audio_queue.put_nowait(indata[:, 0].copy())
    except queue.Full:
        pass


def main():
    window = get_window("hann", FFT_SIZE)
    detector = AudioGestureDetector(SAMPLE_RATE, FFT_SIZE, window)

    stream = sd.Stream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        dtype="float32",
        channels=(CHANNELS, CHANNELS),
        device=(INPUT_DEVICE, None),
        callback=audio_callback,
        latency="low",
    )

    print("Listening for gestures...")
    print("  - Double Tap: Toggle App Switcher / Confirm")
    print("  - Tap: Next App")
    print("  - Drag: Previous App")
    print("Press Ctrl+C to stop.")

    with stream:
        try:
            while True:
                try:
                    block = audio_queue.get(timeout=0.01)
                    detector.process_frame(block)
                except queue.Empty:
                    detector.process_frame(np.zeros(BLOCK_SIZE, dtype=np.float32))

        except KeyboardInterrupt:
            if switcher.is_active:
                switcher.select_app()
            print("\nStopped.")


if __name__ == "__main__":
    main()