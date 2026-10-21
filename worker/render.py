#!/usr/bin/env python3
"""Sermon Processor render worker.

Reads a job file (JSON) and makes one video per output with FFmpeg:
trim to in/out, optional crossfade into an end graphic that holds for N
seconds over silence, and an optional podcast MP3 of the in/out section.

Usage:
    python worker/render.py job.json [--out-dir DIR]

Job file:
{
  "source": "path/to/service.mp4",
  "date": "2026-10-18",                 # optional, used in file names
  "prefix": "Romans 1",                 # optional, used in file names instead of the date
  "folder": "2026/Romans",              # optional, output subfolder (year/series)
  "video": {"height": "source", "crf": 20, "preset": "faster"},   # optional
  "outputs": [
    {"name": "YouTube", "in": "2:45", "out": "41:27",
     "endGraphic": {"file": "graphics/card.png", "seconds": 5, "crossfade": 1.5},
     "podcast": true},
    {"name": "Baptism", "in": "45:03", "out": "55:54", "endGraphic": null}
  ]
}
Times may be seconds (165, 165.5) or "m:ss", "h:mm:ss", with optional decimals.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

AUDIO_RATE = 48000
PODCAST_BITRATE = "96k"
VIDEO_AUDIO_BITRATE = "192k"
DEFAULT_CRF = 20
DEFAULT_PRESET = "faster"


class JobError(Exception):
    """A problem with the job file that the person can fix."""


# ---------------------------------------------------------------- parsing

_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2}(?:\.\d+)?)$")


def parse_time(value, label: str) -> float:
    """Accept 165, 165.5, "165", "2:45", "1:02:10", "41:27.5"."""
    if isinstance(value, bool):
        raise JobError(f"{label}: expected a time, got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError:
            match = _TIME_RE.match(text)
            if not match:
                raise JobError(f"{label}: '{text}' is not a time. Use m:ss, h:mm:ss or seconds.") from None
            hours, minutes, secs = match.groups()  # "75:30" is fine as 75 minutes
            if float(secs) >= 60 or (hours and int(minutes) >= 60):
                raise JobError(f"{label}: '{text}' has minutes or seconds of 60 or more")
            seconds = int(hours or 0) * 3600 + int(minutes) * 60 + float(secs)
    if seconds < 0:
        raise JobError(f"{label}: time can't be negative")
    return seconds


def parse_number(value, label: str, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise JobError(f"{label}: '{value}' is not a number") from None
    if number < minimum:
        raise JobError(f"{label}: must be at least {minimum:g}")
    return number


def safe_name(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", text).strip(" .")
    return cleaned or "output"


def fmt_time(seconds: float) -> str:
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    frac = seconds - whole
    tail = f"{s:02d}" + (f"{frac:.2f}"[1:] if frac >= 0.005 else "")
    return f"{h}:{m:02d}:{tail}" if h else f"{m}:{tail}"


# ---------------------------------------------------------------- probing

@dataclass
class SourceInfo:
    duration: float
    width: int
    height: int
    fps: str          # as a rational, e.g. "30000/1001"
    has_audio: bool


def probe(path: Path) -> SourceInfo:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise JobError(f"Couldn't read the video '{path}': {exc.stderr.strip()}") from None
    data = json.loads(raw)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise JobError(f"'{path}' has no video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
    duration = float(data["format"].get("duration") or video.get("duration") or 0)
    fps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    if fps in ("0/0", "0/1"):
        fps = video.get("r_frame_rate") or "30/1"
    return SourceInfo(duration, int(video["width"]), int(video["height"]), fps, has_audio)


# ---------------------------------------------------------------- job model

@dataclass
class Graphic:
    file: Path
    seconds: float
    crossfade: float


@dataclass
class Output:
    name: str
    start: float
    end: float
    graphic: Graphic | None
    podcast: bool

    @property
    def clip(self) -> float:
        return self.end - self.start


def load_outputs(job: dict, info: SourceInfo, base: Path) -> list[Output]:
    raw_outputs = job.get("outputs")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise JobError("The job has no outputs. Add at least one in/out pair.")
    outputs: list[Output] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_outputs, 1):
        name = safe_name(str(raw.get("name") or f"Output {i}"))
        label = f"Output {i} ({name})"
        if name.lower() in seen:
            raise JobError(f"{label}: two outputs share this name; give each a different name")
        seen.add(name.lower())
        start = parse_time(raw.get("in"), f"{label} in")
        out_raw = raw.get("out")
        end = info.duration if str(out_raw).strip().lower() == "end" else parse_time(out_raw, f"{label} out")
        if end <= start:
            raise JobError(f"{label}: out ({fmt_time(end)}) must be after in ({fmt_time(start)})")
        if end > info.duration + 0.05:
            raise JobError(f"{label}: out ({fmt_time(end)}) is past the end of the video ({fmt_time(info.duration)})")
        end = min(end, info.duration)

        graphic = None
        g = raw.get("endGraphic")
        if g:
            gfile = Path(g.get("file", ""))
            if not gfile.is_absolute():
                gfile = (base / gfile).resolve()
            if not gfile.is_file():
                raise JobError(f"{label}: end graphic '{g.get('file')}' wasn't found")
            seconds = parse_number(g.get("seconds", 5), f"{label} graphic seconds")
            if seconds <= 0:
                raise JobError(f"{label}: graphic seconds must be more than 0")
            crossfade = parse_number(g.get("crossfade", 1), f"{label} crossfade")
            if crossfade >= end - start:
                raise JobError(f"{label}: crossfade ({crossfade:g}s) must be shorter than the clip ({end - start:.1f}s)")
            graphic = Graphic(gfile, seconds, crossfade)
        outputs.append(Output(name, start, end, graphic, bool(raw.get("podcast"))))
    return outputs


# ---------------------------------------------------------------- ffmpeg

def target_size(info: SourceInfo, setting) -> tuple[int, int]:
    """Output frame size. 'source' keeps it; a number sets the height (e.g. 1080)."""
    if setting in (None, "", "source"):
        w, h = info.width, info.height
    else:
        h = int(setting)
        if h <= 0:
            raise JobError("video.height must be 'source' or a positive number like 1080")
        w = round(info.width * h / info.height)
    return w - w % 2, h - h % 2


def build_video_cmd(src: Path, info: SourceInfo, out: Output, dest: Path, size: tuple[int, int],
                    crf: int, preset: str) -> tuple[list[str], float]:
    w, h = size
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={info.fps},format=yuv420p")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
           # -ss before -i seeks fast; with re-encoding the cut is still frame-accurate
           "-ss", f"{out.start:.3f}", "-t", f"{out.clip:.3f}", "-i", str(src)]
    inputs = 1
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{out.clip:.3f}", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        audio_in = f"{inputs}:a"
        inputs += 1
    else:
        audio_in = "0:a:0"

    if out.graphic is None:
        total = out.clip
        graph = (f"[0:v]{fit}[v];"
                 f"[{audio_in}]aresample={AUDIO_RATE},afade=t=in:d=0.05,"
                 f"afade=t=out:st={max(out.clip - 0.05, 0):.3f}:d=0.05[a]")
    else:
        g = out.graphic
        # Graphic input runs crossfade + hold seconds, so it is fully on screen for `seconds`.
        cmd += ["-loop", "1", "-framerate", info.fps, "-t", f"{g.crossfade + g.seconds:.3f}", "-i", str(g.file)]
        gi = inputs
        inputs += 1
        total = out.clip + g.seconds
        fade_len = g.crossfade if g.crossfade > 0 else 0.05
        if g.crossfade > 0:
            video = (f"[0:v]{fit}[main];[{gi}:v]{fit}[card];"
                     f"[main][card]xfade=transition=fade:duration={g.crossfade:.3f}:"
                     f"offset={out.clip - g.crossfade:.3f}[v]")
        else:
            # hard cut: drop the graphic's crossfade lead-in (0 here) and join
            video = f"[0:v]{fit}[main];[{gi}:v]{fit}[card];[main][card]concat=n=2:v=1:a=0[v]"
        audio = (f"[{audio_in}]aresample={AUDIO_RATE},afade=t=in:d=0.05,"
                 f"afade=t=out:st={out.clip - fade_len:.3f}:d={fade_len:.3f},"
                 f"apad=whole_dur={total:.3f}[a]")
        graph = f"{video};{audio}"

    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]",
            "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE, "-ar", str(AUDIO_RATE),
            "-movflags", "+faststart", str(dest)]
    return cmd, total


def build_podcast_cmd(src: Path, info: SourceInfo, out: Output, dest: Path, title: str,
                      date: str) -> tuple[list[str], float]:
    if not info.has_audio:
        raise JobError(f"{out.name}: the video has no audio, so there's no podcast to make")
    fade = min(1.0, out.clip / 4)
    audio = (f"aresample={AUDIO_RATE},"
             f"loudnorm=I=-16:TP=-1.5:LRA=11,"     # podcast loudness standard
             f"afade=t=in:d={fade:.3f},afade=t=out:st={out.clip - fade:.3f}:d={fade:.3f}")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
           "-ss", f"{out.start:.3f}", "-t", f"{out.clip:.3f}", "-i", str(src),
           "-vn", "-af", audio, "-ac", "1", "-ar", "44100",
           "-c:a", "libmp3lame", "-b:a", PODCAST_BITRATE,
           "-id3v2_version", "3", "-metadata", f"title={title}"]
    if date:
        cmd += ["-metadata", f"date={date[:4]}"]
    cmd.append(str(dest))
    return cmd, out.clip


def run_ffmpeg(cmd: list[str], total: float, label: str) -> None:
    """Run FFmpeg, printing progress about every 10% so the Actions log shows it moving."""
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    next_mark = 10
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us=") and total > 0:
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            pct = min(100, int(done / total * 100))
            if pct >= next_mark:
                elapsed = time.monotonic() - started
                print(f"    {label}: {pct}%  ({fmt_time(done)} of {fmt_time(total)}, {elapsed:.0f}s elapsed)", flush=True)
                next_mark = pct - pct % 10 + 10
    stderr = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"FFmpeg failed on {label}:\n{tail}")


# ---------------------------------------------------------------- main

def render(job_path: Path, out_dir: Path) -> list[Path]:
    try:
        job = json.loads(job_path.read_text())
    except json.JSONDecodeError as exc:
        raise JobError(f"The job file isn't valid JSON: {exc}") from None
    base = job_path.parent.resolve()

    src = Path(job.get("source", ""))
    if not src.is_absolute():
        src = (base / src).resolve()
    if not src.is_file():
        raise JobError(f"Source video '{job.get('source')}' wasn't found")

    info = probe(src)
    print(f"Source: {src.name}  {info.width}x{info.height}  {info.fps} fps  {fmt_time(info.duration)}"
          f"{'' if info.has_audio else '  (no audio)'}")

    outputs = load_outputs(job, info, base)
    settings = job.get("video") or {}
    size = target_size(info, settings.get("height", "source"))
    crf = int(settings.get("crf", DEFAULT_CRF))
    preset = str(settings.get("preset", DEFAULT_PRESET))

    date = str(job.get("date") or "").strip()
    folder = str(job.get("folder") or "").strip().strip("/")
    dest_dir = out_dir
    for part in folder.split("/") if folder else []:
        dest_dir = dest_dir / safe_name(part)
    dest_dir.mkdir(parents=True, exist_ok=True)
    label = str(job.get("prefix") or date).strip()
    prefix = f"{label} " if label else ""

    written: list[Path] = []
    for out in outputs:
        dest = dest_dir / f"{prefix}{out.name}.mp4"
        extra = (f", graphic {out.graphic.seconds:g}s, crossfade {out.graphic.crossfade:g}s"
                 if out.graphic else ", no graphic")
        print(f"\n[{out.name}] {fmt_time(out.start)} → {fmt_time(out.end)} ({fmt_time(out.clip)}){extra}, {size[0]}x{size[1]}")
        cmd, total = build_video_cmd(src, info, out, dest, size, crf, preset)
        run_ffmpeg(cmd, total, out.name)
        written.append(dest)
        print(f"    saved {dest}")

        if out.podcast:
            mp3 = dest_dir / f"{prefix}{out.name}.mp3"
            title = str(job.get("title") or f"{prefix}{out.name}").strip()
            cmd, total = build_podcast_cmd(src, info, out, mp3, title, date)
            run_ffmpeg(cmd, total, f"{out.name} podcast")
            written.append(mp3)
            print(f"    saved {mp3}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Render sermon outputs from a job file.")
    parser.add_argument("job", type=Path, help="path to the job JSON file")
    parser.add_argument("--out-dir", type=Path, default=Path("output"), help="where finished files go")
    args = parser.parse_args()
    try:
        files = render(args.job, args.out_dir)
    except JobError as exc:
        print(f"\nJob problem: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print(f"\nDone: {len(files)} file(s) written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
