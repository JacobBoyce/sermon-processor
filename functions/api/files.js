// GET /api/files: videos in Incoming (with matched thumbnails) and end graphics.
import { listFiles } from "../../lib/google.js";
import { json, wrap } from "../../lib/http.js";

export const onRequestGet = wrap(async ({ env }) => json(await listFiles(env)));
