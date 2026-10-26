#!/usr/bin/env python3
"""parallax_ear.py - Parallax's continuous voice loop (v2).

IP Webcam is the sole mic owner and streams audio continuously. This daemon reads that
stream, segments it into utterances with a simple energy VAD, transcribes each with
faster-whisper, and listens for the wake word "parallax". On a wake:
  - if the same utterance carried a command ("hey parallax, what's on my calendar") it uses it;
  - if it was just the wake word, it says "Russell, I'm here. Go ahead." and takes the NEXT utterance.
It INJECTS the command into my live session the same way the Telegram bridge does, so *I* answer it
and speak the reply through the phone. Conversation mode keeps listening for follow-ups after each
exchange (no re-wake needed); an instant pre-cached ack plays the moment a command lands (masks the
think-time); barge-in stops a current answer when a new command/wake comes in.

Self-mute: speak.py writes /tmp/px_mute_until (short) + /tmp/px_last_spoken (what I said); the loop
ignores utterances inside that window OR that echo my own words (is_self_echo), so it never
transcribes its own voice but never blocks the user's reply either.

Modes:
  python3 parallax_ear.py                 # live: stream from IP Webcam
  python3 parallax_ear.py --test-file f.wav  # offline: run the segment+wake logic on a wav
"""
import os, sys, time, subprocess, struct, wave, io, re, difflib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voicecmd as V   # capture/transcribe/inject/grab_shot/fix_wav + config

MUTE_FILE = "/tmp/px_mute_until"
LOG = HERE + "/parallax_ear.log"
ADB = [HERE + "/platform-tools/adb", "-s", "localhost:5555"]   # >>> SET THIS: your fixed adb port
# >>> SET THIS: your AI's wake name + the ways Whisper mis-spells it (say it a few times, read the
# log, add whatever shows up). ALSO update the difflib targets in _is_wake() below. "parallax" = example.
WAKE_VARIANTS = ("parallax", "para lax", "parallx", "parallex", "paralax",
                 "pair a lax", "parawax", "parallux", "paralux")
END_SILENCE = 0.55    # seconds of quiet that ends an utterance (lower = snappier response)
MIN_UTTER = 0.35      # ignore blips shorter than this
MAX_UTTER = 14.0      # force-cut a very long utterance
FRAME_MS = 20
COOLDOWN = 6.0        # min seconds between two wake actions

def log(m):
    line = f"{time.strftime('%FT%TZ', time.gmtime())} {m}"
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

def muted():
    try:
        return time.time() < float(open(MUTE_FILE).read().strip())
    except Exception:
        return False

def is_self_echo(text):
    """True if `text` is my own last spoken reply bouncing back off the phone speaker into the mic.
    This is the PRIMARY echo filter (the mute window is deliberately short so it doesn't eat the
    user's reply). Catches whole-reply echoes (high ratio) AND partial/tail echoes (most of the
    utterance's words appear in what I just said)."""
    try:
        last = open("/tmp/px_last_spoken").read().strip().lower()
    except Exception:
        return False
    if not last:
        return False
    t = text.lower()
    if difflib.SequenceMatcher(None, t, last).ratio() > 0.55:
        return True
    tw = [w for w in re.findall(r"[a-z']+", t) if len(w) > 2]
    if len(tw) >= 3 and sum(1 for w in tw if w in last) / len(tw) >= 0.8:
        return True
    return False

def speak(text):
    # PHONE SPEAKER output (native conversation). This is safe now that IP Webcam's
    # "Allow streaming in background" + overlay permission are set: the server survives while audio
    # plays (verified 2026-10-26, Russell heard it + server stayed 200). speak.py writes
    # /tmp/px_mute_until so the loop ignores my own voice. (Fallback: add "--telegram" if the phone
    # ever starts eating the stream again.)
    subprocess.run(["python3", HERE + "/speak.py", text], capture_output=True)

