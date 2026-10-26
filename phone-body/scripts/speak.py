#!/usr/bin/env python3
"""Parallax speaks. LOCKED voice = en_GB-alan-medium (British male) @ length-scale 0.74 (1.35x).

Usage:
  speak.py "text to say"              -> plays through the phone's speaker (adb)
  speak.py --telegram "text"          -> sends as a Telegram voice message to Russell
  echo "text" | speak.py              -> text from stdin

Pipeline: piper (WAV) -> PyAV (mp3 for phone / opus for Telegram) -> adb push + am start / sendVoice.
No system ffmpeg/lame required (PyAV bundles the codecs). TG creds read from config, never hardcoded.
"""
import sys, os, subprocess, av

HERE = os.path.dirname(os.path.abspath(__file__))
PIPER = os.path.expanduser("~/.local/bin/piper")
# model + adb resolve from this script's dir first, else the durable ~/phonebridge
def _find(rel):
    for base in (HERE, os.path.expanduser("~/phonebridge")):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.expanduser("~/phonebridge"), rel)
MODEL = _find("voices/en_GB-alan-medium.onnx")  # >>> SET THIS: your Piper voice (.onnx) in a voices/ dir
def _adb_target():
    try:
        out = subprocess.check_output([_find("platform-tools/adb"), "devices"], text=True, timeout=10)
        for ln in out.splitlines():
            if ln.startswith("localhost:") and "\tdevice" in ln:
                return ln.split()[0]
    except Exception:
        pass
    return "localhost:5555"   # >>> SET THIS fallback: your fixed adb port (see §4 'adb tcpip 5555')
ADB = [_find("platform-tools/adb"), "-s", _adb_target()]
LENGTH_SCALE = "0.74"            # 1.35x speaking pace, locked by Russell 2026-10-24

def _tg_creds():
    import json
    for p in ("/home/parallax/child-civ-template/config/telegram_config.json",
              os.path.join(HERE, "telegram_config.json")):
        try:
            c = json.load(open(p))
            return c["bot_token"], str(c["chat_id"])
        except Exception:
            continue
    return None, None

def gen_wav(text, wav="/tmp/pxspeak.wav"):
    subprocess.run([PIPER, "-m", MODEL, "--length-scale", LENGTH_SCALE, "-f", wav],
                   input=text.encode(), check=True, capture_output=True)
    return wav

def transcode(wav, out, codec, fmt, layout, rate):
    inp = av.open(wav)
    oc = av.open(out, "w", format=fmt) if fmt else av.open(out, "w")
    ost = oc.add_stream(codec, rate=rate)
    try: ost.layout = layout
    except Exception: pass
    rs = av.AudioResampler(format="s16" if codec == "libopus" else "s16p", layout=layout, rate=rate)
    for fr in inp.decode(inp.streams.audio[0]):
        fr.pts = None
        for rf in (lambda r: r if isinstance(r, list) else [r])(rs.resample(fr)):
            for p in ost.encode(rf): oc.mux(p)
    for p in ost.encode(None): oc.mux(p)
    oc.close(); inp.close()
    return out

def _mark_speaking(wav, text=""):
    """Tell the ear daemon the mic is about to carry MY voice, so it ignores it
    (prevents the voice loop from transcribing its own replies and self-triggering).
    Two signals: a mute window (generous, to cover VLC's startup latency) AND the text I'm
    speaking, so the daemon can drop any utterance that echoes it even if the timing slips."""
    try:
        import wave, time
        with wave.open(wav) as w:
            dur = w.getnframes() / float(w.getframerate() or 22050)
        with open("/tmp/px_mute_until", "w") as f:
            # SHORT window - just covers VLC launch + playback. A long window eats the user's reply
            # that comes right after. The text-based self-echo guard (px_last_spoken) catches any
            # tail that leaks past this.
            f.write(str(time.time() + dur + 2.5))
        if text:
            with open("/tmp/px_last_spoken", "w") as f:
                f.write(text)
    except Exception:
        pass

def play_phone(text):
    wav = gen_wav(text)
    _mark_speaking(wav, text)
    mp3 = transcode(wav, "/tmp/pxspeak.mp3", "mp3", None, "stereo", 44100)
    subprocess.run(ADB + ["push", mp3, "/sdcard/Download/pxspeak.mp3"], check=True, capture_output=True)
    # Route to VLC explicitly: the phone's default audio/mpeg handler (YouTube Music) only plays a
    # ~2.5s PREVIEW of a local file, truncating real answers. VLC plays the whole file. (Verified
    # 2026-10-26.) VLC also coexists with IP Webcam's background streaming so the ears stay alive.
    subprocess.run(ADB + ["shell", "am", "start", "-a", "android.intent.action.VIEW",
                          "-d", "file:///sdcard/Download/pxspeak.mp3", "-t", "audio/mpeg",
                          "-n", "org.videolan.vlc/org.videolan.vlc.StartActivity"],
                   check=True, capture_output=True)
    print("[speak] played on phone:", text[:70])

def send_telegram(text):
    token, chat = _tg_creds()
    if not token:
        print("[speak] no Telegram creds in config; cannot send"); return
    wav = gen_wav(text)
    ogg = transcode(wav, "/tmp/pxspeak.ogg", "libopus", "ogg", "mono", 48000)
    r = subprocess.run(["curl", "-s", "--max-time", "30",
        f"https://api.telegram.org/bot{token}/sendVoice",
        "-F", f"chat_id={chat}", "-F", f"voice=@{ogg}"], capture_output=True, text=True)
    print("[speak] telegram voice:", "sent" if '"ok":true' in r.stdout else "FAIL " + r.stdout[:120])

if __name__ == "__main__":
    args = sys.argv[1:]
    tg = "--telegram" in args
    args = [a for a in args if a != "--telegram"]
    text = args[0] if args else sys.stdin.read().strip()
    if not text:
        print("usage: speak.py [--telegram] \"text\""); sys.exit(1)
    (send_telegram if tg else play_phone)(text)
