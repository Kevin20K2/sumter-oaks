"""Sumter Oaks simulation loop.

Run one or more ticks of the town. Each tick:
  1. builds every resident's prompt from world.json, the saved state, and their memories
  2. asks the model for each resident's action (in parallel)
  3. checks each action against the map and the rules (bad actions become WAIT)
  4. updates positions, what everyone saw, memories, proposals and Town Hall matters
  5. asks the story model for a short paragraph about the tick
  6. writes data/ticks/tick_NNNN.json for the viewer, and saves data/state.json

Usage:
  python sim.py                 run 1 tick with the real models (needs ANTHROPIC_API_KEY)
  python sim.py --ticks 4       run 4 ticks
  python sim.py --test          use the cheap test model for everything
  python sim.py --mock          no API calls at all; canned actions, for checking the plumbing
  python sim.py --reset         start over from seed.json (deletes data/)
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
TICKS = DATA / "ticks"
LOGS = DATA / "logs"
STATE_FILE = DATA / "state.json"
EPISODES = TICKS / "episodes.json"   # lives beside the ticks so the GitHub job already commits it


# ---------------------------------------------------------------- setup

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_env():
    """Read KEY=value lines from .env into the environment (no extra package needed).
    Values in .env win over anything already set in Windows, so there is one place to look."""
    env = ROOT / ".env"
    if not env.exists():
        return "Windows environment" if os.environ.get("ANTHROPIC_API_KEY") else None
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")
    return ".env file"


def new_state(world, seed, cfg):
    agents = list(world["agents"])
    state = {
        "tick": seed.get("tick", 1),
        "time": cfg["start_time"],
        "positions": dict(seed["positions"]),
        "perceptions": {a: list(seed.get("events", [])) for a in agents},
        "memories": {a: [] for a in agents},
        "reflections": {a: list(seed["memory"].get(a, {}).get("reflections", [])) for a in agents},
        "emotions": {a: "neutral" for a in agents},
        "laws": list(world["laws"]),
        "proposals": [],
        "matters": [],
    }
    for a in agents:
        for text in seed["memory"].get(a, {}).get("memories", []):
            m = re.match(r"\[Tick (-?\d+)\]\s*(.*)", text)
            tick, body = (int(m.group(1)), m.group(2)) if m else (0, text)
            state["memories"][a].append({"tick": tick, "text": body, "importance": 5})
    return state


# ---------------------------------------------------------------- the data-center cloud (donations vs spending)

def stripe_list(path, params, key):
    """GET a Stripe list endpoint, following pagination. Read-only."""
    import urllib.parse
    import urllib.request
    out, params = [], dict(params, limit=100)
    while True:
        url = f"https://api.stripe.com/v1/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = json.loads(r.read().decode("utf-8"))
        out += page.get("data", [])
        if not page.get("has_more") or not page.get("data"):
            return out
        params["starting_after"] = page["data"][-1]["id"]


def plan_option(plan, url):
    """'monthly_10' -> {'amount': 10, 'recurring': True, ...}"""
    kind, _, amt = str(plan).partition("_")
    try:
        amount = float(amt)
    except ValueError:
        return None
    return {"plan": plan, "amount": amount, "recurring": kind == "monthly", "url": url}


def refunds_by_payment_intent(key):
    """Total refunded per payment, in cents. Needs Read on Refunds; without it, refunds just aren't subtracted."""
    out = {}
    try:
        for r in stripe_list("refunds", {}, key):
            if r.get("status") in ("succeeded", "pending") and r.get("payment_intent"):
                out[r["payment_intent"]] = out.get(r["payment_intent"], 0) + (r.get("amount") or 0)
    except Exception as e:
        print(f"  ! Couldn't check refunds ({e}); give the Stripe key Read access to Refunds to subtract them.")
    return out


