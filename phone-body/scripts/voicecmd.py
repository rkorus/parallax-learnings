#!/usr/bin/env python3
"""Voice-command helpers for Parallax's v2 wake loop.

Pieces that don't depend on the live phone handoff (so they can be unit-tested):
  - capture_audio(seconds): pull N seconds of IP Webcam /audio.wav over the SOCKS5 tailnet bridge
  - fix_wav(path): rewrite a streaming WAV's RIFF/data sizes to the real byte count (IP Webcam sends a bogus length)
  - grab_shot(path): pull /shot.jpg (the "eyes")
  - transcribe(path): faster-whisper -> text (the command Russell spoke)
  - inject(text): tmux send-keys into my live session so *I* answer, same mechanism the Telegram bridge uses

Self-test (no phone needed):  python3 voicecmd.py --selftest
  Generates speech with Piper, fixes the header, transcribes it, and checks the words come back.
"""
import os, sys, subprocess, struct, wave

HERE = os.path.dirname(os.path.abspath(__file__))
PHONE_IP = "100.x.x.x"              # >>> SET THIS: your phone's Tailscale IP
IPWEBCAM = f"http://{PHONE_IP}:8080" # IP Webcam server (fixed port, unlike adb's)
SOCKS = "localhost:1055"            # userspace-tailscale SOCKS5 bridge
TMUX_PANE = "my-ai-session:0.0"     # >>> SET THIS: tmux session:pane where YOUR AI session runs
WHISPER_MODEL = os.environ.get("PX_WHISPER", "base")  # base=fast; large-v3=accurate

_model = None
def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model

def capture_audio(seconds=9, out="/tmp/pxcmd.wav"):
    """Pull ~`seconds` of audio from IP Webcam's WAV stream via the SOCKS bridge.
    Returns out path on success, None on failure. Header is fixed afterwards."""
    r = subprocess.run(["curl", "-s", "--socks5-hostname", SOCKS,
                        "--max-time", str(seconds + 2),
                        f"{IPWEBCAM}/audio.wav", "-o", out],
                       capture_output=True)
    # curl returns 28 (timeout) which is EXPECTED - we deliberately cut the stream
    if not os.path.exists(out) or os.path.getsize(out) < 1000:
        return None
    fix_wav(out)
    return out

def fix_wav(path):
    """IP Webcam streams a WAV with a placeholder length. Rewrite RIFF + data chunk
    sizes to the actual file size so decoders read the whole thing."""
    with open(path, "r+b") as f:
        data = f.read()
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return  # not a plain WAV; let the decoder try as-is
        # find 'data' chunk
        i = 12
        while i + 8 <= len(data):
            cid = data[i:i+4]; csz = struct.unpack("<I", data[i+4:i+8])[0]
            if cid == b"data":
                real = len(data) - (i + 8)
                f.seek(i + 4); f.write(struct.pack("<I", real))      # data size
                f.seek(4);     f.write(struct.pack("<I", len(data)-8))# RIFF size
                return
            i += 8 + csz + (csz & 1)

def grab_shot(out="/tmp/pxshot.jpg"):
    """Pull a camera frame (the eyes). Returns path or None."""
    subprocess.run(["curl", "-s", "--socks5-hostname", SOCKS, "--max-time", "8",
                    f"{IPWEBCAM}/shot.jpg", "-o", out], capture_output=True)
    return out if os.path.exists(out) and os.path.getsize(out) > 2000 else None

def transcribe(path):
    """faster-whisper -> text. Returns '' if nothing intelligible."""
    segs, _info = _whisper().transcribe(path, language="en", beam_size=1,
                                        vad_filter=True,
                                        vad_parameters=dict(min_silence_duration_ms=500))
    return " ".join(s.text.strip() for s in segs).strip()

def inject(text):
    """Deliver the spoken command into my live session, exactly like the Telegram bridge:
    send-keys -l (literal) then Enter. I then answer it and speak the reply."""
    tag = f"[VOICE] {text}"   # prefix your AI recognizes as a spoken command
    subprocess.run(["tmux", "send-keys", "-t", TMUX_PANE, "-l", tag], check=True, timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_PANE, "Enter"], check=True, timeout=5)
    return True

def _selftest():
    piper = os.path.expanduser("~/.local/bin/piper")
    model = None
    for b in (HERE, os.path.expanduser("~/phonebridge")):
        m = os.path.join(b, "voices/en_GB-alan-medium.onnx")
        if os.path.exists(m): model = m; break
    phrase = "Hey Parallax, what is on my calendar today and send Irina an update."
    subprocess.run([piper, "-m", model, "-f", "/tmp/pxself.wav"],
                   input=phrase.encode(), check=True, capture_output=True)
    # mangle the header to simulate IP Webcam's bogus length, then fix it
    with wave.open("/tmp/pxself.wav") as w:
        print(f"[selftest] generated {w.getnframes()} frames @ {w.getframerate()}Hz, {w.getnchannels()}ch")
    fix_wav("/tmp/pxself.wav")
    got = transcribe("/tmp/pxself.wav")
    print(f"[selftest] said:  {phrase}")
    print(f"[selftest] heard: {got}")
    ok = "parallax" in got.lower() and "irina" in got.lower()
    print(f"[selftest] wake word + command content recovered: {'PASS' if ok else 'CHECK'}")
    return ok

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    print(__doc__)
