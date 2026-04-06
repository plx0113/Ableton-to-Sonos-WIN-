"""
Ableton → Sonos LOW-LATENCY Streamer

Streams raw PCM (WAV) instead of MP3 to eliminate encoder latency.
The remaining ~1-2s latency is Sonos firmware buffering — unavoidable
for HTTP streams. For sub-100ms you'd need Sonos Line-In on a Port/Era 300.

Dependencies:
  pip install sounddevice numpy soco

Usage:
  python Ableton2Sonos.py [--speaker "Living Room"] [--port 8090] [--debug]
"""

import argparse
import sounddevice as sd
import numpy as np
import soco
import socket
import struct
import threading
import queue
import time
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─── Configuration ───────────────────────────────────────────────
# These are the main knobs to adjust behaviour without touching code.

SAMPLE_RATE     = 44100  # fallback rate; overridden at runtime by the device's native rate
CHANNELS        = 2      # stereo
BITS_PER_SAMPLE = 16     # standard CD-quality bit depth

# CHUNK_FRAMES: how many audio frames are captured per callback block.
# Larger = less CPU overhead but more capture latency.
# ~46ms at 44100 Hz. Increase to 4096 if you still hear crackling.
CHUNK_FRAMES    = 2048

# SEND_BATCH: how many chunks are bundled into a single HTTP write to Sonos.
# Larger = smoother playback (less crackling) but slightly more latency.
# Increase in steps (3 → 4 → 6 → 8) until crackling stops.
SEND_BATCH      = 6

STREAM_PORT     = 8090   # local HTTP port Sonos connects to

# Target Sonos speaker(s). Comma-separated for multiple, e.g. "Couch,Den".
# If a name is not found, the script warns and falls back to the first speaker found.
TARGET_SPEAKER      = "Couch"

# Fragment matched (case-insensitive) against audio device names to find VB-Cable.
# Change to 'matrix' if using VB-Audio Matrix instead of VB-Cable.
CAPTURE_DEVICE_HINT = 'cable'

# Set to True to print all detected audio input devices on startup.
# Useful for diagnosing device detection issues.
DEBUG = False

ACTIVE_SAMPLE_RATE = SAMPLE_RATE  # set at runtime to the device's actual native rate

# ─── CLI arguments ───────────────────────────────────────────────
# Allows overriding config values at launch without editing the file.
# Example: python Ableton2Sonos.py --speaker "Den" --port 9000 --debug

def parse_args():
    global TARGET_SPEAKER, STREAM_PORT, DEBUG, CAPTURE_DEVICE_HINT
    parser = argparse.ArgumentParser(description='Ableton → Sonos WAV streamer')
    parser.add_argument('--speaker', default=None,
                        help='Sonos speaker name(s), comma-separated')
    parser.add_argument('--port', type=int, default=STREAM_PORT,
                        help=f'HTTP stream port (default: {STREAM_PORT})')
    parser.add_argument('--device', default=CAPTURE_DEVICE_HINT,
                        help=f'Capture device name hint (default: {CAPTURE_DEVICE_HINT!r})')
    parser.add_argument('--debug', action='store_true',
                        help='Print all audio devices on startup')
    args = parser.parse_args()
    if args.speaker:
        TARGET_SPEAKER = args.speaker
    STREAM_PORT         = args.port
    CAPTURE_DEVICE_HINT = args.device
    DEBUG               = args.debug

# ─── Find capture device ─────────────────────────────────────────
# Scans all audio input devices and returns the index, sample rate, and
# channel count of the first device whose name contains CAPTURE_DEVICE_HINT.
#
# WASAPI (Windows Audio Session API) is preferred over MME because:
#   - ASIO4ALL uses WDM-KS which takes exclusive access to the device
#   - MME attempts fail with "unanticipated host error" when ASIO4ALL is active
#   - WASAPI shared mode reads from the Windows audio mix and coexists with ASIO4ALL

