# Rinkside

Follow NHL, PWHL, IIHF and Olympic hockey teams and players in your own time zone, see every game on one calendar, and subscribe to a live schedule in Apple Calendar.

## Run it locally

Needs only Python 3 (no packages to install):

```bash
python3 server.py
```

Then open http://localhost:8765. On a Mac you can also double-click `Start Rinkside.command`.

## Where the data comes from

| League | Source |
|---|---|
| NHL | api-web.nhle.com (official NHL web API) and search.d3.nhle.com |
| PWHL | thepwhl.com schedule page; rosters from the team articles on Wikipedia |
| IIHF and Olympics | stats.iihf.com |

The server refreshes everything every few minutes and caches it in `data/`.

## Calendar subscriptions

`/cal.ics?f=<follow keys>` returns a live iCalendar feed. Apple Calendar only subscribes to feeds served over **HTTPS**, so subscriptions work once the site is deployed to an HTTPS host, not from `http://localhost`.

## Deploy on Render (free)

1. Push this folder to a GitHub repository.
2. In Render, choose **New → Blueprint**, pick the repository, and click **Apply**. `render.yaml` sets everything up.
3. Open the `https://rinkside-xxxx.onrender.com` address Render gives you.

Free Render services sleep after 15 minutes without visitors and take 30–60 seconds to wake up. Calendar apps retry, so subscriptions catch up on the next refresh.

## Files

- `server.py`: data fetching, caching, schedule API and calendar feeds
- `index.html`: the whole front end (list view, month calendar, follows, watch links)
- `Start Rinkside.command`: macOS double-click launcher
- `render.yaml`, `requirements.txt`, `.python-version`: Render hosting config
- `iihf_seed.json`: known IIHF tournaments, so a fresh deploy doesn't have to scan for them
