import { AppError } from "./google.js";

export const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

export function wrap(handler) {
  return async (ctx) => {
    try {
      return await handler(ctx);
    } catch (err) {
      const status = err instanceof AppError ? err.status : 500;
      return json({ error: err.message || String(err) }, status);
    }
  };
}