def fetch_donations(cfg):
    """Total what supporters have given through this project's Stripe Payment Links.
    One-time gifts count once; monthly gifts count every paid invoice, so renewals add up.
    Needs STRIPE_SECRET_KEY (a restricted, read-only key). Returns None when it can't check."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    sup = cfg.get("support", {})
    if os.environ.get("FAKE_DONATIONS_USD"):  # for local testing
        return {"gross_usd": float(os.environ["FAKE_DONATIONS_USD"]), "payments": 1, "supporters": 1,
                "monthly_supporters": 0, "options": [], "test_mode": True}
    if not key:
        return None
    tag = sup.get("stripe_project_tag", "sumter-oaks")
    links = [l for l in stripe_list("payment_links", {}, key) if (l.get("metadata") or {}).get("project") == tag]
    options = [o for o in (plan_option((l.get("metadata") or {}).get("plan", ""), l["url"])
                           for l in links if l.get("active")) if o]
    link_ids = {l["id"] for l in links}
    refunded = refunds_by_payment_intent(key)
    gross, payments, supporters, monthly, returned = 0, 0, 0, 0, 0
    for s in stripe_list("checkout/sessions", {"status": "complete"}, key):
        if s.get("payment_link") not in link_ids and (s.get("metadata") or {}).get("project") != tag:
            continue
        if s.get("mode") == "subscription" and s.get("subscription"):
            paid = back = 0
            for inv in stripe_list("invoices", {"subscription": s["subscription"], "status": "paid"}, key):
                paid += inv.get("amount_paid", 0)
                back += min(refunded.get(inv.get("payment_intent"), 0), inv.get("amount_paid", 0))
                payments += 1
            gross += paid; returned += back
            if paid and back < paid:   # a fully refunded supporter isn't counted
                supporters += 1; monthly += 1
        elif s.get("payment_status") == "paid":
            paid = s.get("amount_total") or 0
            back = min(refunded.get(s.get("payment_intent"), 0), paid)
            gross += paid; returned += back
            payments += 1
            if back < paid:
                supporters += 1
    gross -= returned   # Stripe keeps its fee on a refund, so the payment still counts toward fees
    return {"gross_usd": gross / 100, "refunded_usd": returned / 100, "payments": payments, "supporters": supporters,
            "monthly_supporters": monthly, "options": sorted(options, key=lambda o: (o["recurring"], o["amount"])),
            "test_mode": key.startswith(("sk_test", "rk_test"))}


CLOUD_LINES = {
    "bright": None,  # say nothing while the cloud has a week or more; a full cloud reads as "no help needed"
    "fair": "The cloud over Sumter Oaks looks ordinary today. Nothing to write home about.",
    "low": "The cloud over Sumter Oaks has gone thin and grey. It looks worn out.",
    "empty": "The cloud over Sumter Oaks is down to a few grey wisps, drizzling on Magnolia Loop.",
}


def update_fund(cfg, state, fetched=None):
    """Recompute the cloud: balance = seed + donations (after card fees) - what the town has spent."""
    sup = cfg.get("support", {})
    fund = state.setdefault("fund", {})
    d = fetched if fetched is not None else fetch_donations_safe(cfg)
    if d is None:  # couldn't reach Stripe: keep the last numbers we had
        d = {k: fund.get(k, 0) for k in ("gross_usd", "payments", "supporters", "monthly_supporters")}
        d.update(options=fund.get("options") or sup.get("links", []), test_mode=fund.get("test_mode", True),
                 source="cached" if fund else "config")
    else:
        d.setdefault("source", "stripe")
        if not d.get("options"):
            d["options"] = sup.get("links", [])
    fees = d["gross_usd"] * sup.get("fee_percent", 2.9) / 100 + d["payments"] * sup.get("fee_fixed_usd", 0.30)
    donated = max(0.0, d["gross_usd"] - fees)
    spent = state.get("spend", {}).get("total_usd", 0.0)
    balance = sup.get("seed_usd", 0.0) + donated - spent
    recent = state.get("recent_tick_costs") or [0.025]
    per_tick = max(sum(recent) / len(recent), 0.001)
    ticks_left = max(0.0, balance / per_tick)
    days_left = ticks_left / sup.get("ticks_per_day", 24)
    level = ("empty" if balance <= 0 else "low" if days_left < sup.get("days_fair", 2)
             else "fair" if days_left < sup.get("days_bright", 7) else "bright")
    prev_level, prev_gross = fund.get("level"), fund.get("gross_usd", 0)
    fund.update(gross_usd=round(d["gross_usd"], 2), payments=d["payments"], supporters=d["supporters"],
                monthly_supporters=d["monthly_supporters"], options=d["options"], test_mode=d["test_mode"],
                level=level)
    doc = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": d.get("source", "stripe"),
        "test_mode": d["test_mode"],
        "seed_usd": round(sup.get("seed_usd", 0.0), 2),
        "donated_usd": round(donated, 2),
        "supporters": d["supporters"],
        "monthly_supporters": d["monthly_supporters"],
        "spent_usd": round(spent, 2),
        "balance_usd": round(balance, 2),
        "avg_tick_usd": round(per_tick, 4),
        "ticks_left": int(ticks_left),
        "days_left": round(days_left, 1),
        "level": level,
        "options": d["options"],
    }
    save_json(TICKS / "fund.json", doc)
    # The residents notice the sky.
    news = None
    if prev_gross is not None and d["gross_usd"] > (prev_gross or 0) and prev_level is not None:
        news = "The cloud over Magnolia Loop gives a little puff, as if someone just topped it up."
    elif prev_level is not None and level != prev_level:
        news = CLOUD_LINES.get(level)
    if news:
        for a in state.get("perceptions", {}):
            state["perceptions"][a].append(news)
    return doc


def fetch_donations_safe(cfg):
    try:
        return fetch_donations(cfg)
    except Exception as e:
        print(f"  ! Couldn't check donations: {e}")
        return None


# ---------------------------------------------------------------- time and weather

def town_tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone", "America/New_York"))
    except Exception:  # no time-zone data installed: fall back to UTC
        return dt.timezone.utc


def real_clock(cfg):
    return cfg.get("clock", "town") == "real"


def town_now(cfg, state):
    """Real Florida time when "clock" is "real"; otherwise the town's own calendar."""
    if real_clock(cfg):
        return dt.datetime.now(town_tz(cfg)).replace(second=0, microsecond=0, tzinfo=None)
    return dt.datetime.fromisoformat(state["time"])


def awake(cfg, now):
    return parse_hhmm(cfg["day_start"]) <= now.time() < parse_hhmm(cfg["day_end"])


def parse_hhmm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def fmt_time(t):
    return t.strftime("%A, %B %d, %I:%M %p").replace(" 0", " ")


