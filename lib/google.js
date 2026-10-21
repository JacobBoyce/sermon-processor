// Google Drive helpers for the web app's API (Cloudflare Pages Functions).
// Signs in with the same refresh token the GitHub worker uses.

const API = "https://www.googleapis.com/drive/v3";
const FOLDER = "application/vnd.google-apps.folder";
const VIDEO_EXT = [".mp4", ".mov", ".m4v", ".mkv", ".mts", ".avi"];
const IMAGE_EXT = [".png", ".jpg", ".jpeg"];

let cached = { token: "", expires: 0 };

export class AppError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

export function folderIdFrom(value = "") {
  let v = String(value).trim();
  if (v.includes("/folders/")) v = v.split("/folders/")[1];
  return v.split(/[?&#/\s]/)[0];
}

async function token(env) {
  if (cached.token && Date.now() < cached.expires - 60_000) return cached.token;
  for (const k of ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]) {
    if (!env[k]) throw new AppError(`Missing setting ${k} in Cloudflare`, 500);
  }
  const r = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    body: new URLSearchParams({
      client_id: env.GOOGLE_CLIENT_ID,
      client_secret: env.GOOGLE_CLIENT_SECRET,
      refresh_token: env.GOOGLE_REFRESH_TOKEN,
      grant_type: "refresh_token",
    }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new AppError(`Google sign-in failed: ${data.error_description || data.error || r.status}`, 502);
  cached = { token: data.access_token, expires: Date.now() + (data.expires_in || 3600) * 1000 };
  return cached.token;
}

export async function drive(env, path, params = {}, raw = false) {
  const url = new URL(API + path);
  for (const [k, v] of Object.entries({ supportsAllDrives: "true", ...params })) url.searchParams.set(k, v);
  const r = await fetch(url, { headers: { Authorization: `Bearer ${await token(env)}` } });
  if (raw) return r;
  if (r.status === 404) throw new AppError("Drive couldn't find that folder. Check DRIVE_ROOT_FOLDER_ID.", 404);
  if (!r.ok) throw new AppError(`Drive said ${r.status}: ${(await r.text()).slice(0, 200)}`, 502);
  return r.json();
}

const FIELDS = "id,name,mimeType,size,modifiedTime,videoMediaMetadata(durationMillis,width,height)";

async function children(env, folderId) {
  const files = [];
  let pageToken = "";
  do {
    const d = await drive(env, "/files", {
      q: `'${folderId}' in parents and trashed = false`,
      fields: `nextPageToken,files(${FIELDS})`,
      includeItemsFromAllDrives: "true",
      pageSize: "1000",
      orderBy: "name",
      ...(pageToken ? { pageToken } : {}),
    });
    files.push(...(d.files || []));
    pageToken = d.nextPageToken || "";
  } while (pageToken);
  return files;
}

async function walk(env, folderId, prefix = "", depth = 0, out = []) {
  const items = await children(env, folderId);
  await Promise.all(
    items.map(async (f) => {
      if (f.mimeType === FOLDER) {
        if (depth < 5) await walk(env, f.id, `${prefix}${f.name}/`, depth + 1, out);
      } else {
        out.push({ ...f, path: prefix + f.name });
      }
    }),
  );
  return out;
}

const ext = (name) => (name.match(/\.[^.]+$/)?.[0] || "").toLowerCase();
const stemPath = (path) => path.replace(/\.[^./]+$/, "").toLowerCase();

export async function listFiles(env) {
  const root = folderIdFrom(env.DRIVE_ROOT_FOLDER_ID);
  if (!root) throw new AppError("Set DRIVE_ROOT_FOLDER_ID in Cloudflare", 500);
  const top = await children(env, root);
  const find = (name) => {
    const f = top.find((x) => x.mimeType === FOLDER && x.name.toLowerCase() === name.toLowerCase());
    if (!f) throw new AppError(`Make a folder named '${name}' in your Sermon Cutter folder`, 404);
    return f;
  };
  const [incoming, thumbs, graphics, output] = ["Incoming", "Thumbnails", "Graphics", "Output"].map(find);
  const [inFiles, thumbFiles, graphicFiles] = await Promise.all([
    walk(env, incoming.id),
    walk(env, thumbs.id),
    children(env, graphics.id),
  ]);

  const thumbByStem = new Map();
  for (const t of thumbFiles) if (IMAGE_EXT.includes(ext(t.name))) thumbByStem.set(stemPath(t.path), t);

  const videos = inFiles
    .filter((f) => VIDEO_EXT.includes(ext(f.name)) || f.mimeType.startsWith("video/"))
    .map((f) => {
      const t = thumbByStem.get(stemPath(f.path));
      const m = f.videoMediaMetadata || {};
      return {
        id: f.id,
        path: f.path,
        size: Number(f.size || 0),
        modified: f.modifiedTime,
        duration: m.durationMillis ? Number(m.durationMillis) / 1000 : null,
        width: m.width || null,
        height: m.height || null,
        thumbnail: t ? { id: t.id, name: t.name } : null,
      };
    })
    .sort((a, b) => (b.modified || "").localeCompare(a.modified || ""));

  return {
    videos,
    graphics: graphicFiles
      .filter((f) => IMAGE_EXT.includes(ext(f.name)))
      .map((f) => ({ id: f.id, name: f.name })),
    outputFolder: `https://drive.google.com/drive/folders/${output.id}`,
  };
}
