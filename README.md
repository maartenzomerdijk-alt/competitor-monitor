# Competitor Content Monitor

Automated tool that scrapes **livefootballtickets.com** and **seatpick.com** daily, detects content changes, runs AI-powered competitive analysis via Claude, and sends Slack alerts.

## Features

- **Stealth scraper** — Playwright with randomised user-agents, viewports, and delays
- **SQLite storage** — raw HTML + clean extracted text, snapshots and diffs all persisted
- **Diff engine** — character-level change %, extracted added/removed sentences
- **AI analysis** — Claude `claude-sonnet-4-6` summarises changes and compares pages side-by-side
- **Slack alerts** — rich block-kit messages fired on significant (>5%) changes
- **JSON reports** — daily report written to `reports/YYYY-MM-DD.json`
- **Scheduler** — APScheduler runs the full pipeline every day at 08:00 UTC

---

## Project Structure

```
competitor-monitor/
├── scraper/
│   ├── crawler.py        # Playwright fetcher with stealth + retry
│   └── extractor.py      # HTML → clean text, headings, links
├── storage/
│   ├── db.py             # SQLite init, connection manager
│   └── snapshots.py      # CRUD for pages / snapshots / diffs
├── analysis/
│   ├── diff.py           # Change % + added/removed sentences
│   ├── compare.py        # Side-by-side comparison runner
│   └── ai_summary.py     # Claude API calls
├── notifications/
│   └── alerts.py         # Slack webhook + JSON report writer
├── reports/              # Daily JSON reports (auto-created)
├── config.yaml           # URL pairs and settings
├── scheduler.py          # APScheduler wrapper
├── main.py               # CLI entry point
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Clone / navigate to the project

```bash
cd competitor-monitor
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
```

### 5. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```
ANTHROPIC_API_KEY=sk-ant-...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

- **ANTHROPIC_API_KEY** — get one at https://console.anthropic.com
- **SLACK_WEBHOOK_URL** — create an Incoming Webhook at https://api.slack.com/apps
  (Add → Incoming Webhooks → Activate → Add to Slack → copy the URL)

### 6. Initialise the database

```bash
python main.py --init-db
```

This creates `competitor_monitor.db` with all tables and seeds the 14 page records (7 pairs).

---

## Usage

### Run the full pipeline immediately

```bash
python main.py --run-now
```

This will:
1. Scrape all 14 URLs (7 mine + 7 competitor)
2. Extract content from each page
3. Compare against previous snapshots and compute diffs
4. Fire Slack alerts for any pages with >5% change
5. Run AI side-by-side comparisons for all 7 slug pairs
6. Write `reports/YYYY-MM-DD.json`

### Run comparisons only (no scraping)

Useful if you want to re-run AI analysis against existing snapshots without hitting the sites again.

```bash
python main.py --compare
```

Results are printed to the console and saved to the daily JSON report.

### Start the daily scheduler

```bash
python main.py --schedule
```

Runs in the foreground. The pipeline triggers every day at **08:00 UTC**.
Use a process manager (e.g. `screen`, `tmux`, `systemd`, or `supervisord`) to keep it running.

---

## Configuration

Edit `config.yaml` to adjust:

```yaml
settings:
  change_threshold_pct: 5    # % change to count as "significant"
  scrape_delay_min: 2        # seconds between requests (min)
  scrape_delay_max: 5        # seconds between requests (max)
  schedule_hour: 8           # UTC hour for daily run
  retry_wait_seconds: 60     # wait before retrying a blocked request
  max_retries: 3             # max attempts per URL
```

---

## Output

### Slack alert (significant change)

```
🔴 Content Change Detected — Competitor Page
Page: arsenal  |  Site: Competitor
Change: 12.4%
Word count: 1,842 → 2,105 (+263)

AI Summary:
The competitor added a new "Club History" section and expanded their
fixture list with hospitality pricing. Strategic intent appears to be
targeting "Arsenal hospitality tickets" long-tail keywords.
```

### JSON report (`reports/2026-02-19.json`)

```json
{
  "generated_at": "2026-02-19T08:12:00Z",
  "date": "2026-02-19",
  "significant_changes": [...],
  "comparisons": [
    {
      "slug": "arsenal",
      "my_word_count": 1420,
      "competitor_word_count": 2105,
      "my_depth_score": 6,
      "competitor_depth_score": 8,
      "content_gaps": "Competitor covers club history, travel guide...",
      "keywords_they_cover": ["arsenal hospitality", "emirates stadium tour", ...],
      "recommendations": "1. Add stadium travel guide section..."
    }
  ]
}
```

---

## Database Schema

| Table | Key columns |
|-------|------------|
| `pages` | id, url, site (mine/competitor), page_slug |
| `snapshots` | id, page_id, scraped_at, raw_html, clean_text, word_count, h1, meta_description, headings (JSON), internal_links (JSON) |
| `diffs` | id, page_id, snapshot_old_id, snapshot_new_id, change_pct, added_text, removed_text, ai_summary, detected_at |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Blocked by site | Increase `scrape_delay_min/max` in config.yaml. The tool will retry after `retry_wait_seconds`. |
| `ANTHROPIC_API_KEY not set` | Ensure your `.env` file exists and is loaded, or export the variable in your shell. |
| Playwright browser not found | Run `playwright install chromium` |
| No diffs generated | You need at least 2 snapshots per page — run `--run-now` twice on separate days. |
| Slack not receiving messages | Confirm the webhook URL is correct and the app is still installed in your workspace. |
