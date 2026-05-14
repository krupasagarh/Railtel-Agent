# Railtel Agent

Standalone Telegram bot for **Railtel / Railwire** only (multi login, single audit, clear session). English and Kannada UI.

## Setup

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy `.env.example` to `.env` (or create `.env`) in this folder (`railtel_agent/`) with at least:

   - `TELEGRAM_BOT_TOKEN=...`
   - `RAILWIRE_USER` / `RAILWIRE_PASS` (or `RAILWIRE_ACCOUNTS_FILE` JSON — see `vk_agent/multi_credentials.py`)

3. Install dependencies and Playwright browser:

   ```bash
   cd railtel_agent
   pip install -r requirements.txt
   playwright install chromium
   ```

4. Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (same as your combined bot). On Windows the code expects the default path under `C:\Program Files\Tesseract-OCR\`.

5. Run the bot:

   ```bash
   python vk_agent/telegram_bot.py
   ```

Request logs default to `logs/bot_requests.jsonl`. Summarize with:

```bash
python vk_agent/summarize_bot_request_log.py
```

## Publishing as its own GitHub repo

From `railtel_agent/`:

```bash
git init
git add .
git commit -m "Initial Railtel Agent"
git branch -M main
git remote add origin https://github.com/YOUR_USER/Railtel-Agent.git
git push -u origin main
```

Create the empty repository **Railtel-Agent** (or `Railtel_Agent`) on GitHub first, then push.
