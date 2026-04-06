# Ableton to Sonos Streamer

Stream audio from Ableton Live to Sonos speakers over your local network using Python.

## How It Works

Ableton outputs to a virtual audio cable. Python captures that audio, serves it as a WAV stream on a local HTTP server, and tells your Sonos speaker to play from it. The stream is raw uncompressed PCM inside a WAV container — no encoding overhead.

## Requirements

- Windows PC
- Python 3.10+
- Ableton Live
- Sonos speaker(s) on the same network as your PC
- [VB-Cable](https://vb-audio.com/Cable/) or VB-Audio Matrix (free virtual audio cable)
- [ASIO4ALL](https://www.asio4all.org/) (free ASIO driver)

## Install

1. Install VB-Cable from the link above and restart your PC.
2. Install Python dependencies:

```
pip install sounddevice numpy soco
```

## Audio Routing Setup

1. Open Ableton and go to **Options > Preferences > Audio**.
2. Set **Driver Type** to ASIO and select **ASIO4ALL v2**.
3. Open the ASIO4ALL control panel (wrench icon in Ableton's audio prefs).
4. Enable **Virtual Cable 1** as an output under the Virtual Audio Cable section.
5. Disable any other output devices you don't need.

If you want to monitor on headphones while streaming to Sonos, enable your Realtek output alongside VB-Cable in ASIO4ALL. Set Ableton's **Master Out** to the VB-Cable channels and **Cue Out** to the Realtek channels.

## Run

```
python Ableton2Sonos.py
```

The script will:

1. Find VB-Cable automatically
2. Detect your PC's local IP
3. Start a WAV stream server on port 8090
4. Discover your Sonos speakers
5. Start playback on the configured speaker(s)

Play something in Ableton and you should hear it on your Sonos speakers.

### Startup Output

When the script runs successfully you will see something like this:

```
==================================================
  Ableton → Sonos (Low-Latency WAV)
==================================================
  Found capture device: [3] CABLE Output (VB-Audio Virtual C) (Windows WASAPI)
    Default samplerate: 44100 Hz, max inputs: 2

  PC IP:        192.168.1.45
  Stream:       http://192.168.1.45:8090/stream.wav
  Sample rate:  44100 Hz
  Chunk:        2048 frames (46.4ms)

  Opening device: ch=2, latency='low'…
  OK

  Starting WAV stream server on :8090…

  Discovering Sonos…
  Found: ['Couch', 'Den', 'Dining']
  Playback started on: Couch (group: ['Couch'])

==================================================
  Streaming! Press Ctrl+C to stop.
==================================================

  [OK] Queue:  12/500 | Dropped: 0 | Uptime: 00:00:08
```

- **Found capture device** — confirms VB-Cable was detected and which host API is being used (WASAPI is correct)
- **Stream URL** — the address Sonos connects to; you can open it in a browser to verify the audio capture is working
- **Opening device** — the script tries `latency='low'` first, falling back to safer configs if it fails
- **Discovering Sonos** — lists all speakers found on the network before starting playback
- **Status line** — updates every second while streaming; `Dropped: 0` means no crackling should be heard

## Command Line Options

You can override configuration at launch without editing the script:

```
python Ableton2Sonos.py --speaker "Den" --port 9000 --debug
```

| Flag | Description |
|---|---|
| `--speaker` | Sonos speaker name(s), comma-separated (e.g. `"Couch,Den"`) |
| `--port` | HTTP stream port (default: `8090`) |
| `--device` | Capture device name hint (default: `cable`) |
| `--debug` | Print all detected audio input devices on startup |

## Configuration

At the top of the script you can set permanent defaults:

| Variable | Default | Description |
|---|---|---|
| `TARGET_SPEAKER` | `"Couch"` | Sonos speaker name(s), comma-separated. `None` = first found |
| `CAPTURE_DEVICE_HINT` | `'cable'` | Name fragment to match capture device. Use `'matrix'` for VB-Audio Matrix |
| `STREAM_PORT` | `8090` | HTTP port the stream server listens on |
| `CHUNK_FRAMES` | `2048` | Audio capture block size in frames (~46ms). Increase if crackling persists |
| `SEND_BATCH` | `6` | Chunks bundled per HTTP write. Increase in steps (3→4→6→8) to fix crackling |
| `DEBUG` | `False` | Print all audio input devices on startup |

## Multi-Speaker Support

Set `TARGET_SPEAKER` to a comma-separated list to stream to multiple speakers:

```python
TARGET_SPEAKER = "Couch,Den"
```

The script joins all listed speakers into a Sonos group before starting playback, so they play in sync. If a speaker name isn't found, a warning is printed and that speaker is skipped.

## Fixing Crackling

If you hear crackling or dropouts, increase `SEND_BATCH` in steps until it's clean:

```
SEND_BATCH = 3  →  4  →  6  →  8
```

This bundles more audio data per HTTP write, giving Sonos more to buffer against network jitter. If crackling persists above `8`, also increase `CHUNK_FRAMES` to `4096` — but note this adds a small amount of capture latency.

The status line shows `Queue` depth and `Dropped` chunk count in real time, which helps diagnose whether crackling is a buffering issue.

## ASIO4ALL Compatibility

The script uses **WASAPI shared mode** to capture audio from VB-Cable. This coexists with ASIO4ALL, which holds exclusive access to the device via WDM-KS. The older MME mode fails with `unanticipated host error` when ASIO4ALL is active — WASAPI avoids this.

If the script fails to open the device, it automatically retries with progressively safer configurations (stereo low latency → stereo high latency → native channel count).

## Auto-Reconnect (Watchdog)

A background thread checks every 5 seconds whether each Sonos speaker is still playing. If a speaker stops due to a network blip, Sonos app interaction, or going to sleep, it automatically resumes the stream without needing a restart.

## Status Line

While streaming, the console shows a live status line:

```
[OK] Queue:  42/500 | Dropped: 0 | Uptime: 00:04:32
```

- **Queue** — audio chunks waiting to be sent. If this is frequently near 500, increase `SEND_BATCH`.
- **Dropped** — chunks discarded because the queue was full. Any non-zero value here indicates crackling.
- **Uptime** — time since streaming started.

## Sonos Stereo Pair (Optional)

If you want left/right stereo from two speakers, pair them in the Sonos app first:

**Settings > System > (pick a room) > Create Stereo Pair**

This is handled entirely by Sonos. The script targets one logical speaker per group.

## Latency

There is roughly 1–2 seconds of delay. This is caused by Sonos's internal firmware buffering and cannot be reduced through software alone. The delay is consistent — you can work around it by triggering clips slightly early.

The only way to get below ~170ms is a **Sonos Era 300 or Sonos Port** (both have a physical line-in jack). A cable from your PC's audio output to the line-in bypasses HTTP streaming entirely.

## Troubleshooting

**No speakers found** — Make sure your PC and Sonos are on the same subnet. Check that Windows Firewall allowed Python when it prompted.

**No audio** — Open `http://localhost:8090/stream.wav` in a browser. If you hear audio there, the capture is working and the issue is Sonos reaching your PC (firewall). If you hear nothing, check your ASIO4ALL routing.

**MME error / device unavailable** — ASIO4ALL has exclusive access to VB-Cable. The script automatically uses WASAPI to work around this. If it still fails, check that VB-Cable's sample rate in Windows Sound settings matches what ASIO4ALL is using.

**ConnectionResetError in the console** — Normal. Sonos probes the stream before settling into a stable connection. Playback still works.

**Wrong network adapter detected** — If the script picks up a VPN or wrong adapter IP, hardcode your LAN IP directly in `get_local_ip()` or set the stream URL manually.

**Speaker not found by name** — Run with `--debug` to see all discovered speakers and check for exact name spelling (case-sensitive).
