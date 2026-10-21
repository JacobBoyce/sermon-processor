// GET /api/image/:id: show a thumbnail or end graphic from Drive. Images only.
import { AppError, drive } from "../../../lib/google.js";
import { wrap } from "../../../lib/http.js";

export const onRequestGet = wrap(async ({ env, params }) => {
  const id = String(params.id || "");
  if (!/^[\w-]{10,}$/.test(id)) throw new AppError("Bad image id");
  const meta = await drive(env, `/files/${id}`, { fields: "mimeType,size" });
  if (!String(meta.mimeType).startsWith("image/")) throw new AppError("Only images can be shown here");
  if (Number(meta.size || 0) > 25 * 1024 * 1024) throw new AppError("Image is too large to preview");
  const r = await drive(env, `/files/${id}`, { alt: "media" }, true);
  if (!r.ok) throw new AppError(`Drive said ${r.status}`, 502);
  return new Response(r.body, {
    headers: { "content-type": meta.mimeType, "cache-control": "private, max-age=3600" },
  });
});
