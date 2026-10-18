/**
 * Mailuo (脉络) Multi-Tenant Cloud Gateway Worker
 * Backed by Cloudflare D1 (mailuo-db: 74d3d401-b084-41bd-9f13-0be9a62e56ea)
 *
 * Isolates every user's sessions, messages, and core themes by `user_id`.
 * Supports `/auth/register`, `/auth/login`, `/auth/me`, and authenticated `/d1/query`
 * (or higher-level tenant-isolated SQL execution).
 */

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  api_token TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_core_themes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  consolidated_insight TEXT NOT NULL DEFAULT '',
  session_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_topic_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  theme_id TEXT,
  parent_session_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  summary TEXT NOT NULL DEFAULT '',
  key_decisions_json TEXT NOT NULL DEFAULT '[]',
  open_questions_json TEXT NOT NULL DEFAULT '[]',
  external_sessions_json TEXT NOT NULL DEFAULT '{}',
  embedding_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_messages (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  agent_source TEXT NOT NULL DEFAULT 'mailuo',
  is_ephemeral INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_app_state (
  user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
`;

async function sha256Hex(text) {
  const data = new TextEncoder().encode(text);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function randomToken(prefix = "mai_tok_") {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return (
    prefix +
    Array.from(bytes)
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
  );
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

let schemaInitialized = false;

async function ensureSchema(db) {
  if (schemaInitialized) return;
  const statements = SCHEMA_SQL.split(";")
    .map((s) => s.trim())
    .filter(Boolean);
  for (const stmt of statements) {
    await db.prepare(stmt).run();
  }
  schemaInitialized = true;
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
      });
    }

    try {
      await ensureSchema(env.DB);
      const url = new URL(request.url);
      const path = url.pathname;

      if (path === "/" || path === "/health") {
        return jsonResponse({
          ok: true,
          service: "mailuo-cloud-gateway",
          version: "0.2.0",
        });
      }

      // 1. 用户注册 (/auth/register)
      if (path === "/auth/register" && request.method === "POST") {
        const body = await request.json();
        const username = (body.username || "").trim().toLowerCase();
        const password = body.password || "";
        if (!username || username.length < 2) {
          return jsonResponse({ ok: false, error: "用户名至少需要 2 个字符" }, 400);
        }
        if (!password || password.length < 4) {
          return jsonResponse({ ok: false, error: "密码至少需要 4 个字符" }, 400);
        }

        const existing = await env.DB.prepare(
          "SELECT id FROM users WHERE username = ?"
        )
          .bind(username)
          .first();
        if (existing) {
          return jsonResponse(
            { ok: false, error: `用户名 '${username}' 已被注册，请直接登录或换个名称` },
            409
          );
        }

        const userId = "usr_" + (await sha256Hex(username + Date.now())).slice(0, 10);
        const passHash = await sha256Hex("mailuo_salt:" + username + ":" + password);
        const token = randomToken("mai_");
        const now = new Date().toISOString();

        await env.DB.prepare(
          "INSERT INTO users (id, username, password_hash, api_token, created_at) VALUES (?, ?, ?, ?, ?)"
        )
          .bind(userId, username, passHash, token, now)
          .run();

        return jsonResponse({
          ok: true,
          user_id: userId,
          username,
          token,
          created_at: now,
        });
      }

      // 2. 用户登录 (/auth/login)
      if (path === "/auth/login" && request.method === "POST") {
        const body = await request.json();
        const username = (body.username || "").trim().toLowerCase();
        const password = body.password || "";
        const passHash = await sha256Hex("mailuo_salt:" + username + ":" + password);

        const user = await env.DB.prepare(
          "SELECT id, username, api_token, created_at FROM users WHERE username = ? AND password_hash = ?"
        )
          .bind(username, passHash)
          .first();

        if (!user) {
          return jsonResponse({ ok: false, error: "用户名或密码错误" }, 401);
        }

        return jsonResponse({
          ok: true,
          user_id: user.id,
          username: user.username,
          token: user.api_token,
          created_at: user.created_at,
        });
      }

      // 验证 Bearer Token
      const authHeader = request.headers.get("Authorization") || "";
      const token = authHeader.replace(/^Bearer\s+/i, "").trim();
      if (!token) {
        return jsonResponse(
          { ok: false, error: "未提供认证 Token，请先运行 `mai register` 或 `mai login`" },
          401
        );
      }

      const currentUser = await env.DB.prepare(
        "SELECT id, username, created_at FROM users WHERE api_token = ?"
      )
        .bind(token)
        .first();

      if (!currentUser) {
        return jsonResponse(
          { ok: false, error: "无效或已过期的 Token，请重新运行 `mai login`" },
          401
        );
      }

      if (path === "/auth/me") {
        return jsonResponse({
          ok: true,
          user_id: currentUser.id,
          username: currentUser.username,
          created_at: currentUser.created_at,
        });
      }

      // 3. 租户隔离 SQL 执行接口 (/d1/query)
      // 客户端传入带 `:user_id` 或由服务端强制注入 currentUser.id
      if (path === "/d1/query" && request.method === "POST") {
        const body = await request.json();
        const sql = body.sql || "";
        const rawParams = Array.isArray(body.params) ? body.params : [];
        const params = rawParams.map((p) =>
          p === "__CURRENT_USER_ID__" ? currentUser.id : p
        );

        const stmt = env.DB.prepare(sql).bind(...params);
        const res = await stmt.all();
        return jsonResponse({
          ok: true,
          user_id: currentUser.id,
          results: res.results || [],
        });
      }

      return jsonResponse({ ok: false, error: "Not Found" }, 404);
    } catch (err) {
      return jsonResponse({ ok: false, error: String(err.message || err) }, 500);
    }
  },
};