def stop_playback():
    """Cut off any TTS currently playing (barge-in): force-stop VLC so a new command or a
    fresh 'Hey Parallax' interrupts my current answer instead of waiting it out. Also clears the
    mute so I start listening immediately."""
    try:
        subprocess.run(ADB + ["shell", "am", "force-stop", "org.videolan.vlc"],
                       capture_output=True, timeout=8)
        import os as _os
        try: _os.remove(MUTE_FILE)
        except Exception: pass
    except Exception as e:
        log(f"stop_playback error: {e}")

ACK_PHONE = "/sdcard/Download/px_ack.mp3"
ACK_TEXT = "One moment."
_ack_ready = [False]

def setup_ack():
    """Generate + push the instant-ack audio ONCE, so wake-time playback skips gen/transcode/push.
    Called when the stream first opens (phone reachable). Idempotent."""
    if _ack_ready[0]:
        return
    try:
        sys.path.insert(0, HERE)
        import speak as S
        wav = S.gen_wav(ACK_TEXT, "/tmp/px_ack.wav")
        S.transcode(wav, "/tmp/px_ack.mp3", "mp3", None, "stereo", 44100)
        r = subprocess.run(ADB + ["push", "/tmp/px_ack.mp3", ACK_PHONE], capture_output=True, timeout=15)
        _ack_ready[0] = (r.returncode == 0)
        log(f"instant-ack {'ready' if _ack_ready[0] else 'push failed'}")
    except Exception as e:
        log(f"setup_ack error: {e}")

def play_ack():
    """Play the pre-pushed instant-ack (fast - no gen/transcode/push) to confirm I heard, masking
    the round-trip while I think. Short self-mute so I don't transcribe it."""
    if not _ack_ready[0]:
        return
    try:
        with open(MUTE_FILE, "w") as f: f.write(str(time.time() + 3.0))
        with open("/tmp/px_last_spoken", "w") as f: f.write(ACK_TEXT.lower())
        subprocess.run(ADB + ["shell", "am", "start", "-a", "android.intent.action.VIEW",
                              "-d", f"file://{ACK_PHONE}", "-t", "audio/mpeg",
                              "-n", "org.videolan.vlc/org.videolan.vlc.StartActivity"],
                       capture_output=True, timeout=8)
    except Exception as e:
        log(f"play_ack error: {e}")

def _words(text):
    return re.findall(r"[a-z']+", text.lower())

# NOTE: do NOT block "parallel"/"parallels" - Whisper frequently transcribes the spoken
# wake word "Parallax" AS "parallel" (or "Parallel X"). Missing the wake >> a rare false fire.
WAKE_BLOCK = {"paralegal", "parable", "parables", "parallelogram"}

def _is_wake(tok):
    """Fuzzy: does this token sound like 'parallax'? (Whisper spells it many ways:
    Parallax/Paralox/Paralax/Parallux...). Edit-distance ratio beats any fixed list.
    A small blocklist rejects real words that score close (e.g. 'parallel')."""
    if len(tok) < 5 or tok in WAKE_BLOCK:
        return False
    if tok in WAKE_VARIANTS:
        return True
    return max(difflib.SequenceMatcher(None, tok, t).ratio()
               for t in ("parallax", "parallux", "paralax")) >= 0.72

def _wake_index(ws):
    """Return (start_index, span) of the wake word in a word list, or None.
    Joins up to 3 tokens to catch 'para lax' / 'pair a lax'."""
    for i, w in enumerate(ws):
        if _is_wake(w):
            return i, 1
        if i + 1 < len(ws) and _is_wake(w + ws[i+1]):
            return i, 2
        if i + 2 < len(ws) and _is_wake(w + ws[i+1] + ws[i+2]):
            return i, 3
    return None

def has_wake(text):
    return _wake_index(_words(text)) is not None

def strip_wake(text):
    """Return the command portion after the wake word, '' if only the wake word was said."""
    ws = _words(text)
    hit = _wake_index(ws)
    if hit is None:
        return text.strip()   # no wake word found (caller decides)
    i, span = hit
    rest = ws[i + span:]
    # drop a stray leading token left over when Whisper splits "Parallax" -> "parallel x"/"...ax"
    if rest and rest[0] in ("x", "ex", "axe", "ax", "lax", "lux"):
        rest = rest[1:]
    return " ".join(rest).strip()

