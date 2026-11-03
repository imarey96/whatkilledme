# whatkilled.me

Paste a Warcraft Logs link and see what killed your tank: a plain verdict, pressure by pull or phase, a cooldown timeline, deaths, and side-by-side attempts.

Works on Mythic+ and raids for all six tank specs: Blood Death Knight, Vengeance Demon Hunter, Guardian Druid, Brewmaster Monk, Protection Paladin and Protection Warrior. Raid fights are split by boss phase and also show how the tanks shared damage and taunts. What each spec tracks lives in `tanks.py`.

## Run it

1. Create a client at https://www.warcraftlogs.com/api/clients and copy `.env.example` to `.env` with its ID and secret.
2. Install and start:

```bash
pip install -r requirements.txt
```

```bash
python app.py
```

Open http://127.0.0.1:8000.

The command-line version uses the same engine:

```bash
python analyze.py "https://www.warcraftlogs.com/reports/CODE?fight=4" --compare 3
```

## Hosting on Railway

The repo has a `Dockerfile` and `railway.json`, so Railway builds and health-checks it automatically.

1. Create a Railway project from this GitHub repo.
2. Add `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` as service variables.
3. Generate a public domain, or add your own under the service's networking settings.

Things to know:

- All visitors share one Warcraft Logs API allowance. Results are cached for 15 minutes, but heavy traffic can still run it out.
- The cache and rate limits live in memory, so they reset on each deploy. Run one replica, because replicas wouldn't share them.
- The image sets `TRUST_PROXY=1` so rate limits apply per visitor. Outside a proxy, leave it unset, or visitors could fake their address.
