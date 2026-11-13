#!/usr/bin/env python3
"""A small example bot for Siege & Settle.

It plays one account in a Bot world through the public REST API, with the Python standard library only:

  1. gets a play ticket for the world and joins it the first time;
  2. every turn reads the overview of the account (villages, resources, build queue);
  3. claims the tutorial rewards whose goal is reached, and builds towards the current tutorial goal;
  4. otherwise grows the economy: storage when it is nearly full, crop when food runs short, then the lowest
     resource field, then the main building;
  5. trains a few troops in the barracks when the stores are comfortable.

Configuration comes from environment variables (see README.md). Nothing about the account is written here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("siege-bot")

# Building types (gid) this bot refers to, as named in the API documentation. GET /v1/worlds/{world}/rules lists
# every building with its costs and requirements; the bot reads everything else from there.
WAREHOUSE = 10
GRANARY = 11
MAIN_BUILDING = 15
BARRACKS = 19
CROPLAND = 4

# Resources are always [wood, clay, iron, crop].
CROP = 3

# Slots: resource fields come first, the village centre is 19-40 (40 is reserved for the wall).
FIRST_CENTRE_SLOT = 19
WALL_SLOT = 40


class ApiError(Exception):
    """An answer of the game that is not a success. `code` is stable: match on it, never on the message."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


# --------------------------------------------------------------------------------------------------------------
# The client: authentication, play ticket, pacing and retries. Nothing in here knows about the game's rules.
# --------------------------------------------------------------------------------------------------------------