# ---- audio segmentation ----
class Segmenter:
    """Feed it int16 mono PCM frames; it yields complete utterances (bytes)."""
    def __init__(self, rate):
        self.rate = rate
        self.fbytes = int(rate * FRAME_MS / 1000) * 2
        self.baseline = None
        self.cur = bytearray()
        self.in_speech = False
        self.sil = 0.0
        self.spoke = 0.0

    def _rms(self, frame):
        n = len(frame) // 2
        if n == 0: return 0.0
        vals = struct.unpack("<%dh" % n, frame[:n*2])
        return (sum(v*v for v in vals) / n) ** 0.5

    def push(self, frame):
        rms = self._rms(frame)
        # adaptive noise floor: slow EMA of quiet frames
        if self.baseline is None:
            self.baseline = rms + 1
        thr = max(300.0, self.baseline * 3.0)
        voiced = rms > thr
        if not voiced:
            self.baseline = 0.98 * self.baseline + 0.02 * rms
        dt = FRAME_MS / 1000.0
        out = None
        if voiced:
            self.in_speech = True
            self.sil = 0.0
            self.spoke += dt
            self.cur += frame
        elif self.in_speech:
            self.sil += dt
            self.cur += frame
            if self.sil >= END_SILENCE:
                if self.spoke >= MIN_UTTER:
                    out = bytes(self.cur)
                self._reset()
        if self.in_speech and (len(self.cur) / self.fbytes) * dt >= MAX_UTTER:
            out = bytes(self.cur); self._reset()
        return out

    def _reset(self):
        self.cur = bytearray(); self.in_speech = False; self.sil = 0.0; self.spoke = 0.0

def write_wav(pcm, rate, path="/tmp/pxutter.wav"):
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm)
    return path

# ---- wake handling ----
_last_action = [0.0]
_awaiting_command = [False]
_conversation_until = [0.0]       # stay in conversation (no wake word needed) until this time
CONV_WINDOW = 60.0                # seconds to keep listening for a follow-up after each exchange
                                  # (answers play on the phone + self-mute, so give a generous window)
EXIT_PHRASES = ("that's all", "thats all", "that's it", "thanks parallax", "thank you parallax",
                "stop listening", "goodbye", "go to sleep", "never mind", "let's end", "lets end",
                "let's wrap up", "we're done", "were done", "that's enough", "thats enough")

def _is_exit(text):
    low = text.lower()
    return any(p in low for p in EXIT_PHRASES)

def handle_utterance(pcm, rate):
    if muted():
        return
    path = write_wav(pcm, rate)
    text = V.transcribe(path)
    if not text:
        return
    log(f"heard: {text!r}")
    if is_self_echo(text):
        log("ignored self-echo (my own reply off the speaker)")
        return
    now = time.time()
    in_conv = now < _conversation_until[0]
    # follow-up to a bare "Hey Parallax" (we prompted, now the command)
    if _awaiting_command[0] and (now - _last_action[0] < 20):
        _awaiting_command[0] = False
        act_on_command(text)
        return
    if has_wake(text):
        if now - _last_action[0] < COOLDOWN:
            return
        stop_playback()   # a fresh "Hey Parallax" mid-answer interrupts it
        cmd = strip_wake(text)
        if len(cmd.split()) >= 2:
            act_on_command(cmd)
        else:
            _last_action[0] = now
            _awaiting_command[0] = True
            log("wake only -> prompting for command")
            speak("Russell, I'm here. Go ahead.")
        return
    # CONVERSATION MODE: within the window after an exchange, no wake word needed
    if in_conv and len(text.split()) >= 2:
        if now - _last_action[0] < COOLDOWN:
            return
        if _is_exit(text):
            _conversation_until[0] = 0.0
            _awaiting_command[0] = False
            log("conversation ended by exit phrase")
            speak("Okay. Just say Hey Parallax when you need me.")
            return
        log(f"conversation follow-up: {text!r}")
        act_on_command(text)
        return

