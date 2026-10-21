# SermonProcessor

A project that takes a video from a drive and edits it then creates a YouTube video and Spotify post.

Right now (Phase 1) it cuts a recorded service into finished videos, with an optional end graphic and podcast MP3.

## Try it (Phase 1)

1. Open the **Actions** tab, pick **Render**, and click **Run workflow**.
2. Leave the defaults and click the green **Run workflow** button.
3. When the run finishes (a few minutes), open it and download **rendered-files** at the bottom.

The run makes a test clip with a color pattern and a tone that changes pitch every 30 seconds, then renders:

| File | What to check |
| --- | --- |
| `2026-10-18 YouTube.mp4` | Starts at 0:15 of the test clip. Fades into the end graphic over 1.5 s. The graphic holds 5 s in silence. |
| `2026-10-18 YouTube.mp3` | Same section as audio only: mono, 96 kbps, even loudness, short fades. |
| `2026-10-18 Baptism.mp4` | A second cut from the same source, with no graphic. |

Files land in `2026/Sample Series/`, the same year and series folders the real Drive will use.

### Your own times

Paste this into **outputs** when you run it, and change the numbers:

```json
[{"name":"YouTube","in":"0:20","out":"1:40","podcast":true,
  "endGraphic":{"file":"Graphics/end-card.png","seconds":5,"crossfade":1}},
 {"name":"Baptism","in":"1:50","out":"2:40","endGraphic":null}]
```

Times can be `m:ss`, `h:mm:ss` or plain seconds, with decimals (`41:27.5`). Set `crossfade` to `0` for a hard cut.

## How it works

- `worker/render.py` reads a job file and runs FFmpeg once per output (see the top of the file for the format).
- `worker/make_sample.py` makes the test clip, the end graphic and a sample job.
- `.github/workflows/render.yml` runs both on a GitHub machine.

Each output's length is `(out − in) + graphic seconds`. The crossfade overlaps the end of the clip, so the graphic always gets its full time on screen.

## Run it on your Drive (Phase 2)

Needs the GitHub secrets `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` and the variable `DRIVE_ROOT_FOLDER_ID` (see the setup steps in the build plan).

The Drive folder looks like this. Year and series subfolders can be any depth:

```
Sermon Cutter/
  Incoming/2026/Romans/2026-10-18 service.mp4
  Thumbnails/2026/Romans/2026-10-18 service.png   (same name, .png or .jpg)
  Graphics/end-card.png
  Output/                                          (filled in by the worker)
```

1. **Check Drive:** Actions → **Process from Drive** → Run workflow with action **list**. The run page lists every video, whether its thumbnail was found, and the graphics.
2. **Render:** run it again with action **render** and a job like this:

```json
{"source": "2026/Romans/2026-10-18 service.mp4",
 "outputs": [
   {"name": "YouTube", "in": "2:45", "out": "41:27", "podcast": true,
    "endGraphic": {"file": "end-card.png", "seconds": 5, "crossfade": 1}},
   {"name": "Baptism", "in": "45:03", "out": "55:54", "endGraphic": null}]}
```

`source` is the path inside Incoming, and `file` is the path inside Graphics. Finished files go to `Output/2026/Romans/`, named with the date from the start of the video's file name. Running the same job again replaces those files instead of making copies. The run page links to each file in Drive.

## Web app (Phases 3–4)

`web/index.html` is the setup screen and `functions/api/` is its API, both hosted on Cloudflare Pages (free). The API reads Drive with the same Google sign-in as the worker, and starts **Process from Drive** on GitHub.

Cloudflare Pages settings: build command empty, build output directory `web`. Settings → Variables and secrets:

| Name | Type | Value |
| --- | --- | --- |
| `APP_PASSCODE` | Secret | The code people type to open the app |
| `GITHUB_TOKEN` | Secret | Fine-grained token for this repo with **Actions: Read and write** |
| `GITHUB_REPO` | Text | `JacobBoyce/SermonProcessor` |
| `GOOGLE_CLIENT_ID` | Secret | Same as the GitHub secret |
| `GOOGLE_CLIENT_SECRET` | Secret | Same as the GitHub secret |
| `GOOGLE_REFRESH_TOKEN` | Secret | Same as the GitHub secret |
| `DRIVE_ROOT_FOLDER_ID` | Text | Same as the GitHub variable |
