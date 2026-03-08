# 🏛 Architecture Job Bot

Scrapes **Indeed, ZipRecruiter, Glassdoor, LinkedIn** daily for entry-level, intern, and junior architect roles. Generates a beautiful HTML job board hosted free on GitHub Pages. Optional SMS alerts via Twilio.

---

## Setup (30 minutes, free)

### Step 1 — Fork this repo

Click **Fork** in the top-right on GitHub. This creates your own private copy.

### Step 2 — Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages**
2. Under "Source", select **Deploy from a branch**
3. Branch: `main`, Folder: `/docs`
4. Click **Save**

Your job board will be live at:
`https://YOUR-USERNAME.github.io/YOUR-REPO-NAME`

### Step 3 — Run the bot manually first

Go to **Actions** tab → **Daily Architecture Job Scrape** → **Run workflow**

This triggers the first scrape. After ~2 minutes, your site will have real jobs!

### Step 4 — (Optional) Enable SMS alerts

1. Sign up at [twilio.com](https://twilio.com) — free trial gives $15 credit
2. Get your Account SID, Auth Token, and a Twilio phone number
3. In your GitHub repo go to **Settings → Secrets and variables → Actions**
4. Add these secrets:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`  
   - `TWILIO_FROM` (your Twilio number, e.g. `+12025551234`)
   - `TWILIO_TO` (your cell number, e.g. `+19175559876`)
5. In `scraper.py`, change line 22 to: `TWILIO_ENABLED = True`

### Step 5 — Customize your searches

Edit the `SEARCH_TERMS` list in `scraper.py`:

```python
SEARCH_TERMS = [
    "intern architect",
    "entry level architect",
    "junior architect",
    "junior designer architecture",
    "architectural designer",
    # Add your own:
    "architectural intern New York",
    "IDP architect",
]
```

---

## Schedule

The bot runs every day at **9:00 AM UTC** (5 AM ET / 2 AM PT).

To change the time, edit `.github/workflows/daily-scrape.yml`:
```yaml
- cron: "0 9 * * *"   # Change 9 to any hour (0-23 UTC)
```

---

## Files

| File | Purpose |
|------|---------|
| `scraper.py` | Main scraper — edit search terms here |
| `requirements.txt` | Python dependencies |
| `.github/workflows/daily-scrape.yml` | GitHub Actions schedule |
| `docs/index.html` | Generated job board (auto-updated) |
| `all_jobs.json` | All scraped jobs (auto-updated) |
| `seen_jobs.json` | Tracks seen job IDs to avoid duplicates |

---

## Adding More Sources

The scraper uses **python-jobspy** which already covers:
- ✅ Indeed
- ✅ ZipRecruiter  
- ✅ Glassdoor
- ✅ LinkedIn (limited)

To add specific architecture firm career pages, add a custom scraper function in `scraper.py` following the same pattern as `scrape_indeed()`.