def find_capture_device():
    devices  = sd.query_devices()
    hostapis = sd.query_hostapis()

    # Find the index of the WASAPI host API so we can prefer it
    wasapi_idx = next((i for i, api in enumerate(hostapis) if 'WASAPI' in api['name']), None)

    if DEBUG:
        print("  All input devices:")
        for i, dev in enumerate(devices):
            if dev['max_input_channels'] > 0:
                api_name = hostapis[dev['hostapi']]['name']
                print(f"    [{i}] {dev['name']}  sr={int(dev['default_samplerate'])}"
                      f"  ch={dev['max_input_channels']}  api={api_name}")

    # First pass: WASAPI only. Second pass: any host API (fallback).
    for preferred_api in ([wasapi_idx] if wasapi_idx is not None else []) + [None]:
        for i, dev in enumerate(devices):
            name_match = CAPTURE_DEVICE_HINT.lower() in dev['name'].lower()
            api_match  = preferred_api is None or dev['hostapi'] == preferred_api
            if name_match and dev['max_input_channels'] >= 2 and api_match:
                api_name = hostapis[dev['hostapi']]['name']
                print(f"  Found capture device: [{i}] {dev['name']} ({api_name})")
                print(f"    Default samplerate: {int(dev['default_samplerate'])} Hz, "
                      f"max inputs: {dev['max_input_channels']}")
                return i, int(dev['default_samplerate']), dev['max_input_channels']

    print(f"  Capture device matching {CAPTURE_DEVICE_HINT!r} not found!")
    sys.exit(1)

def get_local_ip():
    # Uses a UDP trick: connect to an external address (no data is sent)
    # just to let the OS pick the correct outbound network interface.
    # Returns the LAN IP of this PC, which Sonos will use to reach the stream.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.0.0.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()

# ─── Audio capture ───────────────────────────────────────────────
# audio_q holds captured PCM chunks waiting to be sent to Sonos.
# maxsize=500 acts as a safety buffer (~23s at default settings).
# If it fills up, chunks are dropped rather than blocking the callback.

audio_q    = queue.Queue(maxsize=500)
drop_count = 0  # total chunks dropped due to queue overflow — shown in status line

def audio_callback(indata, frames, time_info, status):
    # Called by sounddevice on a low-latency audio thread for every captured block.
    # Converts float32 samples to int16 PCM and queues them for the HTTP server.
    global drop_count
    if status:
        print(f"  audio: {status}")
    try:
        # Slice to CHANNELS (handles devices with more than 2 channels)
        # Scale float32 [-1.0, 1.0] to int16 [-32767, 32767]
        pcm = (indata[:, :CHANNELS] * 32767).astype(np.int16).tobytes()
        audio_q.put_nowait(pcm)
    except queue.Full:
        # Queue is backed up — HTTP server isn't draining fast enough.
        # Drop the chunk rather than stalling the audio thread.
        drop_count += 1
        if drop_count % 100 == 0:
            print(f"\n  WARNING: audio queue full — {drop_count} chunks dropped "
                  f"(crackling likely; increase SEND_BATCH or CHUNK_FRAMES)")

# ─── WAV header for infinite streaming ───────────────────────────
# Sonos expects a valid WAV header before the PCM data.
# Setting data_size to 0xFFFFFFFF signals an unknown/infinite length,
# which tells Sonos to keep reading indefinitely rather than stopping
# when it thinks the file has ended.