def weather_for(t, tick):
    rng = random.Random(t.strftime("%Y%m%d"))
    sky = rng.choice(["Sunny", "Sunny", "Partly cloudy", "Humid and hazy", "Breezy and clear"])
    shower = rng.random() < 0.25 and 14 <= t.hour <= 17
    base = rng.randint(72, 80)
    temp = base + round(6 * max(0, 1 - abs(t.hour + t.minute / 60 - 15) / 8))
    return f"{'Afternoon shower' if shower else sky}, {temp}°F"


# ---------------------------------------------------------------- prompts

def bullets(items):
    items = list(items)
    return "\n".join(f"  - {i}" for i in items) if items else "  - (none)"


def retrieve_memories(mems, n):
    """Most recent memories plus the most important older ones, in time order."""
    recent = mems[-(n // 2 + 1):]
    rest = [m for m in mems if m not in recent]
    important = sorted(rest, key=lambda m: m["importance"], reverse=True)[: n - len(recent)]
    picked = sorted(recent + important, key=lambda m: m["tick"])
    return [f"[Tick {m['tick']}] {m['text']}" for m in picked]


def build_prompt(template, world, state, cfg, agent_id):
    a = world["agents"][agent_id]
    zid = state["positions"][agent_id]
    zone = world["zones"][zid]
    nearby = [f"{o} ({world['agents'][o]['name']})" for o, z in state["positions"].items() if z == zid and o != agent_id]
    laws = [f"{l['id']} {l['title']}: {l['text']}" for l in state["laws"]]
    open_props = [p for p in state["proposals"] if p["status"] == "open"]
    if open_props:
        laws.append("Open proposals awaiting votes at Town Hall: " + "; ".join(
            f"{p['id']} \"{p['title']}\" ({p['text']})" for p in open_props))
    now = dt.datetime.fromisoformat(state["time"])
    values = {
        "agent_name": a["name"],
        "town_name": world["town_name"],
        "agent_age": str(a["age"]),
        "agent_profession": a["profession"],
        "agent_traits": a["traits"],
        "agent_core_motivations": a["core_motivations"],
        "agent_current_goal": a["current_goal"],
        "current_zone": f"{zone['name']} [{zid}] (connects to: {', '.join(zone['connects_to'])})",
        "active_municipal_laws": bullets(laws),
        "retrieved_reflections": bullets(state["reflections"][agent_id][-4:]),
        "retrieved_memories": bullets(retrieve_memories(state["memories"][agent_id], cfg["memories_per_prompt"])),
        "current_tick": str(state["tick"]),
        "simulation_time": fmt_time(now),
        "weather_condition": weather_for(now, state["tick"]),
        "nearby_agents_list": ", ".join(nearby) or "none",
        "interactive_objects_list": ", ".join(zone["objects"]) or "none",
        "recent_observations_log": bullets(state["perceptions"].get(agent_id) or ["Nothing notable."]),
        "minutes_per_tick": str(cfg["minutes_per_tick"]),
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


# ---------------------------------------------------------------- model calls

def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


USAGE_LOCK = __import__("threading").Lock()


class Brain:
    def __init__(self, cfg, test=False, mock=False):
        self.cfg, self.mock = cfg, mock
        self.usage = {}
        self.no_thinking_param = set()
        self.resident_model = cfg["models"]["test"] if test else cfg["models"]["resident"]
        self.story_model = cfg["models"]["test"] if test else cfg["models"]["story"]
        self.client = None
        if not mock:
            try:
                import anthropic
            except ImportError:
                sys.exit("The anthropic package isn't installed. Run:  pip install -r requirements.txt")
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not key or "your-key-here" in key:
                sys.exit("ANTHROPIC_API_KEY isn't set. Put it in a .env file next to sim.py (see .env.example).")
            print(f"API key: {key[:13]}... ({len(key)} characters)")
            if not key.startswith("sk-ant-api"):
                print("  Warning: API keys from Console > Settings > API keys normally start with sk-ant-api.")
            self.client = anthropic.Anthropic()
            self.anthropic = anthropic

    def preflight(self):
        """One tiny call before running ticks, so a bad key or model fails loudly instead of silently."""
        if self.mock:
            return
        a = self.anthropic
        for model in sorted({self.resident_model, self.story_model}):
            try:
                self._ask(model, "Reply with OK.", "ping", 5)
            except a.AuthenticationError:
                sys.exit("Anthropic rejected the API key (401). Create a new key at platform.claude.com under "
                         "Settings > API keys, paste it into .env, and try again.")
            except a.PermissionDeniedError as e:
                sys.exit(f"This key isn't allowed to use {model} (403): {e}")
            except a.NotFoundError:
                sys.exit(f"Model {model!r} wasn't found (404). Check the model names in config.json.")
            except a.BadRequestError as e:
                msg = str(e)
                if "credit" in msg.lower():
                    sys.exit("Your API account is out of credits. Add credits under Billing in the Console.")
                sys.exit(f"The API refused the request (400): {msg}")
            except a.APIConnectionError:
                sys.exit("Couldn't reach the Anthropic API. Check your internet connection.")
        print("API check passed.")

    def _ask(self, model, system, user, max_tokens):
        # These models think before answering by default, and thinking counts toward max_tokens.
        # config.json "thinking" turns up-front thinking off per model; if the API refuses that
        # setting, retry once without it and stop sending it for this run.
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}])
        thinking = self.cfg.get("thinking", {}).get(model)
        if thinking and model not in self.no_thinking_param:
            kwargs["thinking"] = thinking
        try:
            r = self.client.messages.create(**kwargs)
        except Exception as e:
            if "thinking" in kwargs and "thinking" in str(e).lower():
                print(f"  ! {model} refused thinking={thinking}; continuing without it")
                self.no_thinking_param.add(model)
                kwargs.pop("thinking")
                r = self.client.messages.create(**kwargs)
            else:
                raise
        self._record(model, getattr(r, "usage", None))
        if getattr(r, "stop_reason", None) == "max_tokens":
            print(f"  ! A {model} reply hit its length limit ({max_tokens} tokens) and was cut off. "
                  f"Raise max_tokens in config.json.")
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

    # Token and dollar tracking, reset at the start of every tick.
    def reset_usage(self):
        self.usage = {}

    def _record(self, model, usage):
        if usage is None:
            return
        with USAGE_LOCK:
            u = self.usage.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            u["calls"] += 1
            u["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            u["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

    def cost_report(self):
        prices = self.cfg.get("prices_per_mtok", {})
        total = 0.0
        for model, u in self.usage.items():
            p = prices.get(model)
            u["usd"] = round((u["input_tokens"] * p["input"] + u["output_tokens"] * p["output"]) / 1e6, 5) if p else None
            total += u["usd"] or 0
        return {"usd": round(total, 5), "by_model": self.usage}

    def decide(self, prompt, agent_id, world, state, rng):
        if self.mock:
            return mock_decision(agent_id, world, state, rng), "(mock)"
        ask = "Decide your one action for this tick. Reply with the JSON object only."
        raw = self._ask(self.resident_model, prompt, ask, self.cfg["max_tokens"]["resident"])
        try:
            return extract_json(raw), raw
        except (ValueError, json.JSONDecodeError):
            raw = self._ask(self.resident_model, prompt,
                            ask + " Your previous reply was not valid JSON; send only the JSON object.",
                            self.cfg["max_tokens"]["resident"])
            return extract_json(raw), raw

    def story(self, summary):
        if self.mock:
            return " ".join(summary["lines"][:4])
        system = ("You narrate a warm, gently comic, realistic town simulation set in Sumter Oaks, a Florida "
                  "55+ golf cart community. Write 2-4 plain sentences in present tense summarizing this moment "
                  "for readers following along. Use first names. No headings, no lists, no quotation of every line.")
        return self._ask(self.story_model, system, json.dumps(summary, ensure_ascii=False),
                         self.cfg["max_tokens"]["story"]).strip()

    def recap(self, material):
        """Write the next 'Previously on Sumter Oaks' episode from the last recap plus what happened since."""
        if self.mock:
            last = material["new_ticks"][-1]
            return {
                "title": f"The Saga Continues ({len(material['new_ticks'])} ticks)",
                "recap": " ".join(t["story"] for t in material["new_ticks"][-2:])[:600],
                "standings": {a: f"Last seen at {r['where']}, feeling {r['mood']}." for a, r in material["residents"].items()},
                "next_time": [f"What happens after {last['time']}?"],
            }
        system = (
            "You are the narrator of \"Sumter Oaks\", a warm, gently comic daytime soap opera about four AI "
            "neighbors in a Florida 55+ golf-cart community. Write the \"Previously on Sumter Oaks\" recap that "
            "catches up a brand-new viewer on the whole story so far.\n"
            "Voice: a classic soap announcer: dramatic, a little breathless, affectionate, never mean. Small stakes "
            "told as grand drama is the joke.\n"
            "Rules:\n"
            "- Use only what is in the material. Never invent events, relationships, secrets or facts. You may "
            "dramatize tone, not content.\n"
            "- Fold the previous recap into this one so it covers everything, but lead with what's newest.\n"
            "- next_time items are open questions about unresolved threads, never predictions.\n"
            "- Use first names. Keep it family-friendly.\n"
            "Reply with JSON only, no code fences:\n"
            "{\"title\": \"episode title, 3-7 words, no episode number\", "
            "\"recap\": \"4-6 sentences, under 120 words\", "
            "\"standings\": {\"<resident id>\": \"one short line on where they stand\", ...}, "
            "\"next_time\": [\"question\", \"question\"]}"
        )
        user = json.dumps(material, ensure_ascii=False)
        limit = self.cfg["max_tokens"].get("recap", 1500)
        raw = self._ask(self.story_model, system, user, limit)
        try:
            return extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            print(f"  ! Recap reply wasn't JSON, asking once more. It began: {raw[:160]!r}")
            raw = self._ask(self.story_model, system + "\nYour last reply was not valid JSON. Reply with the JSON object only.", user, limit)
            return extract_json(raw)

    def reflect(self, agent, memories):
        if self.mock:
            return []
        system = ("You help a simulated resident form beliefs. Given their recent memories, write 2 short "
                  "first-person reflections (one sentence each) about people, relationships or their goal. "
                  "Reply as a JSON array of strings only.")
        raw = self._ask(self.story_model, system,
                        json.dumps({"resident": agent, "memories": memories}, ensure_ascii=False),
                        self.cfg["max_tokens"]["story"])
        try:
            out = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            return [s for s in out if isinstance(s, str)][:2]
        except json.JSONDecodeError:
            return []


# ---------------------------------------------------------------- mock residents (no API)

MOCK_LINES = {
    "gary": ["You ever wire a marine speaker to a golf cart? Me neither, until Tuesday.",
             "If your disposal acts up again, just holler.", "One game of pickleball. One."],
    "donna": ["Court 2, Saturday, 8 AM. Bring water.", "Did you hear about the mailbox complaint?",
              "Seven is a flock, not a collection."],
    "hal": ["That cart needs approval. Rules are rules.", "Quiet hours start at ten. Just a reminder.",
            "Your recycling bin is facing the right way now. Good."],
    "rosa": ["Dinner Friday at six. I'm not taking no.", "Has anyone actually eaten today?",
             "You two should sit together at dinner."],
}


def mock_decision(agent_id, world, state, rng):
    zid = state["positions"][agent_id]
    zone = world["zones"][zid]
    others = [o for o, z in state["positions"].items() if z == zid and o != agent_id]
    roll = rng.random()
    if others and roll < 0.45:
        act = {"type": "SPEAK", "parameters": {"target": rng.choice(others), "content": rng.choice(MOCK_LINES[agent_id])}}
    elif roll < 0.75:
        act = {"type": "MOVE", "parameters": {"target": rng.choice(zone["connects_to"]), "content": "errand"}}
    elif zone["objects"]:
        act = {"type": "INTERACT", "parameters": {"target": rng.choice(zone["objects"]), "content": "checks on it"}}
    else:
        act = {"type": "WAIT", "parameters": {"target": "none", "content": "people-watching"}}
    if zid == "town_hall" and agent_id == "hal" and not state["proposals"]:
        act = {"type": "DRAFT_LAW", "parameters": {"target": "Flamingo Fairness Act",
                                                   "content": "Lawn flamingos count as one ornament per flock."}}
    open_props = [p for p in state["proposals"] if p["status"] == "open" and agent_id not in p["votes"]]
    if zid == "town_hall" and open_props:
        act = {"type": "VOTE", "parameters": {"target": open_props[0]["id"], "content": rng.choice(["YES", "NO"])}}
    return {"thought_process": "(mock)", "emotional_state": rng.choice(["cheerful", "curious", "grumpy", "hopeful"]),
            "importance_rating": rng.randint(3, 7), "action": act}


# ---------------------------------------------------------------- validation

def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


def match_id(raw, candidates, names=None):
    """Find raw among candidate ids, also accepting display names or first names."""
    key = norm(raw)
    for c in candidates:
        if key == c:
            return c
    for c in candidates:
        n = norm((names or {}).get(c, ""))
        if n and (key == n or key == n.split("_")[0]):
            return c
    return None


def validate(result, agent_id, world, state):
    """Return (clean_result, note). Anything out of reach becomes WAIT with a note."""
    zid = state["positions"][agent_id]
    zone = world["zones"][zid]
    act = result.get("action") or {}
    typ = str(act.get("type", "WAIT")).upper().strip()
    p = act.get("parameters") or {}
    target, content = p.get("target"), str(p.get("content") or "").strip()
    note = None

    def wait(reason, public):
        # The reason stays in the log and engine_note; the story shows the resident's own words
        # if they gave any, otherwise a plain description of what they're doing.
        return {"type": "WAIT", "parameters": {"target": "none", "content": content or public}}, reason

    if typ == "MOVE":
        zone_names = {z: world["zones"][z]["name"] for z in zone["connects_to"]}
        dest = match_id(target, zone["connects_to"], zone_names)
        act, note = ({"type": "MOVE", "parameters": {"target": dest, "content": content}}, None) if dest else \
            wait(f"tried to go to {target!r}, which isn't connected to {zid}", "thinking about where to head next")
    elif typ == "SPEAK":
        here = [o for o, z in state["positions"].items() if z == zid and o != agent_id]
        who = match_id(target, here, {o: world["agents"][o]["name"] for o in here})
        act, note = ({"type": "SPEAK", "parameters": {"target": who, "content": content}}, None) if who and content else \
            wait(f"tried to speak to {target!r}, who isn't here", "looking around for someone to talk to")
    elif typ == "INTERACT":
        obj = match_id(target, zone["objects"])
        act, note = ({"type": "INTERACT", "parameters": {"target": obj, "content": content}}, None) if obj else \
            wait(f"tried to use {target!r}, which isn't in {zid}", f"puttering around {zone['name']}")
    elif typ == "DRAFT_LAW":
        act, note = ({"type": "DRAFT_LAW", "parameters": {"target": str(target).strip(), "content": content}}, None) \
            if zid == "town_hall" and target and content else wait("tried to draft a law away from Town Hall", "mulling over the neighborhood rules")
    elif typ == "VOTE":
        ids = [p_["id"] for p_ in state["proposals"] if p_["status"] == "open"]
        pid = str(target or "").upper().strip()
        vote = content.upper()
        act, note = ({"type": "VOTE", "parameters": {"target": pid, "content": vote}}, None) \
            if zid == "town_hall" and pid in ids and vote in ("YES", "NO") else wait(f"tried to vote on {target!r}", "reading over the open proposals")
    else:
        act = {"type": "WAIT", "parameters": {"target": "none", "content": content or "watching the street"}}

    try:
        imp = max(1, min(10, int(result.get("importance_rating", 5))))
    except (TypeError, ValueError):
        imp = 5
    clean = {
        "thought_process": str(result.get("thought_process", "")),
        "emotional_state": str(result.get("emotional_state", "neutral"))[:40],
        "importance_rating": imp,
        "action": act,
    }
    if note:
        clean["engine_note"] = note
    return clean, note


# ---------------------------------------------------------------- one tick

def first(world, a):
    return world["agents"][a]["name"].split()[0]


def describe(world, a, act):
    """Public, third-person description of an action."""
    p, n = act["parameters"], first(world, a)
    t = act["type"]
    if t == "SPEAK":
        return f"{n} said to {first(world, p['target'])}: “{p['content']}”"
    if t == "MOVE":
        return f"{n} headed to {world['zones'][p['target']]['name']}."
    if t == "INTERACT":
        return f"{n} ({p['target'].replace('_', ' ')}): {p['content']}"
    if t == "DRAFT_LAW":
        return f"{n} filed a proposed law: “{p['target']}”."
    if t == "VOTE":
        return f"{n} cast a vote on {p['target']}."
    return f"{n} waited: {p['content']}"


def write_episode(world, state, episodes, pending, brain, results):
    """Ask the narrator for the next episode and append it to data/ticks/episodes.json."""
    agents = list(world["agents"])
    prev = episodes["episodes"][-1] if episodes["episodes"] else None
    material = {
        "episode_number": len(episodes["episodes"]) + 1,
        "previous_recap": {k: prev[k] for k in ("title", "recap", "standings")} if prev else None,
        "new_ticks": pending,
        "residents": {a: {"name": world["agents"][a]["name"], "goal": world["agents"][a]["current_goal"],
                          "mood": results[a]["emotional_state"],
                          "where": world["zones"][state["positions"][a]]["name"]} for a in agents},
        "open_town_hall_matters": [m["text"] for m in state["matters"][-6:]],
    }
    ep = brain.recap(material)
    standings = ep.get("standings") or {}
    episode = {
        "number": material["episode_number"],
        "title": str(ep.get("title", "")).strip() or f"Episode {material['episode_number']}",
        "recap": str(ep.get("recap", "")).strip(),
        "standings": {a: str(standings.get(a, "")).strip() for a in agents},
        "next_time": [str(q).strip() for q in (ep.get("next_time") or []) if str(q).strip()][:3],
        "through_tick": pending[-1]["tick"],
        "from_tick": pending[0]["tick"],
        "simulation_time": pending[-1]["time"],
    }
    if not episode["recap"]:
        raise ValueError("narrator returned an empty recap")
    episodes["episodes"].append(episode)
    save_json(EPISODES, episodes)
    print(f"  Episode {episode['number']}: {episode['title']}")


def run_tick(world, state, cfg, template, brain):
    tick = state["tick"]
    now = town_now(cfg, state)
    rng = random.Random(tick * 7919)
    agents = list(world["agents"])
    if real_clock(cfg):
        # First tick of a new real day: everyone wakes up at home.
        last = state.get("last_tick_date")
        if last and last != now.date().isoformat():
            for a in agents:
                state["positions"][a] = world["agents"][a]["home"]
                state["perceptions"][a] = [f"A new morning in {world['town_name']}. You're at home."]
        state["time"] = now.isoformat(timespec="minutes")
    start_pos = dict(state["positions"])

    brain.reset_usage()

    # 1-2. prompts and decisions, in parallel
    prompts = {a: build_prompt(template, world, state, cfg, a) for a in agents}
    raws, results, log = {}, {}, []
    with cf.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futs = {a: pool.submit(brain.decide, prompts[a], a, world, state, random.Random(rng.random())) for a in agents}
        for a, f in futs.items():
            try:
                decision, raws[a] = f.result()
            except Exception as e:  # network error, bad JSON twice, etc.
                decision, raws[a] = {"action": {"type": "WAIT", "parameters": {"content": "lost in thought"}}}, f"ERROR: {e}"
            results[a], note = validate(decision, a, world, state)
            log.append({"agent": a, "raw": raws[a], "note": note})
            if str(raws[a]).startswith("ERROR"):
                print(f"  ! {first(world, a)}: {raws[a][:300]}")
    if all(str(raws[a]).startswith("ERROR") for a in agents):
        sys.exit("Every resident's API call failed, so this tick was not saved. See the errors above.")

    # 3-4. apply actions
    zone_events = {}          # zone -> list of public descriptions
    public_lines, new_matters = [], []
    for a in agents:
        act = results[a]["action"]
        line = describe(world, a, act)
        zone_events.setdefault(start_pos[a], []).append((a, line))
        public_lines.append(line)
        p = act["parameters"]
        if act["type"] == "MOVE":
            state["positions"][a] = p["target"]
        elif act["type"] == "INTERACT" and start_pos[a] == "town_hall" and p["target"] in (
                "architectural_review_desk", "proposal_drop_box"):
            new_matters.append({"by": a, "text": f"{world['agents'][a]['name']}: {p['content']}"})
        elif act["type"] == "DRAFT_LAW":
            pid = f"P-{len(state['proposals']) + 1:03d}"
            state["proposals"].append({"id": pid, "title": p["target"], "text": p["content"], "by": a,
                                       "opened": tick, "votes": {}, "status": "open"})
            new_matters.append({"by": a, "text": f"{pid} proposed by {first(world, a)}: {p['target']}. {p['content']}"})
        elif act["type"] == "VOTE":
            for pr in state["proposals"]:
                if pr["id"] == p["target"]:
                    pr["votes"][a] = p["content"]
        state["emotions"][a] = results[a]["emotional_state"]

    # proposals close when everyone has voted or time runs out
    town_news = []
    for pr in state["proposals"]:
        if pr["status"] != "open":
            continue
        if len(pr["votes"]) == len(agents) or tick - pr["opened"] >= cfg["proposal_open_ticks"]:
            yes = sum(v == "YES" for v in pr["votes"].values())
            no = sum(v == "NO" for v in pr["votes"].values())
            passed = yes > no
            pr["status"] = "passed" if passed else "failed"
            if passed:
                state["laws"].append({"id": f"L-{len(state['laws']) + 1:02d}", "title": pr["title"], "text": pr["text"]})
            msg = f"{pr['id']} “{pr['title']}” {pr['status']} {yes}-{no}."
            new_matters.append({"by": pr["by"], "text": msg})
            town_news.append(f"Town Hall notice: {msg}")

    # what each resident perceives next tick, and what they remember
    for a in agents:
        seen = [line for (who, line) in zone_events.get(start_pos[a], []) if who != a]
        if state["positions"][a] != start_pos[a]:
            seen.append(f"You arrived at {world['zones'][state['positions'][a]]['name']}.")
        for o in agents:
            if o != a and state["positions"][o] == state["positions"][a] and start_pos[o] != state["positions"][a]:
                seen.append(f"{first(world, o)} just arrived.")
        seen += town_news
        state["perceptions"][a] = seen
        imp = results[a]["importance_rating"]
        own = describe(world, a, results[a]["action"]).replace(first(world, a), "I", 1)
        state["memories"][a].append({"tick": tick, "text": own, "importance": imp})
        for s in seen:
            state["memories"][a].append({"tick": tick, "text": s, "importance": imp})
        state["memories"][a] = state["memories"][a][-200:]

    # 5. story
    weather = weather_for(now, tick)
    summary = {"tick": tick, "time": fmt_time(now), "weather": weather,
               "where": {first(world, a): world["zones"][start_pos[a]]["name"] for a in agents},
               "moods": {first(world, a): results[a]["emotional_state"] for a in agents},
               "lines": public_lines + town_news}
    try:
        story = brain.story(summary)
    except Exception as e:
        story = " ".join(public_lines)
        log.append({"agent": "story", "raw": f"ERROR: {e}"})

    # periodic reflections
    if cfg["reflect_every_ticks"] and tick % cfg["reflect_every_ticks"] == 0:
        for a in agents:
            try:
                recent = [m["text"] for m in state["memories"][a][-20:]]
                state["reflections"][a] = (state["reflections"][a] + brain.reflect(world["agents"][a]["name"], recent))[-8:]
            except Exception as e:
                log.append({"agent": a, "raw": f"REFLECT ERROR: {e}"})

    state["matters"] += new_matters

    # every few ticks, a new "Previously on Sumter Oaks" episode
    tick_summary = {"tick": tick, "time": fmt_time(now), "story": story,
                    "dialogue": [describe(world, a, results[a]["action"]) for a in agents
                                 if results[a]["action"]["type"] == "SPEAK"]}
    pending = state.setdefault("recap_pending", [])
    pending.append(tick_summary)
    episodes = load_json(EPISODES) if EPISODES.exists() else {"episodes": []}
    every = cfg.get("recap_every_ticks", 6)
    if not episodes["episodes"] and len(pending) == 1:
        # First episode on a town that's already running: catch up from the saved ticks.
        earlier = []
        for f in sorted(TICKS.glob("tick_*.json"))[-12:]:
            t = load_json(f)
            if t["tick"] < tick:
                earlier.append({"tick": t["tick"], "time": t["simulation_time"], "story": t.get("story", ""),
                                "dialogue": [describe(world, a, r["action"]) for a, r in t.get("results", {}).items()
                                             if r["action"]["type"] == "SPEAK"]})
        pending[:0] = earlier
    if every and (len(pending) >= every or not episodes["episodes"]):
        try:
            write_episode(world, state, episodes, pending, brain, results)
            state["recap_pending"] = []
        except Exception as e:  # keep the material and try again next tick
            log.append({"agent": "recap", "raw": f"RECAP ERROR: {e}"})
            print(f"  ! Recap skipped this tick: {e}")

    # what this tick cost, plus running totals by day and overall
    cost = brain.cost_report()
    spend = state.setdefault("spend", {"total_usd": 0.0, "by_day": {}})
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    except Exception:  # Windows without tzdata: fall back to UTC days
        tz = dt.timezone.utc
    today = dt.datetime.now(tz).strftime("%Y-%m-%d")
    spend["total_usd"] = round(spend["total_usd"] + cost["usd"], 5)
    day = spend["by_day"].setdefault(today, {"ticks": 0, "usd": 0.0})
    day["ticks"] += 1
    day["usd"] = round(day["usd"] + cost["usd"], 5)
    if cost["usd"] > 0:
        state["recent_tick_costs"] = (state.get("recent_tick_costs", []) + [cost["usd"]])[-24:]

    # 6. write the tick for the viewer
    tick_doc = {
        "tick": tick,
        "simulation_time": fmt_time(now),
        "weather": weather,
        "positions": start_pos,
        "story": story,
        "events": town_news + [line for line in public_lines if "said to" not in line],
        "results": results,
        "matters": new_matters,
        "laws": state["laws"],
        "cost": cost,
    }
    name = f"tick_{tick:04d}.json"
    save_json(TICKS / name, tick_doc)
    index_path = TICKS / "index.json"
    index = load_json(index_path) if index_path.exists() else {"ticks": []}
    if name not in index["ticks"]:
        index["ticks"].append(name)
    index["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_json(index_path, index)
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"tick_{tick:04d}.json").write_text(json.dumps({"prompts": prompts, "replies": log}, indent=2, ensure_ascii=False), encoding="utf-8")

    if real_clock(cfg):
        state["last_tick_date"] = now.date().isoformat()
    else:
        # town calendar: advance the clock; overnight, everyone goes home
        nxt = now + dt.timedelta(minutes=cfg["minutes_per_tick"])
        if nxt.time() >= parse_hhmm(cfg["day_end"]) or nxt.date() != now.date():
            day = (now + dt.timedelta(days=1)).date()
            nxt = dt.datetime.combine(day, parse_hhmm(cfg["day_start"]))
            for a in agents:
                state["positions"][a] = world["agents"][a]["home"]
                state["perceptions"][a] = [f"A new morning in {world['town_name']}. You're at home."]
        state["time"] = nxt.isoformat(timespec="minutes")
    state["tick"] = tick + 1
    fund = update_fund(cfg, state)
    print(f"  Cloud: {fund['level']} · balance ${fund['balance_usd']:.2f} · about {fund['days_left']} days left")
    save_json(STATE_FILE, state)
    return tick_doc


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Run Sumter Oaks ticks.")
    ap.add_argument("--ticks", type=int, default=1, help="how many ticks to run (default 1)")
    ap.add_argument("--test", action="store_true", help="use the cheap test model for everything")
    ap.add_argument("--mock", action="store_true", help="no API calls; canned actions")
    ap.add_argument("--reset", action="store_true", help="delete data/ and start from seed.json")
    ap.add_argument("--force", action="store_true", help="run even while the town is asleep (testing)")
    args = ap.parse_args()

    key_source = load_env()
    cfg = load_json(ROOT / "config.json")
    world = load_json(ROOT / "world.json")
    template = (ROOT / "prompt_template.md").read_text(encoding="utf-8")

    if args.reset and DATA.exists():
        shutil.rmtree(DATA)
    state = load_json(STATE_FILE) if STATE_FILE.exists() else new_state(world, load_json(ROOT / "seed.json"), cfg)
    brain = Brain(cfg, test=args.test, mock=args.mock)
    mode = "mock" if args.mock else f"{brain.resident_model} / {brain.story_model}"
    print(f"Sumter Oaks: running {args.ticks} tick(s) from tick {state['tick']} [{mode}]")
    if not args.mock:
        print(f"Key loaded from: {key_source}")

    # On the real clock, the town sleeps outside day hours.
    if real_clock(cfg) and not args.force:
        now = town_now(cfg, state)
        if not awake(cfg, now):
            print(f"It's {now:%I:%M %p} in {world['town_name']}; everyone's asleep until "
                  f"{cfg['day_start']}. No tick run. (Use --force to run anyway.)")
            return

    # When the cloud is empty and pausing is on, the town sleeps until someone chips in.
    if cfg.get("support", {}).get("pause_when_empty") and not args.mock:
        fund = update_fund(cfg, state)
        save_json(STATE_FILE, state)
        if fund["level"] == "empty":
            print(f"The cloud is empty (balance ${fund['balance_usd']:.2f}), so the town is paused. No ticks run.")
            return
    brain.preflight()

    for _ in range(args.ticks):
        doc = run_tick(world, state, cfg, template, brain)
        print(f"\nTick {doc['tick']}  {doc['simulation_time']}  {doc['weather']}")
        for a, r in doc["results"].items():
            act = r["action"]
            extra = f"  <- {r['engine_note']}" if "engine_note" in r else ""
            print(f"  {first(world, a):6} [{r['emotional_state']}] {act['type']} {act['parameters'].get('target')}: "
                  f"{act['parameters'].get('content', '')}{extra}")
        print(f"  Story: {doc['story']}")
        print(f"  Cost: ${doc['cost']['usd']:.4f}")
    spend = state.get("spend", {})
    if spend:
        print(f"\nSpend so far: ${spend['total_usd']:.4f} total")
        for day, d in sorted(spend["by_day"].items())[-3:]:
            print(f"  {day}: {d['ticks']} ticks, ${d['usd']:.4f}")
    print(f"\nSaved to {TICKS}")


if __name__ == "__main__":
    main()