def act_on_command(cmd):
    stop_playback()   # BARGE-IN: cut off any answer I'm mid-way through playing
    play_ack()        # INSTANT feedback ("One moment.") while I think - masks round-trip latency
    now = time.time()
    _last_action[0] = now
    _conversation_until[0] = now + CONV_WINDOW   # keep listening for the next thing he says
    log(f"COMMAND: {cmd!r}")
    # No camera grab in the hot path - it added ~1-2s per turn. Grab /shot.jpg on demand only when
    # a question actually needs vision.
    V.inject(f"[VOICE] {cmd}")

# ---- live stream ----
def parse_wav_header(hdr):
    """Return (rate, channels, bits) from a RIFF/WAVE header prefix."""
    try:
        if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            return 16000, 1, 16
        i = 12
        rate = chans = bits = None
        while i + 8 <= len(hdr):
            cid = hdr[i:i+4]; csz = struct.unpack("<I", hdr[i+4:i+8])[0]
            if cid == b"fmt ":
                chans = struct.unpack("<H", hdr[i+10:i+12])[0]
                rate = struct.unpack("<I", hdr[i+12:i+16])[0]
                bits = struct.unpack("<H", hdr[i+22:i+24])[0]
                return rate, chans, bits
            if cid == b"data":
                break
            i += 8 + csz + (csz & 1)
    except Exception:
        pass
    return 16000, 1, 16

def live():
    log("parallax_ear live loop starting")
    url = f"{V.IPWEBCAM}/audio.wav"
    while True:
        try:
            p = subprocess.Popen(["curl", "-s", "-N", "--socks5-hostname", V.SOCKS, url],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            hdr = p.stdout.read(44)
            if not hdr or len(hdr) < 44:
                log("no stream (server down?); retrying in 5s"); p.kill(); time.sleep(5); continue
            rate, chans, bits = parse_wav_header(hdr)
            log(f"stream open: {rate}Hz {chans}ch {bits}bit")
            setup_ack()   # push the instant-ack file now that the phone is reachable
            seg = Segmenter(rate)
            fb = seg.fbytes * max(1, chans)
            while True:
                buf = p.stdout.read(fb)
                if not buf or len(buf) < fb:
                    log("stream ended; reconnecting"); break
                if chans == 2:   # downmix to mono (take left channel)
                    buf = b"".join(buf[i:i+2] for i in range(0, len(buf), 4))
                utter = seg.push(buf)
                if utter:
                    try: handle_utterance(utter, rate)
                    except Exception as e: log(f"handle error: {e}")
        except Exception as e:
            log(f"stream error: {e}")
        finally:
            try: p.kill()
            except Exception: pass
        time.sleep(3)

def test_file(path):
    with wave.open(path) as w:
        rate = w.getframerate(); chans = w.getnchannels()
        pcm = w.readframes(w.getnframes())
    if chans == 2:
        pcm = b"".join(pcm[i:i+2] for i in range(0, len(pcm), 4))
    seg = Segmenter(rate)
    fb = seg.fbytes
    found = []
    for i in range(0, len(pcm) - fb, fb):
        u = seg.push(pcm[i:i+fb])
        if u:
            t = V.transcribe(write_wav(u, rate))
            found.append(t)
            print(f"  utterance: {t!r}  wake={has_wake(t)}  cmd={strip_wake(t)!r}")
    # flush tail
    if seg.in_speech and seg.spoke >= MIN_UTTER:
        t = V.transcribe(write_wav(bytes(seg.cur), rate))
        found.append(t)
        print(f"  tail utterance: {t!r}  wake={has_wake(t)}  cmd={strip_wake(t)!r}")
    print(f"segments: {len(found)}")

if __name__ == "__main__":
    if "--test-file" in sys.argv:
        test_file(sys.argv[sys.argv.index("--test-file")+1])
    else:
        live()