class Client:
    """A thin client for the game's REST API.

    - Every call carries the API key (`Authorization: Bearer ...`).
    - Calls under /v1/worlds/{world}/ also carry a play ticket (`X-Play-Ticket`), renewed before it expires.
    - Calls are paced to about 4 a second, under every published rate limit; a 429 waits as long as the game asks.
    - Commands (POST, PUT, PATCH, DELETE) carry an `Idempotency-Key`, so a retry after a network error never runs
      the same command twice.
    """

    TICKET_ERRORS = {"ticket_missing", "ticket_invalid", "ticket_revoked", "ticket_expired"}

    def __init__(self, base_url: str, key: str, world: str, min_interval: float = 0.25, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.world = world
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_call = 0.0
        self._ticket: Optional[str] = None
        self._ticket_expires = 0.0
        self._needs_ticket = True  # False once the world says it is served without tickets

    # -- public helpers ---------------------------------------------------------------------------------------

    def world_path(self, path: str) -> str:
        return f"/v1/worlds/{self.world}{path}"

    def get(self, path: str, **query: Any) -> Any:
        if query:
            path += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        return self.request("GET", self.world_path(path))

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        return self.request("POST", self.world_path(path), body if body is not None else {})

    # -- the play ticket --------------------------------------------------------------------------------------

    def ensure_ticket(self) -> None:
        if not self._needs_ticket:
            return
        if self._ticket and time.time() < self._ticket_expires - 600:
            return
        try:
            out = self._send("POST", self.world_path("/ticket"), {}, with_ticket=False)
        except ApiError as e:
            if e.code == "world_not_direct":  # this world is served without tickets
                self._needs_ticket = False
                self._ticket = None
                return
            raise
        self._ticket = out["ticket"]
        self._ticket_expires = float(out.get("expires_at") or time.time() + 3600)
        log.debug("new play ticket, valid until %s", time.strftime("%H:%M", time.localtime(self._ticket_expires)))

    # -- requests ---------------------------------------------------------------------------------------------

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        in_world = path.startswith(f"/v1/worlds/{self.world}/")
        if in_world:
            self.ensure_ticket()
        try:
            return self._send(method, path, body, with_ticket=in_world)
        except ApiError as e:
            if in_world and e.status == 401 and e.code in self.TICKET_ERRORS:
                self._ticket = None  # get a fresh ticket and try once more
                self.ensure_ticket()
                return self._send(method, path, body, with_ticket=True)
            raise

    def _send(self, method: str, path: str, body: Optional[dict], with_ticket: bool) -> Any:
        headers = {
            "Authorization": "Bearer " + self.key,
            "Accept": "application/json",
            "User-Agent": "example-python-bot/1.0",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        if with_ticket and self._ticket:
            headers["X-Play-Ticket"] = self._ticket

        for attempt in range(6):
            self._pace()
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    err = json.loads(raw) if raw else {}
                except ValueError:
                    err = {}
                if e.code == 429:
                    wait = float(e.headers.get("Retry-After") or 1)
                    log.info("rate limited, waiting %.0f s", wait)
                    time.sleep(wait)
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(e.code, err.get("code", "http_%d" % e.code), err.get("message", "")) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt >= 3:
                    raise
                log.warning("network error (%s), retrying", e)
                time.sleep(2 ** attempt)
        raise ApiError(429, "rate_limited", "still rate limited after several waits")

    def _pace(self) -> None:
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


# --------------------------------------------------------------------------------------------------------------
# Decisions: pure functions of what the game told us, so they are easy to read, test and change.
# --------------------------------------------------------------------------------------------------------------


@dataclass
class Build:
    """One build order: upgrade the building in `slot`, or construct `gid` in the empty `slot`."""

    action: str  # "upgrade" or "construct"
    slot: int
    gid: int
    level: int  # the level it will reach
    reason: str


class Rules:
    """The parts of GET /rules the bot uses: the cost of every building level and the building requirements."""

    def __init__(self, raw: dict):
        self.buildings = {b["gid"]: b for b in raw.get("buildings", [])}
        limits = raw.get("limits", {})
        self.field_slots = int(limits.get("field_slots", 18))

    def cost(self, gid: int, level: int) -> Optional[list]:
        b = self.buildings.get(gid)
        if not b:
            return None
        for lv in b.get("levels", []):
            if lv.get("level") == level:
                return lv.get("cost")
        return None

    def upkeep(self, gid: int, level: int) -> int:
        """The crop a level of a building eats every hour (its population)."""
        b = self.buildings.get(gid)
        for lv in (b or {}).get("levels", []):
            if lv.get("level") == level:
                return int(lv.get("pop", 0))
        return 0

    def max_level(self, gid: int) -> int:
        b = self.buildings.get(gid)
        return int(b.get("max_level", 0)) if b else 0

    def requirements_met(self, gid: int, levels: dict) -> bool:
        """`levels` maps a gid to the highest level the village has (or has queued) of it."""
        b = self.buildings.get(gid)
        if not b:
            return False
        req = b.get("requirements") or {}
        if req.get("special"):
            return False  # special buildings (artifacts, capital only, ...) are left to humans
        return all(levels.get(g, 0) >= lv for g, lv in req.get("levels", []))


def job_field(job: dict, name: str) -> Any:
    """A field of a queued job. The build queue spells its fields with a capital letter (Slot, Gid, Level)."""
    return job.get(name, job.get(name.capitalize()))


def planned_levels(village: dict) -> dict:
    """The level every slot will have once the queued jobs end: {slot: (gid, level)}."""
    out = {s["slot"]: (s["gid"], s["level"]) for s in village.get("slots", [])}
    for job in village.get("jobs", []):
        slot, gid, level = job_field(job, "slot"), job_field(job, "gid"), job_field(job, "level")
        if slot is None or level is None:
            continue
        cur_gid, cur_level = out.get(slot, (gid, 0))
        out[slot] = (gid or cur_gid, max(cur_level, level))
    return out


def best_levels(planned: dict) -> dict:
    """The highest planned level of every building type in the village: {gid: level}."""
    out: dict = {}
    for gid, level in planned.values():
        if gid:
            out[gid] = max(out.get(gid, 0), level)
    return out


def affordable(cost: Optional[list], resources: list) -> bool:
    return cost is not None and all(have >= need for have, need in zip(resources, cost))


def order_for(gid: int, planned: dict, rules: Rules, reason: str, target: Optional[int] = None) -> Optional[Build]:
    """Raise building `gid` by one level (its lowest copy), or construct it in a free slot when the village has none.
    With `target`, nothing is ordered once the building reaches that level."""
    own = [(level, slot) for slot, (g, level) in planned.items() if g == gid]
    if own:
        level, slot = min(own)
        if target is not None and max(lv for lv, _ in own) >= target:
            return None
        if level >= rules.max_level(gid):
            return None
        return Build("upgrade", slot, gid, level + 1, reason)
    if not rules.requirements_met(gid, best_levels(planned)):
        return None
    free = [slot for slot, (g, _) in planned.items() if g == 0 and FIRST_CENTRE_SLOT <= slot < WALL_SLOT]
    if not free:
        return None
    return Build("construct", min(free), gid, 1, reason)


def choose_build(village: dict, rules: Rules, goals: Optional[list] = None, max_queue: int = 2) -> Optional[Build]:
    """What to build next in a village, or None. `goals` are the tutorial's building goals not reached yet."""
    resources = village.get("resources", [0, 0, 0, 0])
    caps = [village.get("max_store") or 1] * 3 + [village.get("max_crop") or 1]
    queue = village.get("queue") or {}
    limit = max_queue
    if any(have >= 0.9 * cap for have, cap in zip(resources, caps)):
        # the stores are about to overflow: paying for one more queued job now beats losing the production
        limit = int(queue.get("max", max_queue + 1))
    if len(village.get("jobs", [])) >= min(limit, int(queue.get("max", limit))):
        return None

    planned = planned_levels(village)
    candidates: list = []

    production = village.get("production", [0, 0, 0, 0])
    # The game refuses a building (not every crop field) when the crop left after the population of the village, of
    # every queued job and of the new level is 1 or less: the queued jobs eat crop too, before they are built.
    queued_upkeep = sum(rules.upkeep(job_field(j, "gid") or 0, job_field(j, "level") or 0) for j in village.get("jobs", []))
    free_crop = production[CROP] - queued_upkeep
    fields = sorted(
        (level, slot, gid)
        for slot, (gid, level) in planned.items()
        if slot <= rules.field_slots and gid and level < rules.max_level(gid)
    )

    # 1. Food: every building and troop eats crop; below a small surplus, a crop field comes first.
    if free_crop < 5:
        crop_fields = [f for f in fields if f[2] == CROPLAND]
        if crop_fields:
            level, slot, gid = crop_fields[0]
            candidates.append(Build("upgrade", slot, gid, level + 1, "crop surplus is low"))

    # 2. The tutorial: its rewards pay for much of the start. A goal already queued is skipped, never queued twice.
    for goal in goals or []:
        if goal.get("kind") == "building" and goal.get("gid"):
            order = order_for(goal["gid"], planned, rules, "tutorial goal", target=goal.get("level"))
            if order:
                candidates.append(order)
                break

    # 3. Storage: never let production go to waste.
    max_store = village.get("max_store") or 1
    max_crop = village.get("max_crop") or 1
    if max(resources[:CROP]) >= 0.8 * max_store:
        candidates.append(order_for(WAREHOUSE, planned, rules, "warehouse nearly full"))
    if resources[CROP] >= 0.8 * max_crop:
        candidates.append(order_for(GRANARY, planned, rules, "granary nearly full"))

    # 4. The lowest resource field; among equals, the resource the village has least of.
    if fields:
        lowest = fields[0][0]
        ties = [f for f in fields if f[0] == lowest]
        level, slot, gid = min(ties, key=lambda f: resources[f[2] - 1] if 1 <= f[2] <= 4 else 0)
        candidates.append(Build("upgrade", slot, gid, level + 1, "lowest resource field"))

    # 5. The main building builds faster: keep it level with the fields.
    if fields:
        average = sum(f[0] for f in fields) / len(fields)
        if best_levels(planned).get(MAIN_BUILDING, 0) < average:
            candidates.append(order_for(MAIN_BUILDING, planned, rules, "main building behind the fields"))

    for c in candidates:
        if not c or not affordable(rules.cost(c.gid, c.level), resources):
            continue
        if c.gid != CROPLAND and free_crop - rules.upkeep(c.gid, c.level) <= 1:
            continue  # the game refuses it (not_enough_crop): a crop field has to come first
        return c
    return None


def choose_training(village: dict, options: list, reserve: float = 0.5) -> Optional[tuple]:
    """(unit id, amount) to train, or None: the cheapest unit the barracks offers, paid only from what is above
    `reserve` of the stores, so building never starves."""
    resources = village.get("resources", [0, 0, 0, 0])
    caps = [village.get("max_store", 0)] * 3 + [village.get("max_crop", 0)]
    spare = [max(0, have - reserve * cap) for have, cap in zip(resources, caps)]
    # `max` is how many the village could start now (0 when the unit is not researched yet)
    usable = [o for o in options if o.get("researched") and o.get("max", 0) > 0 and o.get("cost")]
    if not usable:
        return None
    unit = min(usable, key=lambda o: sum(o["cost"]))
    amount = int(min(min(spare[i] // c for i, c in enumerate(unit["cost"]) if c > 0), unit["max"]))
    if amount < 1:
        return None
    return unit["unit"], amount


def reward_fits(reward: Optional[dict], village: Optional[dict]) -> bool:
    """A reward is paid into the capital's stores, and what does not fit is lost: claim it when it fits, or when the
    stores are nearly empty anyway (a small warehouse may never hold a big reward)."""
    if not reward or not village:
        return True
    resources = village.get("resources", [0, 0, 0, 0])
    caps = [village.get("max_store", 0)] * 3 + [village.get("max_crop", 0)]
    amounts = [reward.get(k, 0) for k in ("wood", "clay", "iron", "crop")]
    fits = all(have + add <= cap for have, add, cap in zip(resources, amounts, caps))
    nearly_empty = all(have <= 0.25 * cap for have, cap in zip(resources, caps))
    return fits or nearly_empty


def tutorial_plan(quests: dict, capital: Optional[dict] = None) -> tuple:
    """From GET /quests: the step to claim now [(id, title)] (at most one, and only if its reward fits in the
    capital's stores), and the building goals of the tutorial not reached yet, in the tutorial's order.

    The tutorial pays a reward for every step, which matters most in the first hours. Steps that ask a question
    (`needs_answer` other than "none") are left to humans."""
    if not quests.get("enabled") or quests.get("finished"):
        return [], []
    steps = quests.get("steps")
    if steps is None:  # a single chain: only the current quest can be claimed
        current = quests.get("current") or {}
        nxt = current.get("next") or []
        if current.get("met") and nxt and current.get("needs_answer", "none") == "none":
            if reward_fits(nxt[0].get("reward"), capital):
                return [(nxt[0]["id"], current.get("title"))], []
            return [], []
        cond = current.get("condition") or {}
        return [], [cond] if cond.get("kind") == "building" and not current.get("met") else []
    claims = [(s["id"], s.get("title")) for s in steps
              if s.get("met") and not s.get("claimed") and s.get("needs_answer", "none") == "none"
              and reward_fits(s.get("reward"), capital)][:1]
    goals = [s["condition"] for s in steps
             if not s.get("met") and not s.get("claimed") and (s.get("condition") or {}).get("kind") == "building"]
    return claims, goals


# --------------------------------------------------------------------------------------------------------------
# The bot: one turn reads the account, decides and acts.
# --------------------------------------------------------------------------------------------------------------


class Bot:
    def __init__(self, client: Client, tribe: int = 1, max_queue: int = 2, train: bool = True, dry_run: bool = False):
        self.c = client
        self.tribe = tribe
        self.max_queue = max_queue
        self.train = train
        self.dry_run = dry_run
        self.rules: Optional[Rules] = None

    def start(self) -> None:
        world = self.c.request("GET", f"/v1/worlds/{self.c.world}")
        if world.get("kind") != "bot":
            raise SystemExit(f"world {self.c.world} is a {world.get('kind')} world: API keys only play in Bot worlds")
        log.info("world %s (%s), speed %s", world.get("name"), self.c.world, world.get("speed"))
        self.rules = Rules(self.c.get("/rules"))
        try:
            self.c.get("/overview")
        except ApiError as e:
            if e.code != "not_member":
                raise
            out = self.c.post("/join", {"tribe": self.tribe})
            log.info("joined the world: player %s, first village %s", out.get("player"), out.get("village"))

    def turn(self) -> None:
        overview = self.c.get("/overview")
        villages = overview.get("villages", [])
        capital = next((v for v in villages if v.get("capital")), villages[0] if villages else None)
        goals = self.tutorial(capital)
        for village in villages:
            name = village.get("name", village["id"])
            res = village.get("resources", [])
            log.info("%s: resources %s / %s, queue %d/%d", name, [int(r) for r in res], village.get("max_store"),
                     len(village.get("jobs", [])), (village.get("queue") or {}).get("max", 0))
            self.build(village, goals)
            if self.train:
                self.train_troops(village)

    def tutorial(self, capital: Optional[dict]) -> list:
        """Claims a tutorial step whose goal is met, and returns the building goals of the tutorial."""
        try:
            quests = self.c.get("/quests")
        except ApiError as e:
            log.debug("no tutorial: %s", e)
            return []
        claims, goals = tutorial_plan(quests, capital)
        for quest_id, title in claims:
            if self.dry_run:
                log.info("would claim tutorial step: %s", title)
                continue
            try:
                out = self.c.post("/quests/claim", {"quest": quest_id})
                log.info("claimed tutorial step: %s (reward %s)", title, (out or {}).get("reward"))
            except ApiError as e:
                log.info("tutorial step %s not claimed: %s", quest_id, e.code)
        return goals

    def build(self, village: dict, goals: list) -> None:
        order = choose_build(village, self.rules, goals, self.max_queue)
        if not order:
            return
        what = f"{order.action} slot {order.slot} (building {order.gid}) to level {order.level}: {order.reason}"
        if self.dry_run:
            log.info("would %s", what)
            return
        path = f"/villages/{village['id']}/{order.action}"
        body = {"slot": order.slot} if order.action == "upgrade" else {"slot": order.slot, "gid": order.gid}
        try:
            self.c.post(path, body)
            log.info("%s", what)
        except ApiError as e:
            # The game refuses exactly as it would refuse a player (not_enough_resources, queue_full, ...).
            log.info("could not %s: %s", what, e.code)

    def train_troops(self, village: dict) -> None:
        if not any(s["gid"] == BARRACKS and s["level"] > 0 for s in village.get("slots", [])):
            return
        if any(job_field(t, "gid") in (None, BARRACKS) for t in village.get("training", [])):
            return  # one batch at a time keeps resources for building
        try:
            options = self.c.get(f"/villages/{village['id']}/train-options", building=BARRACKS)
        except ApiError as e:
            log.info("no train options: %s", e.code)
            return
        choice = choose_training(village, options.get("units") or [])
        if not choice:
            return
        unit, amount = choice
        if self.dry_run:
            log.info("would train %d of unit %d", amount, unit)
            return
        try:
            self.c.post(f"/villages/{village['id']}/train", {"unit": unit, "amount": amount})
            log.info("training %d of unit %d", amount, unit)
        except ApiError as e:
            log.info("could not train: %s", e.code)


# --------------------------------------------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Example bot for Siege & Settle Bot worlds")
    p.add_argument("--once", action="store_true", help="play one turn and exit")
    p.add_argument("--dry-run", action="store_true", help="read and decide, but send no command")
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    try:
        base_url = os.environ["GAME_BASE_URL"]
        key = os.environ["GAME_API_KEY"]
        world = os.environ["GAME_WORLD"]
    except KeyError as e:
        print(f"missing environment variable {e}: see README.md", file=sys.stderr)
        return 2
    tribe = int(os.environ.get("GAME_TRIBE", "1"))
    turn_seconds = float(os.environ.get("GAME_TURN_SECONDS", "60"))
    max_queue = int(os.environ.get("GAME_MAX_QUEUE", "2"))
    train = os.environ.get("GAME_TRAIN", "1") != "0"

    bot = Bot(Client(base_url, key, world), tribe=tribe, max_queue=max_queue, train=train, dry_run=args.dry_run)
    bot.start()
    while True:
        try:
            bot.turn()
        except ApiError as e:
            log.warning("turn failed: %s", e)
        if args.once:
            return 0
        time.sleep(turn_seconds)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
