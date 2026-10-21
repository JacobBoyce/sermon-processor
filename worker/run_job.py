#!/usr/bin/env python3
"""Run a job against Google Drive: download, render, upload.

Drive layout (inside the folder named by DRIVE_ROOT_FOLDER_ID):
    Incoming/2026/Romans/2026-10-18 service.mp4
    Thumbnails/2026/Romans/2026-10-18 service.png      (same name, .png/.jpg)
    Graphics/end-card.png
    Output/2026/Romans/...                              (created as needed)

Usage:
    python worker/run_job.py list
    python worker/run_job.py render --job '{"source": "2026/Romans/2026-10-18 service.mp4", "outputs": [...]}'

In a render job, "source" is the path inside Incoming and each endGraphic "file"
is the path inside Graphics. The output folders copy the source's year/series
folders, and the date comes from the file name when it starts with YYYY-MM-DD.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render  # noqa: E402
from drive import Drive, DriveError  # noqa: E402

FOLDERS = ("Incoming", "Graphics", "Thumbnails", "Output")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".mts", ".avi"}
IMAGE_EXT = (".png", ".jpg", ".jpeg")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def folder_id_from(value: str) -> str:
    """Accept a bare folder ID or a whole Drive link, and return just the ID."""
    value = value.strip()
    if "/folders/" in value:
        value = value.split("/folders/", 1)[1]
    return re.split(r"[?&#/\s]", value, maxsplit=1)[0]


def summary(text: str) -> None:
    """Add a line to the GitHub run page summary, when running in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")


def top_folders(drive, root_id: str) -> dict:
    root = drive.get(root_id)
    found = {}
    for name in FOLDERS:
        f = drive.child(root_id, name, folder=True)
        if f is None:
            raise DriveError(f"Make a folder named '{name}' inside '{root.name}'")
        found[name] = f
    return found


def find_thumbnail(drive, thumbs_id: str, rel_video: str):
    """Thumbnail with the same name and subfolders as the video, in .png or .jpg."""
    p = PurePosixPath(rel_video)
    folder = thumbs_id
    for part in p.parent.parts:
        f = drive.child(folder, part, folder=True)
        if f is None:
            return None
        folder = f.id
    for ext in IMAGE_EXT:
        f = drive.child(folder, p.stem + ext, folder=False)
        if f:
            return f
    return None


def mb(n: int) -> str:
    return f"{n / 1048576:,.0f} MB" if n < 1 << 30 else f"{n / (1 << 30):,.1f} GB"


# ---------------------------------------------------------------- actions

def do_list(drive, root_id: str) -> None:
    folders = top_folders(drive, root_id)
    summary("## Drive check\n")
    incoming = [(path, f) for path, f in drive.walk(folders["Incoming"].id)]
    videos = [(p, f) for p, f in incoming if PurePosixPath(p).suffix.lower() in VIDEO_EXT]
    print(f"Incoming: {len(videos)} video(s)")
    summary("| Video | Size | Thumbnail |\n| --- | --- | --- |")
    for path, f in videos:
        thumb = find_thumbnail(drive, folders["Thumbnails"].id, path)
        mark = thumb.name if thumb else "missing"
        print(f"  {path}  ({mb(f.size)})  thumbnail: {mark}")
        summary(f"| {path} | {mb(f.size)} | {mark} |")
    for name in ("Graphics", "Thumbnails", "Output"):
        items = list(drive.walk(folders[name].id))
        print(f"{name}: {len(items)} file(s)")
        for path, f in items[:50]:
            print(f"  {path}  ({mb(f.size)})")
        summary(f"\n**{name}:** {len(items)} file(s)")