def make_wav_header(sample_rate):
    byte_rate   = sample_rate * CHANNELS * (BITS_PER_SAMPLE // 8)
    block_align = CHANNELS * (BITS_PER_SAMPLE // 8)
    data_size   = 0xFFFFFFFF  # max uint32 = "unknown length" / infinite stream
    file_size   = data_size + 36  # intentionally overflows — fine for streaming

    header   = struct.pack('<4sI4s', b'RIFF', file_size & 0xFFFFFFFF, b'WAVE')
    fmt      = struct.pack('<4sIHHIIHH',
                   b'fmt ', 16, 1, CHANNELS, sample_rate,
                   byte_rate, block_align, BITS_PER_SAMPLE)
    data_hdr = struct.pack('<4sI', b'data', data_size & 0xFFFFFFFF)
    return header + fmt + data_hdr

# ─── HTTP stream server ───────────────────────────────────────────
# A minimal HTTP server that serves a never-ending WAV stream to Sonos.
# Runs in a daemon thread so it doesn't block the main loop.
#
# Connection flow:
#   1. Sonos sends a HEAD request to check the stream exists
#   2. Sonos sends a GET request and keeps the connection open
#   3. Server sends WAV header, then pre-buffers a few chunks to avoid
#      an immediate underrun, then streams PCM indefinitely
#
# SEND_BATCH chunks are bundled per write to reduce syscall overhead.
# If the connection drops (Sonos reboots, network blip), the exception
# is caught silently and Sonos will reconnect automatically.

class StreamHandler(BaseHTTPRequestHandler):

    def do_HEAD(self):
        # Sonos probes the stream with HEAD before committing to a GET.
        # Respond with audio/wav headers so Sonos knows what to expect.
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('icy-name', 'Ableton Stream')
        self.end_headers()

    def do_GET(self):
        # Any path other than /stream.wav returns a plain text status page.
        if self.path != '/stream.wav':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Ableton streamer running. Stream at /stream.wav')
            return

        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.end_headers()

        # One chunk's worth of silence used as a fill when the queue is empty
        silence = b'\x00' * (CHUNK_FRAMES * CHANNELS * (BITS_PER_SAMPLE // 8))

        # Pre-buffer SEND_BATCH chunks before sending anything.
        # This gives Sonos enough data to start without an immediate underrun.
        prebuf = b''
        while len(prebuf) < len(silence) * SEND_BATCH:
            try:
                prebuf += audio_q.get(timeout=1)
            except queue.Empty:
                prebuf += silence  # fill with silence if audio hasn't started yet

        self.wfile.write(make_wav_header(ACTIVE_SAMPLE_RATE))
        self.wfile.write(prebuf)
        self.wfile.flush()

        # Main streaming loop: drain SEND_BATCH chunks per write.
        # batch_timeout ensures we don't stall longer than 1.5 chunk durations
        # before substituting silence — keeps the stream continuous.
        chunk_duration = CHUNK_FRAMES / ACTIVE_SAMPLE_RATE  # seconds per chunk
        batch_timeout  = chunk_duration * 1.5
        try:
            while True:
                batch = b''
                for _ in range(SEND_BATCH):
                    try:
                        batch += audio_q.get(timeout=batch_timeout)
                    except queue.Empty:
                        batch += silence  # send silence rather than stalling
                self.wfile.write(batch)
                self.wfile.flush()
        except Exception:
            pass  # Sonos disconnected — server stays alive for reconnect

    def log_message(self, format, *args):
        # Only log actual stream connections, not every HTTP probe
        if 'GET /stream.wav' in (args[0] if args else ''):
            print(f"  Sonos connected: {self.client_address[0]}")

    def handle_error(self, request, client_address):
        pass  # Sonos frequently probes and drops connections — suppress the noise

# ─── Sonos control ───────────────────────────────────────────────
# Discovers all Sonos speakers on the LAN, resolves the target speaker(s),
# joins multiple speakers into a group if needed, then tells the coordinator
# to play the local HTTP stream.
#
# If TARGET_SPEAKER lists multiple names (e.g. "Couch,Den"), the first
# speaker becomes the group primary and the rest join its group before
# playback starts — ensuring all speakers play in sync.

def start_sonos(stream_url):
    print("\n  Discovering Sonos…")
    discovered = soco.discover(timeout=10)
    if not discovered:
        print("  No Sonos speakers found!")
        sys.exit(1)

    speakers = {s.player_name: s for s in discovered}
    print(f"  Found: {sorted(speakers.keys())}")

    coordinator = None

    if TARGET_SPEAKER:
        targets = []
        for name in [n.strip() for n in TARGET_SPEAKER.split(',')]:
            if name in speakers:
                targets.append(speakers[name])
            else:
                print(f"  WARNING: speaker {name!r} not found, skipping")
        if targets:
            primary = targets[0]
            # Join additional speakers into the primary's group before playing
            for speaker in targets[1:]:
                speaker.join(primary)
                print(f"  Joined {speaker.player_name} → {primary.player_name}'s group")
            # Use the group coordinator (may differ from primary if already grouped)
            coordinator = primary.group.coordinator

    # Fall back to the first discovered speaker if nothing matched
    if coordinator is None:
        coordinator = list(discovered)[0].group.coordinator

    coordinator.play_uri(stream_url, title="Ableton Live")
    print(f"  Playback started on: {coordinator.player_name} "
          f"(group: {[s.player_name for s in coordinator.group.members]})")
    return [coordinator]

# ─── Sonos watchdog ──────────────────────────────────────────────
# Runs in a background thread and polls each coordinator every 5 seconds.
# If a speaker has stopped (e.g. due to a network blip, Sonos app interaction,
# or speaker going to sleep), it automatically resumes the stream.
# Errors during the check are silently ignored — it will retry next cycle.

def sonos_watchdog(coordinators, stream_url, stop_event):
    while not stop_event.is_set():
        time.sleep(5)
        for coordinator in coordinators:
            try:
                info  = coordinator.get_current_transport_info()
                state = info.get('current_transport_state', '')
                if state not in ('PLAYING', 'TRANSITIONING'):
                    print(f"\n  Watchdog: {coordinator.player_name} stopped ({state}), resuming…")
                    coordinator.play_uri(stream_url, title="Ableton Live")
            except Exception:
                pass  # network blip — try again next cycle

# ─── Main ────────────────────────────────────────────────────────

def main():
    parse_args()

    print("=" * 50)
    print("  Ableton → Sonos (Low-Latency WAV)")
    print("=" * 50)

    global ACTIVE_SAMPLE_RATE
    device_idx, device_rate, device_channels = find_capture_device()
    ACTIVE_SAMPLE_RATE = device_rate  # store for use in WAV header and timeout calc
    local_ip   = get_local_ip()
    stream_url = f"http://{local_ip}:{STREAM_PORT}/stream.wav"

    print(f"\n  PC IP:        {local_ip}")
    print(f"  Stream:       {stream_url}")
    print(f"  Sample rate:  {device_rate} Hz")
    print(f"  Chunk:        {CHUNK_FRAMES} frames ({CHUNK_FRAMES/device_rate*1000:.1f}ms)")

    # Try opening the capture device with progressively safer configurations.
    # WASAPI low latency is ideal; high latency is more compatible with some drivers.
    # Falling back to device_channels handles devices that reject a fixed channel count.
    attempts = [
        dict(channels=2,               latency='low'),
        dict(channels=2,               latency='high'),
        dict(channels=device_channels, latency='high'),
    ]
    audio_stream = None
    for attempt in attempts:
        ch, lat = attempt['channels'], attempt['latency']
        print(f"\n  Opening device: ch={ch}, latency={lat!r}…")
        try:
            audio_stream = sd.InputStream(
                device=device_idx,
                samplerate=device_rate,
                channels=ch,
                blocksize=CHUNK_FRAMES,  # number of frames per callback
                dtype='float32',         # sounddevice normalises to [-1.0, 1.0]
                callback=audio_callback,
                latency=lat,
            )
            audio_stream.start()
            print("  OK")
            break
        except sd.PortAudioError as e:
            print(f"  Failed: {e}")
            audio_stream = None

    if audio_stream is None:
        print(f"\n  ERROR: Could not open capture device with any config.")
        print(f"  Fix: Right-click speaker icon → Sounds → Recording tab")
        print(f"       → CABLE Output → Properties → Advanced → set to {device_rate} Hz, 2 channel")
        sys.exit(1)

    # Start the HTTP server in a daemon thread.
    # daemon=True means it shuts down automatically when the main thread exits.
    print(f"\n  Starting WAV stream server on :{STREAM_PORT}…")
    server = HTTPServer(('0.0.0.0', STREAM_PORT), StreamHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)  # give the server a moment to bind before Sonos tries to connect

    # Tell Sonos to start playing the stream
    coordinators = start_sonos(stream_url)

    # start_event shared between watchdog and main — set on Ctrl+C to stop all threads
    stop_event = threading.Event()
    watchdog_thread = threading.Thread(
        target=sonos_watchdog, args=(coordinators, stream_url, stop_event), daemon=True)
    watchdog_thread.start()

    print("\n" + "=" * 50)
    print("  Streaming! Press Ctrl+C to stop.")
    print("=" * 50 + "\n")

    # Status line: updates in place every second using \r (carriage return).
    # Queue depth and drop count help diagnose crackling without stopping the stream.
    start_time = time.time()
    try:
        while True:
            elapsed = int(time.time() - start_time)
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            print(f"  [OK] Queue: {audio_q.qsize():>3}/500 | Dropped: {drop_count} | "
                  f"Uptime: {h:02d}:{m:02d}:{s:02d}", end='\r')
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopping…")
        stop_event.set()
        for coordinator in coordinators:
            try:
                coordinator.stop()
            except Exception:
                pass
        audio_stream.stop()
        audio_stream.close()
        server.shutdown()
        print("  Done.")

if __name__ == '__main__':
    main()
