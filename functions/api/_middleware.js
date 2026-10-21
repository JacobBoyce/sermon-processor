// Every /api request must carry the app passcode (set as APP_PASSCODE in Cloudflare).
import { json } from "../../lib/http.js";

function same(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function onRequest({ request, env, next }) {
  if (!env.APP_PASSCODE) return json({ error: "Set APP_PASSCODE in Cloudflare to turn the app on" }, 500);
  const url = new URL(request.url);
  // Images are loaded by <img> tags, which can't send headers, so they pass the code as ?k=
  const given = request.headers.get("x-passcode") || url.searchParams.get("k") || "";
  if (!same(given, env.APP_PASSCODE)) return json({ error: "Wrong passcode" }, 401);
  return next();
}