def do_render(drive, root_id: str, job: dict, work: Path, height: str | None) -> None:
    folders = top_folders(drive, root_id)
    rel_source = str(job.get("source") or "").strip("/")
    if not rel_source:
        raise render.JobError("The job needs a 'source': the video's path inside Incoming")
    src = drive.resolve(folders["Incoming"].id, rel_source)

    free = shutil.disk_usage(work).free
    print(f"Source: Incoming/{rel_source} ({mb(src.size)}); free disk {mb(free)}")
    if src.size and src.size * 2.2 > free:
        print("  Warning: disk space is tight for this source and its renders.")

    local_src = drive.download(src, work / "source" / src.name)

    local_job = dict(job)
    local_job["source"] = str(local_src)
    p = PurePosixPath(rel_source)
    local_job.setdefault("folder", str(p.parent) if str(p.parent) != "." else "")
    m = DATE_RE.match(p.stem)
    if m and not local_job.get("date"):
        local_job["date"] = m.group(1)
    if not local_job.get("date") and not local_job.get("prefix"):
        local_job["prefix"] = PurePosixPath(src.name).stem   # no date in the name: use the file name
    if height:
        local_job.setdefault("video", {})["height"] = height

    graphics: dict[str, str] = {}
    for out in local_job.get("outputs", []):
        g = out.get("endGraphic")
        if not g:
            continue
        name = str(g.get("file") or "")
        if not name:
            images = [f for f in drive.children(folders["Graphics"].id)
                      if not f.is_folder and f.name.lower().endswith(IMAGE_EXT)]
            if len(images) != 1:
                raise render.JobError("Say which end graphic to use (\"file\"): "
                                      + (", ".join(f.name for f in images) or "the Graphics folder is empty"))
            name = images[0].name
        if name not in graphics:
            gf = drive.resolve(folders["Graphics"].id, name)
            graphics[name] = str(drive.download(gf, work / "graphics" / gf.name, progress=lambda *_: None))
        g["file"] = graphics[name]

    thumb = find_thumbnail(drive, folders["Thumbnails"].id, rel_source)
    print(f"Thumbnail: {thumb.name if thumb else 'none found (add one to Thumbnails with the same name)'}")

    job_path = work / "job.json"
    job_path.write_text(json.dumps(local_job, indent=2))
    out_dir = work / "output"
    written = render.render(job_path, out_dir)

    local_src.unlink(missing_ok=True)   # free disk before uploading
    summary(f"## Rendered `{rel_source}`\n\n| File | Size | Drive |\n| --- | --- | --- |")
    print("\nUploading to Output:")
    for f in written:
        rel_dir = f.parent.relative_to(out_dir).as_posix()
        folder_id = drive.ensure_folder(folders["Output"].id, "" if rel_dir == "." else rel_dir)
        file_id = drive.upload(f, folder_id)
        where = f"Output/{rel_dir}/{f.name}" if rel_dir != "." else f"Output/{f.name}"
        print(f"  {where}")
        summary(f"| {where} | {mb(f.stat().st_size)} | [open](https://drive.google.com/file/d/{file_id}/view) |")


def main() -> int:
    parser = argparse.ArgumentParser(description="Process a sermon video from Google Drive.")
    parser.add_argument("action", choices=["list", "render"])
    parser.add_argument("--job", default="", help="job JSON text, or a path to a job file")
    parser.add_argument("--height", default="", help="override output height: source or 1080")
    parser.add_argument("--work", type=Path, default=Path("work"))
    args = parser.parse_args()

    root_id = folder_id_from(os.environ.get("DRIVE_ROOT_FOLDER_ID", ""))
    try:
        if not root_id:
            raise DriveError("Set the DRIVE_ROOT_FOLDER_ID variable to your Sermon Cutter folder's ID")
        drive = Drive.from_env()
        if args.action == "list":
            do_list(drive, root_id)
        else:
            text = args.job.strip()
            if not text:
                raise render.JobError("Paste a job into the 'job' box")
            job = json.loads(Path(text).read_text()) if text.endswith(".json") and Path(text).is_file() \
                else json.loads(text)
            args.work.mkdir(parents=True, exist_ok=True)
            do_render(drive, root_id, job, args.work, args.height or None)
    except json.JSONDecodeError as exc:
        print(f"\nThe job isn't valid JSON: {exc}", file=sys.stderr)
        return 2
    except (DriveError, render.JobError) as exc:
        print(f"\nProblem: {exc}", file=sys.stderr)
        summary(f"\n**Problem:** {exc}")
        return 2
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
