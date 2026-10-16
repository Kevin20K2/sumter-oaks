// Town Hall suggestion box: files a visitor's note as an issue in a PRIVATE GitHub repo.
// Vercel env vars: MAILBOX_TOKEN (fine-grained token, Issues: read/write on that repo only)
//                  MAILBOX_REPO  (e.g. "Kevin20K2/sumter-oaks-mailbox")
const KINDS = { idea: "Idea for the town", issue: "Something's off", hi: "Just saying hi" };
const recent = new Map(); // ip -> [timestamps]; best-effort, per warm instance
const LIMIT = 5, WINDOW_MS = 60 * 60 * 1000;

const clean = (v, max) => String(v ?? "").replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "").trim().slice(0, max);

function rateLimited(ip) {
  const now = Date.now();
  const list = (recent.get(ip) || []).filter(t => now - t < WINDOW_MS);
  if (list.length >= LIMIT) { recent.set(ip, list); return true; }
  list.push(now); recent.set(ip, list);
  if (recent.size > 5000) recent.clear();
  return false;
}

async function readBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string") return JSON.parse(req.body);
  let raw = "";
  for await (const chunk of req) { raw += chunk; if (raw.length > 8000) throw new Error("too big"); }
  return JSON.parse(raw || "{}");
}

module.exports = async (req, res) => {
  if (req.method !== "POST") { res.setHeader("Allow", "POST"); return res.status(405).json({ ok: false }); }
  const { MAILBOX_TOKEN, MAILBOX_REPO } = process.env;
  if (!MAILBOX_TOKEN || !/^[\w.-]+\/[\w.-]+$/.test(MAILBOX_REPO || "")) return res.status(503).json({ ok: false, error: "closed" });

  let body;
  try { body = await readBody(req); } catch (e) { return res.status(400).json({ ok: false }); }

  // Bots: a filled hidden field or a form sent in under 3 seconds gets a quiet "ok" and is dropped.
  if (clean(body.website, 200) || !(Number(body.ms) >= 3000)) return res.status(200).json({ ok: true });

  const kind = KINDS[body.kind] ? body.kind : "idea";
  const message = clean(body.message, 600);
  const name = clean(body.name, 60);
  const email = clean(body.email, 120);
  if (message.length < 3) return res.status(400).json({ ok: false, error: "empty" });
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return res.status(400).json({ ok: false, error: "email" });
  if ((message.match(/https?:\/\//gi) || []).length > 2) return res.status(200).json({ ok: true });

  const ip = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim() || "unknown";
  if (rateLimited(ip)) return res.status(429).json({ ok: false, error: "slow down" });

  const oneLine = message.replace(/\s+/g, " ");
  const title = `[${KINDS[kind]}] ${oneLine.length > 70 ? oneLine.slice(0, 67) + "…" : oneLine}`;
  const quote = message.split("\n").map(l => "> " + l).join("\n");
  const issueBody = [
    quote, "",
    `**Type:** ${KINDS[kind]}`,
    `**From:** ${name || "(no name)"}`,
    `**Reply to:** ${email || "(no email; no reply wanted)"}`,
    `**Sent:** ${new Date().toISOString()}`,
    "", "_Dropped in the Town Hall suggestion box on sumter-oaks.com._",
  ].join("\n");

  try {
    const r = await fetch(`https://api.github.com/repos/${MAILBOX_REPO}/issues`, {
      method: "POST",
      headers: { Authorization: `Bearer ${MAILBOX_TOKEN}`, Accept: "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sumter-oaks-suggestion-box" },
      body: JSON.stringify({ title, body: issueBody }),
    });
    if (!r.ok) { console.error("mailbox", r.status, (await r.text()).slice(0, 300)); return res.status(502).json({ ok: false }); }
    return res.status(200).json({ ok: true });
  } catch (e) {
    console.error("mailbox", e.message);
    return res.status(502).json({ ok: false });
  }
};
