# Skyjet Leaderboard - Discord Forum + Monthly Competition Bot

This bot reads your `contract-board` forum posts and posts a monthly leaderboard in a `leaderboard` channel on the first day of each month.

## Features

- Slash command: `/archived_posts`
- Fetches archived forum posts page-by-page (`page_size` configurable, max 100)
- Exports full results as a JSON attachment
- Slash command: `/post_monthly_leaderboard` (manual trigger)
- Automatic monthly leaderboard posting (first day of month)
- Creates `leaderboard` channel automatically if missing (when bot has `Manage Channels` permission)
- Counts contracts claimed via forum post tags (tag name = member name)

## Requirements

- Python 3.10+
- A Discord bot token
- Bot added to your server with permissions to view forum channels and threads

## Setup

Install dependencies:

```powershell
pip install -r requirements.txt
```

Set environment variables in PowerShell:

```powershell
$env:DISCORD_BOT_TOKEN="YOUR_BOT_TOKEN"
$env:DISCORD_FORUM_CHANNEL_ID="123456789012345678"
$env:DISCORD_GUILD_ID="123456789012345678"
# Optional: override forum/channel names if IDs are not provided
$env:CONTRACT_FORUM_CHANNEL_NAME="contract-board"
$env:LEADERBOARD_CHANNEL_NAME="leaderboard"
# Optional: post in a specific leaderboard channel by ID
$env:LEADERBOARD_CHANNEL_ID="123456789012345678"
# Optional: timezone for "first day of month" check
$env:LEADERBOARD_TIMEZONE="UTC"
# Optional: ignore non-claim tags (comma separated, case-insensitive)
$env:LEADERBOARD_IGNORE_TAGS="open,urgent,unclaimed"
```

Run the bot:

```powershell
python main.py
```

## Command Usage

- `/archived_posts`
  - Uses `DISCORD_FORUM_CHANNEL_ID`
- `/archived_posts forum_channel_id:<id> page_size:<1-100>`
  - Overrides default channel and page size

The bot replies with:

- A summary count of archived posts
- A preview list of first posts
- A JSON file with all archived posts

### Monthly Leaderboard

- Automatic: bot checks every 30 minutes and posts once on day `1` of each month.
- Manual: `/post_monthly_leaderboard`
- Optional manual params:
  - `forum_channel_id:<id>`
  - `leaderboard_channel_id:<id>`
  - `page_size:<1-100>`

The leaderboard covers the **previous calendar month**, includes medal ranking, total contracts, and unclaimed count.
