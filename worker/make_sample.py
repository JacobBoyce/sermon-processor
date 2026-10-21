#!/usr/bin/env python3
"""Make a test clip, an end graphic and a sample job, so the worker can be tried
without any real footage.

Usage:
    python worker/make_sample.py [--dir sample] [--minutes 3] [--height 1080]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=Path("sample"))
    parser.add_argument("--minutes", type=float, default=3.0)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args()

    d = args.dir
    (d / "Incoming" / "2026" / "Sample Series").mkdir(parents=True, exist_ok=True)
    (d / "Graphics").mkdir(parents=True, exist_ok=True)
    h = args.height - args.height % 2
    w = round(h * 16 / 9) // 2 * 2
    secs = args.minutes * 60

    video = d / "Incoming" / "2026" / "Sample Series" / "test.mp4"
    # Moving test pattern with a running clock, plus a tone that changes pitch each
    # 30 s so cut points are easy to hear.
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={secs}",
         "-f", "lavfi", "-i",
         f"aevalsrc='0.2*sin(2*PI*(330+110*mod(floor(t/30),4))*t)':s=48000:d={secs}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ac", "2", str(video)])

    graphic = d / "Graphics" / "end-card.png"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1d2330:size=1920x1080",
         "-vf", "drawbox=x=160:y=440:w=1600:h=200:color=0xd9711c:t=fill,"
                "drawbox=x=160:y=680:w=900:h=40:color=0xf6f7f9:t=fill",
         "-frames:v", "1", str(graphic)])

    job = {
        "source": "Incoming/2026/Sample Series/test.mp4",
        "date": "2026-10-18",
        "folder": "2026/Sample Series",
        "title": "Sample Sermon",
        "video": {"height": "source", "crf": 20, "preset": "faster"},
        "outputs": [
            {"name": "YouTube", "in": "0:15", "out": f"{int(secs * 0.6 // 60)}:{int(secs * 0.6 % 60):02d}",
             "endGraphic": {"file": "Graphics/end-card.png", "seconds": 5, "crossfade": 1.5},
             "podcast": True},
            {"name": "Baptism", "in": f"{int(secs * 0.7 // 60)}:{int(secs * 0.7 % 60):02d}",
             "out": f"{int(secs * 0.9 // 60)}:{int(secs * 0.9 % 60):02d}", "endGraphic": None},
        ],
    }
    (d / "job.json").write_text(json.dumps(job, indent=2))
    print(f"Sample ready in {d}/: {video.name} ({w}x{h}, {args.minutes:g} min), end-card.png, job.json")


if __name__ == "__main__":
    main()
