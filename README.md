# Syncloud Bot

Telegram bot for downloading tracks from SoundCloud and syncing liked tracks.

## Features

- Direct download from SoundCloud links.
- Auto-sync of liked tracks by schedule.
- MP3 metadata processing (title, artist, cover).
- Upload via Pyrogram (bypass Bot API 50 MB limit).
- User error log in Telegram menu.

## Tech Stack

- Python 3.11+
- python-telegram-bot
- pyrogram + tgcrypto
- mutagen
- yt-dlp
- scdl
- ffmpeg

## Project Structure

- bot.py - application entry point.
- config.py - environment-driven settings loader.
- db.py - SQLite data layer.
- handlers_menu.py - bot menu and settings.
- handlers_sync.py - sync logic and scheduler task.
- handlers_direct_download.py - direct track processing.
- pyrogram_sender.py - MTProto audio upload helper.
- ui_texts.py - text constants.
- utils.py - utility helpers.

## Quick Start

1. Clone repository.
2. Create virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Install required system tools:

```bash
# Ubuntu/Debian example
sudo apt update
sudo apt install -y ffmpeg
pip install yt-dlp scdl
```

4. Configure environment:

```bash
cp .env.example .env
# then edit .env
```

Required variables:

- TELEGRAM_BOT_TOKEN
- API_ID
- API_HASH

Optional variables:

- DOWNLOAD_FOLDER (default: downloads)
- BOT_VERSION (default: 1.1.0)

5. Run the bot:

```bash
python bot.py
```

## Notes

- This project stores runtime data in local SQLite (soundcloud_bot.db).
- Pyrogram may create local session files; they are ignored by .gitignore.
- Never commit real tokens to GitHub.

## Security Checklist Before Push

- Ensure .env is not tracked.
- Rotate any previously exposed tokens.
- Confirm no database/session/log files are staged.
