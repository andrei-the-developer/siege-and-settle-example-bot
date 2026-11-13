# Siege & Settle example bot (Python)

A small, readable bot that plays one account in a **Bot world** of [Siege & Settle](https://www.siegeandsettle.com)
through the game's public REST API. One file, Python 3.9 or newer, standard library only: no packages to install.

Siege & Settle is a strategy game played in the browser: you grow a village, train troops, trade, join an alliance
and fight for the control points of a world that lasts a few weeks. Next to the Classic worlds, played from the browser
with no scripts or API keys, there are **Bot worlds**, where people and programs share the same map and the same rules.
This bot is a starting point for yours: it shows how to authenticate, keep a play ticket, stay within the rate
limits, read the account and send commands, with a simple strategy you can replace.

## What you need

- An account on the game (a username and a password; [siegeandsettle.com](https://siegeandsettle.com)).
- A **Bot world**: `GET https://siegeandsettle.com/v1/worlds` lists the worlds, and Bot worlds have `"kind": "bot"`.
  The world list in the game marks them too.
- An **API key**: in a world, open your **Profile**, then **Edit profile**; the **API key** section creates one
  (a name and your password). The key is shown **once**: copy it then, and keep it secret. Whoever holds it plays as
  you in Bot worlds. The same section lists your keys and revokes them.

The wiki page [Playing with an API key](https://siegeandsettle.com/wiki/mechanics/api-key) explains the same steps
with curl, and the full API reference is at [siegeandsettle.com/v1/docs](https://siegeandsettle.com/v1/docs).

## Install

```bash
git clone https://github.com/andrei-the-developer/siege-and-settle-example-bot.git
cd siege-and-settle-example-bot
python3 --version   # 3.9 or newer
```

Nothing else: the bot uses only the standard library.

## Configure

The bot reads its settings from environment variables; nothing about your account is written in the code.

| Variable | Required | Meaning |
|---|---|---|
| `GAME_BASE_URL` | yes | the address of the game: `https://siegeandsettle.com` |
| `GAME_API_KEY` | yes | your API key (`xk_...`) |
| `GAME_WORLD` | yes | the id of a Bot world, from `GET /v1/worlds` |
| `GAME_TRIBE` | no | the tribe to join with the first time: 1 Romans (default), 2 Germanic Tribes, 3 Dacians |
| `GAME_TURN_SECONDS` | no | seconds between two turns (default 60; a normal-speed world changes slowly) |
| `GAME_MAX_QUEUE` | no | how many builds the bot keeps queued per village (default 2) |
| `GAME_TRAIN` | no | `0` to never train troops (default `1`) |

For example, in a file `.env` that you never commit (the `.gitignore` here already leaves it out):

```bash
export GAME_BASE_URL=https://siegeandsettle.com
export GAME_API_KEY=xk_YOUR_KEY
export GAME_WORLD=your-bot-world-id
export GAME_TRIBE=1
```

## Run

```bash
source .env
python3 siege_bot.py --dry-run --once   # one turn: read and decide, send nothing
python3 siege_bot.py --once             # one turn for real
python3 siege_bot.py                    # play a turn every GAME_TURN_SECONDS until Ctrl+C
python3 siege_bot.py -v                 # the same, with every request's details
```

A run in a new account looks like this (a world sped up 200 times, so a turn every 15 seconds):

```text
21:15:17 world My Bot World (my-bot-world), speed 200
21:15:19 MyBot's village: resources [750, 750, 750, 750] / 800, queue 0/5
21:15:19 upgrade slot 1 (building 1) to level 1: tutorial goal
21:15:34 MyBot's village: resources [696, 602, 682, 666] / 800, queue 0/5
21:15:34 upgrade slot 26 (building 15) to level 2: tutorial goal
21:15:50 claimed tutorial step: Fill the build queue (reward [90, 50, 80, 30])
21:15:50 MyBot's village: resources [617, 559, 614, 651] / 800, queue 1/5
21:15:50 construct slot 19 (building 16) to level 1: tutorial goal
21:16:06 claimed tutorial step: Raise the main building (reward [110, 160, 90, 70])
21:16:06 MyBot's village: resources [608, 456, 611, 621] / 800, queue 0/5
21:16:06 upgrade slot 26 (building 15) to level 3: tutorial goal
21:16:21 claimed tutorial step: Build a rally point (reward [120, 70, 100, 40])
21:16:21 MyBot's village: resources [615, 558, 608, 667] / 800, queue 1/5
21:16:22 construct slot 20 (building 19) to level 1: tutorial goal
21:16:37 claimed tutorial step: Main building level 3 (reward [210, 140, 260, 120])
21:16:37 MyBot's village: resources [536, 495, 455, 598] / 800, queue 0/5
21:16:38 construct slot 21 (building 23) to level 1: tutorial goal
21:16:53 claimed tutorial step: Hide a cranny (reward [30, 30, 30, 40])
21:16:53 MyBot's village: resources [718, 592, 692, 718] / 800, queue 0/5
21:16:54 construct slot 22 (building 18) to level 1: tutorial goal
21:16:54 training 1 of unit 21
```

Run the tests of the decisions (no server needed):

```bash
python3 -m unittest -v
```

## What the bot does

When it starts:

1. Checks that the world is a Bot world (Classic worlds refuse API keys).
2. Reads the world's rules once (`GET /v1/worlds/{world}/rules`): the cost of every building level and what each
   building requires.
3. Joins the world with `GAME_TRIBE` if the account is not in it yet (`POST .../join`).

Then every turn:

1. **Reads the account**: `GET .../overview` returns every village with its resources, production, storage, building
   slots and build queue.
2. **Follows the tutorial** (`GET .../quests`): it claims a step whose goal is reached (`POST .../quests/claim`), one
   per turn and only when the reward fits in the stores, since what does not fit is lost. The tutorial pays a reward
   for every step, which helps most in the first hours, when resources are tightest.
3. **Builds**, in each village, while the build queue holds fewer than `GAME_MAX_QUEUE` jobs (when a store is 90%
   full it fills the game's queue instead, since paying now beats losing the production). The first of these that the
   village can pay for, and whose crop upkeep the village can feed:
   - a crop field when the crop surplus is small (every building and troop eats crop);
   - the next building the tutorial asks for (never one already queued);
   - the warehouse or the granary when it is 80% full;
   - the lowest resource field, of the resource the village has least of;
   - the main building, when it is below the average field level (it makes every build faster).

   Upgrades go to `POST .../villages/{village}/upgrade`, new buildings to `.../construct`.
4. **Trains** when the village has a barracks and nothing training there: the cheapest unit it offers
   (`GET .../train-options?building=19`), paid only from what is above half of the stores, so building never starves
   (`POST .../villages/{village}/train`).

Every refusal is logged with its `code` (`not_enough_resources`, `queue_full`, ...): the game refuses a bot exactly
as it refuses a player, and the bot simply tries again next turn.

## How it talks to the game

All of it is in the `Client` class:

- **Authentication**: every call carries `Authorization: Bearer <key>`. With a key there is no proof-of-work.
- **Play ticket**: most worlds run on an engine of their own. `POST /v1/worlds/{world}/ticket` returns a ticket valid
  for 12 hours, sent as `X-Play-Ticket` on every call under `/v1/worlds/{world}/`. The client renews it ten minutes
  before it expires, or when a call answers `401 ticket_invalid` / `ticket_revoked`. A world that answers the ticket
  request with `409 world_not_direct` needs no ticket.
- **Rate limits**: 10 calls a second with a key and 5 commands a second in a Bot world. The client spaces its calls
  to about 4 a second, and on `429 rate_limited` waits for `Retry-After` seconds.
- **Safe retries**: every command carries an `Idempotency-Key`, so repeating it after a lost answer returns the first
  answer instead of running it twice.
- **Errors**: an `ApiError` with the HTTP status and the stable `code` of the answer. Match on the code, never on the
  message, which is meant for people.

## Make it yours

The decisions are plain functions of what the game returned, so they are easy to change and to test:
`choose_build`, `choose_training` and `tutorial_plan` in `siege_bot.py`, with their tests in `test_siege_bot.py`.
Some ideas, with the API calls they need (all in the [API reference](https://siegeandsettle.com/v1/docs)):

- **Raid**: find targets with `GET .../map?x=..&y=..` or `GET .../tiles?x=..&y=..`, check the travel time with
  `GET .../villages/{village}/send-preview`, then `POST .../villages/{village}/send` with
  `{"to": {"x": .., "y": ..}, "mission": 4, "units": [..]}` (units by position 1-10 in your tribe's order). Read the
  outcome in `GET .../reports`.
- **Defend**: `overview.movements` lists the attacks coming to your villages; build the wall, train defensive units,
  or turn on evasion (`PUT .../villages/{village}/evasion`).
- **Trade**: `GET .../villages/{village}/market` and `POST .../villages/{village}/offers` balance your resources.
- **Expand**: `GET .../villages/{village}/expansion` tells the culture points you need for the next village.
- **Join an alliance**: `GET .../alliances`, then accept an invitation with `POST .../invitations/{id}/accept`.
- **Several accounts**: run one bot per account, each with its own key. Every account is a player like any other.

### Or connect an AI agent instead

The same game is offered to AI agents as tools by the MCP server at `https://siegeandsettle.com/mcp` (streamable
HTTP), with the same `Authorization: Bearer <key>` header and no ticket. Any MCP client that lets you set a header
works; the [bots page](https://www.siegeandsettle.com/bots/) has ready recipes for several clients and APIs.

## Rules for bots

- Programs play **only in Bot worlds**. A Classic world refuses an API key with `403 bots_forbidden`.
- A bot is a player like any other: the same rules, the same costs and times, the same refusals.
- Stay within the rate limits: beyond them the game answers `429` until the quota refills.
- Keep your key secret. If it leaks, revoke it in your profile and create a new one.

## Troubleshooting

| You see | It means |
|---|---|
| `missing environment variable` | set `GAME_BASE_URL`, `GAME_API_KEY` and `GAME_WORLD` (see Configure) |
| `401 invalid_key` | the key is wrong or revoked: create a new one in your profile |
| `403 bots_forbidden` | the world is a Classic world: pick one with `"kind": "bot"` |
| `401 ticket_missing` / `ticket_invalid` | the client gets a new ticket by itself; if it keeps happening, check the clock of your machine |
| `403 not_member` | the account is not in the world yet: the bot joins at start, or join from the browser |
| `could not ... not_enough_resources` | normal: the bot waits for production and tries again next turn |
| `could not ... queue_full` | the build queue is full: lower `GAME_MAX_QUEUE` or wait |
| `rate limited, waiting N s` | the bot calls too fast for your account (another program using the same key?) |
| the bot does nothing for a while | everything it wants costs more than the stores hold: it waits for production |

## License

MIT, see [LICENSE](LICENSE).
