#!/usr/bin/env python3
"""Incrementally transcribe a growing WAV recording of Russell's call.

Watches /tmp/call_full.wav (being written live by curl), and every time ~WINDOW seconds
of new audio have accumulated, transcribes that window with faster-whisper and appends the
text (with an elapsed-time marker) to /tmp/call_transcript.txt. So when the call ends the
transcript is already ~complete and assembly is instant.

Run: nohup python3 call_transcriber.py &   (stop by killing it)
"""
import os, sys, time, struct, wave
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PX_WHISPER", "small")   # good accuracy/speed balance for multi-speaker call
import voicecmd as V

SRC = os.environ.get("CALL_SRC", "/tmp/call_full.wav")
OUT = os.environ.get("CALL_OUT", "/tmp/call_transcript.txt")
WINDOW = 90          # seconds per transcription chunk
POLL = 8             # seconds between size checks

def wav_meta(path):
    """Parse header -> (data_offset, rate, channels, bits). File may be mid-write."""
    with open(path, "rb") as f:
        hdr = f.read(4096)
    if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
        return 44, 44100, 1, 16
    i = 12; rate=44100; chans=1; bits=16
    while i + 8 <= len(hdr):
        cid = hdr[i:i+4]; csz = struct.unpack("<I", hdr[i+4:i+8])[0]
        if cid == b"fmt ":
            chans = struct.unpack("<H", hdr[i+10:i+12])[0]
            rate = struct.unpack("<I", hdr[i+12:i+16])[0]
            bits = struct.unpack("<H", hdr[i+22:i+24])[0]
        if cid == b"data":
            return i + 8, rate, chans, bits
        i += 8 + csz + (csz & 1)
    return 44, rate, chans, bits

def transcribe_slice(path, off, rate, chans, bits, start_byte, nbytes):
    with open(path, "rb") as f:
        f.seek(off + start_byte)
        pcm = f.read(nbytes)
    if chans == 2:
        pcm = b"".join(pcm[i:i+2] for i in range(0, len(pcm), 4)); chans = 1
    tmp = "/tmp/call_win.wav"
    with wave.open(tmp, "w") as w:
        w.setnchannels(1); w.setsampwidth(bits//8); w.setframerate(rate)
        w.writeframes(pcm)
    return V.transcribe(tmp)

def main():
    open(OUT, "w").close()
    # wait for the file + header
    while not (os.path.exists(SRC) and os.path.getsize(SRC) > 4096):
        time.sleep(POLL)
    off, rate, chans, bits = wav_meta(SRC)
    bps = rate * (bits//8) * chans          # bytes per second
    win_bytes = WINDOW * bps
    done_bytes = 0                          # PCM bytes already transcribed
    last_size = -1
    stagnant = 0
    while True:
        size = os.path.getsize(SRC)
        avail = (size - off) - done_bytes
        if avail >= win_bytes:
            txt = transcribe_slice(SRC, off, rate, chans, bits, done_bytes, win_bytes)
            mark = time.strftime("%H:%M", time.gmtime())
            with open(OUT, "a") as f:
                f.write(f"[+{done_bytes//bps//60}m] {txt}\n")
            done_bytes += win_bytes
        else:
            # detect recording stopped (size stable) -> flush the tail then exit
            if size == last_size:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 4:   # ~32s with no growth = call ended
                tail = (size - off) - done_bytes
                if tail > bps * 3:   # at least 3s of tail
                    txt = transcribe_slice(SRC, off, rate, chans, bits, done_bytes, tail)
                    with open(OUT, "a") as f:
                        f.write(f"[+{done_bytes//bps//60}m] {txt}\n")
                with open(OUT, "a") as f:
                    f.write("[END OF CALL]\n")
                return
            time.sleep(POLL)
        last_size = size

if __name__ == "__main__":
    main()
