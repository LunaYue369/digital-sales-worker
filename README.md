# Digital Sales Worker

AI-powered B2B cold email automation platform. Covers the full outbound sales lifecycle: prospect discovery via Google Maps, email enrichment from company websites, AI-written personalized emails, Gmail sending with human-like pacing, reply tracking with sentiment analysis, and campaign reporting -- all operated through Slack.

## Features

- **Prospect Discovery** -- Scrapes Google Maps for businesses by category/location, crawls their websites to find contact emails
- **Multi-Strategy Email Finder** -- Extracts emails via mailto links, JSON-LD schema, visible text, and raw HTML regex; validates with MX record checks; maintains a learned junk filter from bounce feedback
- **AI Email Writing** -- GPT-4.1 researches each company and writes personalized cold emails; Reviewer agent gates quality with up to 3 revision rounds
- **Human-Like Sending** -- Normal distribution delays (mean 25s), random "coffee break" pauses (5% chance of 1-2 min delay), configurable rate limits
- **Reply Tracking** -- Monitors Gmail for replies; classifies as human/bounce/OOO/spam/auto-reply; GPT-5 deep analysis of human replies (sentiment, intent, objections, follow-up advice)
- **Google Drive Integration** -- Auto-detects new prospect spreadsheets uploaded to Drive and kicks off campaigns
- **Multi-User Support** -- Per-user Gmail OAuth, sending config, and campaign isolation

## Architecture

```
main.py                        # Slack bot entry point (Bolt, Socket Mode)
auto/
  auto_pipeline.py             # Main campaign loop: research -> write -> review -> send
  prospect_pipeline.py         # Google Maps scraping + email enrichment
  track_pipeline.py            # Gmail reply monitoring + AI classification
  report_pipeline.py           # Campaign analytics + AI insights
agents/
  researcher.py                # Company research (concurrent, cached)
  copywriter.py                # Personalized email writing (GPT-4.1)
  reviewer.py                  # Quality gate: up to 3 rounds
  reply_analyzer.py            # Reply sentiment analysis (GPT-5)
  reporter.py                  # Campaign insight generation
services/
  email_finder.py              # Multi-strategy email extraction + MX validation
  email_sender.py              # Gmail API with human-like pacing
  reply_tracker.py             # Reply detection + classification
  auth.py                      # Gmail OAuth flow (in-Slack authorization)
  drive_poller.py              # Google Drive new file detection
  spreadsheet.py               # Prospect data parsing
  stats.py                     # Campaign statistics
  junk_list.py                 # Learned junk email filter
  failed_log.py                # Failed email tracking for post-mortem
core/
  bot.py                       # Slack command router
  state.py                     # Per-user campaign state
  user_config.py               # User identity + email config
```

## Pipeline Flow

```
Google Maps Scrape ──> Website Crawl ──> Email Extraction + MX Validation
        |
        v
   AI Research (concurrent, cached) ──> Personalized Email Writing
        |
        v
   Review Loop (up to 3 rounds, discard on failure)
        |
        v
   Gmail Send (human-like pacing: ~25s avg, random pauses)
        |
        v
   Reply Monitoring ──> Classification ──> AI Sentiment Analysis
        |
        v
   Campaign Report + AI Insights
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Bot Framework | Slack Bolt (Socket Mode) |
| LLM | OpenAI GPT-4.1 / GPT-5 |
| Email | Gmail API (OAuth 2.0) |
| Prospect Scraping | Google Maps Scraper (Go binary) |
| Email Validation | dnspython (MX record checks) |
| Web Scraping | BeautifulSoup, requests |
| File Integration | Google Drive API |
| Concurrency | ThreadPoolExecutor (up to 5 workers) |

## Email Finder Strategy

The email finder uses a multi-layer extraction approach, ordered by reliability:

| Priority | Method | Description |
|----------|--------|-------------|
| 1 | `mailto:` links | Most reliable -- explicit contact links |
| 2 | JSON-LD schema | Structured data from page markup |
| 3 | Visible text | Email patterns in rendered page content |
| 4 | Raw HTML regex | Fallback -- scan full HTML source |
| Post | MX validation | Verify domain can receive email |
| Post | Junk filter | Exclude noreply, placeholder, and technical addresses |
