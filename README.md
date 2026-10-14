# recuring

A Telegram bot for tasks that keep coming back — plus a web dashboard that doesn't feel bolted on.

Not another to-do list. This one is built around the fact that most tasks in real life repeat: take the trash out every Tuesday, water the plants every 3 days, pay rent on the 1st. `recuring` treats recurrence as a first-class thing instead of a checkbox you tick on manually every time.

Runs as a single Python process (bot + dashboard in one), talks to Postgres, and is small enough that one person can actually read all of it in an evening.

<img src="docs/screenshots/login.png" width="360" alt="Dashboard login screen">

*(The rest of the dashboard — task list, history, templates — isn't pictured here. My own instance has real tasks on it that I'd rather not publish, and the login screen is the only part of it that's nobody's business but Telegram's.)*

## What it actually does

- **Natural language dates.** `Buy milk tomorrow at 18:00`, `Купить молоко завтра в 18:00`, `Kup mleko jutro o 18:00` — all parsed the same way, no slash-commands required. Works in Russian, English and Polish, including relative stuff like "in 3 days" / "через 3 дня" / "za 3 dni".
- **Recurring and one-off tasks**, with snoozing, tags, subtasks, and a "quiet hours" window so it doesn't ping you at 2am.
- **A web dashboard** per user or per group chat, behind a token + password, with its own session cookies (HMAC-signed, no external session store). Doubles as a Telegram Mini App — open it from the bot's menu button and you're logged in via `initData`, no password typing on your phone.
- **Task templates**, CSV export, history with a little activity chart, task sharing via Telegram's inline share mechanism, and optional photos attached to tasks.
- **Group chats work too** — tasks can belong to a chat, assigned members get notified, and the history shows who actually did what.

## Why Postgres pooling is pinned so specifically

If you read `db/core.py` you'll notice a fairly opinionated comment block about Supabase connection modes. Short version: this was built to run for free, on Render's free tier, against Supabase's free tier, and those two free tiers have opinions about each other that aren't documented anywhere obvious. Session pooler, not transaction pooler. IPv4 pooler host, not the direct IPv6-only one. If you're deploying somewhere else this won't matter to you, but if you hit `db_name` errors or prepared-statement weirdness, that comment is where the answer lives.

## Running it locally

```bash
git clone https://github.com/NiceTryJony/recuring.git
cd recuring
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp env.example .env   # fill in BOT_TOKEN and DATABASE_URL at minimum
python bot.py
```

You need:

- A Postgres database (schema is created automatically on first run, see `db/schema.py`)
- A bot token from [@BotFather](https://t.me/BotFather)
- `DASHBOARD_BASE_URL` if you want the dashboard reachable and the Mini App button to show up (Telegram requires HTTPS for Mini Apps, so `localhost` won't cut it — use something like ngrok while developing)

## Tests

```bash
pytest                 # unit + integration (needs TEST_DATABASE_URL for the integration ones)
pytest -m e2e           # headless-browser dashboard tests, needs `playwright install chromium`
pytest -m perf -s       # dashboard performance baseline, same browser requirement
```

CI runs the default suite against a throwaway Postgres container on every push, on 3.11 and 3.12.

## Layout

```
bot.py           entry point — python bot.py and that's it
tgbot/           aiogram handlers, scheduling (APScheduler), keyboards, i18n glue
dashboard/       aiohttp routes, server-rendered HTML fragments, auth/CSRF/session logic
db/              every SQL query in the project, one module per table-ish concern
date_parser.py   the natural-language date parsing, framework-free and unit-testable on its own
i18n.py          ru / en / pl strings
```

Each package has its own README with the boring-but-important stuff (import order, what not to delete, which table is which). Start there before touching anything cross-cutting.

## Deploying

`render.yaml` is the actual deployment config this project runs on — Render web service, Frankfurt region, free plan. It's committed because the required env vars are documented right there better than they would be in prose. Nothing about the code requires Render specifically; swap the platform and keep the Postgres connection notes above in mind.

## License

No license file yet — ask before reusing chunks of this commercially. Happy to talk about it if you're interested.
