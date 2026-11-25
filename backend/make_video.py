import os, shutil, subprocess, sys, time
from pathlib import Path
from comfy_client import generate_clip, CLIPS_DIR, ROOT

FINAL_DIR = ROOT / "outputs" / "final"
WORK_DIR = ROOT / "outputs" / "work"
MUSIC_FILE = ROOT / "music" / "background.mp3"   # optional
VOICES = {"female": "Microsoft Zira Desktop", "male": "Microsoft David Desktop"}
FPS, W, H = 24, 720, 1280

# Subject first so the small model keeps the subject.
SHOT_TEMPLATES = [
    "{s}, wide shot, cinematic lighting",
    "{s}, close-up, shallow depth of field",
    "{s}, slow tracking shot, warm cinematic lighting",
    "{s}, low angle shot, dramatic light",
    "{s}, side view, soft natural light",
    "{s}, final hero shot, soft background blur",
]


def split_scenes(subject, count=6):
    return [SHOT_TEMPLATES[i % len(SHOT_TEMPLATES)].format(s=subject) for i in range(count)]


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-700:]}")
    return r


def duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    return float(r.stdout.strip())


# ---------- narration (Windows built-in American English voices, no install) ----------
PS_SCRIPT = r'''
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $s.SelectVoice($env:TTS_VOICE) } catch {}
$s.Rate = [int]$env:TTS_RATE
$s.SetOutputToWaveFile($env:TTS_OUT)
$s.Speak([IO.File]::ReadAllText($env:TTS_TXT, [Text.Encoding]::UTF8))
$s.Dispose()
'''


def speak(text, out_wav, voice="female", rate=0):
    txt = out_wav.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    env = dict(os.environ, TTS_VOICE=VOICES.get(voice, VOICES["female"]),
               TTS_RATE=str(rate), TTS_OUT=str(out_wav), TTS_TXT=str(txt))
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_SCRIPT], env=env)


def build_segment(clip, narration, out_path, slow, voice):
    """One scene = slowed + upscaled video + narration audio. Returns (path, seconds)."""
    clip_dur = duration(clip)
    wav = None
    if narration:
        wav = out_path.with_suffix(".wav")
        speak(narration, wav, voice)
        need = duration(wav) + 0.4
        slow = min(max(slow, need / clip_dur), 2.5)     # stretch video to fit the voice
        target = max(clip_dur * slow, need)             # any remainder = last frame held
    else:
        target = clip_dur * slow
    base = clip_dur * slow
    vf = (f"[0:v]setpts={slow:.3f}*PTS,minterpolate=fps={FPS}:mi_mode=blend,"
          f"scale={W}:{H}:flags=lanczos,tpad=stop_mode=clone:stop_duration={max(0, target - base) + 0.2:.2f},"
          f"format=yuv420p[v]")
    af = "[1:a]aresample=44100,aformat=channel_layouts=stereo,apad[a]"
    cmd = ["ffmpeg", "-y", "-i", str(clip)]
    cmd += ["-i", str(wav)] if wav else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]",
            "-t", f"{target:.3f}", "-r", str(FPS), "-c:v", "libx264", "-crf", "18",
            "-preset", "medium", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            str(out_path)]
    run(cmd)
    return out_path, target


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def make_video(prompts, name, narrations=None, voice="female", captions=True,
               slow=1.5, cooldown=0, progress=None, gen=None):
    gen = gen or {}
    total = len(prompts)
    narrations = list(narrations or [])
    narrations += [""] * (total - len(narrations))
    say = progress or (lambda d, t, m: print(f"[{d}/{t}] {m}"))

    clips = []
    for i, p in enumerate(prompts, 1):
        dest = CLIPS_DIR / f"{name}_scene{i:02d}.mp4"
        if dest.exists():
            say(i, total, f"Scene {i} already exists, skipping")
        else:
            say(i - 1, total, f"Generating scene {i} of {total}")
            generate_clip(p, out_name=dest.name, **gen)
            say(i, total, f"Scene {i} done")
            if cooldown and i < total:
                say(i, total, f"Cooling down for {cooldown}s")
                time.sleep(cooldown)
        clips.append(dest)

    work = WORK_DIR / name
    work.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)

    segs, srt_items, t = [], [], 0.0
    for i, (clip, nar) in enumerate(zip(clips, narrations), 1):
        say(total, total, f"Building scene {i} (video + narration)")
        nar = nar.strip()
        seg, dur = build_segment(clip, nar, work / f"seg{i:02d}.mp4", slow, voice)
        segs.append(seg)
        if nar:
            srt_items.append((t, t + dur - 0.1, nar))
        t += dur

    say(total, total, "Joining scenes")
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
    current = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(current)])

    if MUSIC_FILE.exists():
        say(total, total, "Adding background music")
        mixed = work / "mixed.mp4"
        run(["ffmpeg", "-y", "-i", str(current), "-stream_loop", "-1", "-i", str(MUSIC_FILE),
             "-filter_complex", "[1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first:dontnormalize=1[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-shortest", str(mixed)])
        current = mixed

    final = FINAL_DIR / f"{name}.mp4"
    if captions and srt_items:
        say(total, total, "Burning subtitles")
        srt = work / "subs.srt"
        srt.write_text("".join(f"{k}\n{srt_time(a)} --> {srt_time(b)}\n{txt}\n\n"
                               for k, (a, b, txt) in enumerate(srt_items, 1)), encoding="utf-8")
        style = "FontSize=11,Bold=1,Outline=2,Shadow=0,MarginV=70,Alignment=2"
        run(["ffmpeg", "-y", "-i", current.name, "-vf", f"subtitles=subs.srt:force_style='{style}'",
             "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", str(final)],
            cwd=str(work))
    else:
        shutil.copy(current, final)
    say(total, total, "Finished")
    return final


if __name__ == "__main__":
    subject = sys.argv[1] if len(sys.argv) > 1 else "a fluffy orange cat dancing in a living room"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    print("Saved:", make_video(split_scenes(subject, count), "cli_test",
                               narrations=["Meet the dancing cat."] * count,
                               gen={"negative_extra": "person, girl, woman, human"}))