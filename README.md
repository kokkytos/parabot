# Paragliding Community Telegram Bot

A Telegram bot for a paragliding community that handles:

- **Private Check-In/Check-Out codes** for participants, looked up in Airtable and delivered via a personalized link + QR code.
- **New member onboarding** in group chats, with a temporary welcome message and a deep link to a private welcome DM.
- **Weather reporting** for a flying site (Paramythia, Greece) and two personal Weather Underground stations, including an estimated cloudbase, with both on-demand commands and a daily scheduled post.

Built with [python-telegram-bot](https://docs.python-telegram-bot.org/), [httpx](https://www.python-httpx.org/), and the [Airtable](https://airtable.com/) and [OpenWeather](https://openweathermap.org/api) / [Weather Underground](https://www.wunderground.com/) APIs.

---

## Features

### Participant codes
- `/start` — Opens a private chat with the bot and shows a **My Code** button. Also handles deep links used to clean up temporary group welcome messages.
- `/codes` — Sends the user's personal Check-In/Check-Out link and a matching QR code. Always delivered via **private message**, even when the command is run inside a group.
- Participants are looked up in Airtable, first by Telegram username, then by Telegram user ID.

### New member welcome
- When a new (non-bot) member joins a group, the bot posts a temporary welcome message with a **Get my private welcome** button that deep-links into a private `/start` chat.
- The temporary group message auto-deletes after 5 minutes (`TEMP_MESSAGE_SECONDS`), and is also cleaned up early once the user opens the private welcome message.

### Weather
- `/weather_para` — Current conditions for Paramythia, Greece, from OpenWeather.
- `/weather_gri` — Current conditions from the "Grika" Weather Underground personal weather station, plus today's daily max wind/gust and a link to the live station dashboard.
- `/weather_sev` — Same as above, for the "Sevasto" station.
- `/weather_all` — Runs all three reports and posts them together in a single combined message. This is also posted **automatically every day at 12:00 (Europe/Athens time)**.
- Every report includes an estimated **cloudbase (Espy's equation)** in meters ASL, calculated from temperature and dew point.
- **Where replies go:**
  - Run in a **private chat** with the bot → the reply is sent there.
  - Run in a **group/topic** → the command message is deleted and the reply is posted to a single, pre-configured "weather log" topic, keeping the rest of the group tidy.

### `/help`
Shows an in-chat summary of all commands and behavior.

---

## Requirements

- Python 3.10+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An Airtable base with a participants table
- (Optional) An OpenWeather API key, for `/weather_para`
- (Optional) A Weather Underground API key, for `/weather_gri`, `/weather_sev`, and `/weather_all`
- For the daily scheduled weather post, the job-queue extra for `python-telegram-bot`

---

## Installation

```bash
git clone <this-repo-url>
cd <this-repo>
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install "python-telegram-bot[job-queue]" httpx python-dotenv
```

---

## Configuration

Create a `.env` file in the project root (never commit this file):

```env
# Required
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
AIRTABLE_TOKEN=your_airtable_personal_access_token
AIRTABLE_BASE_ID=your_airtable_base_id

# Optional — needed for the corresponding weather commands
OPENWEATHER_API_KEY=your_openweather_api_key
WU_API_KEY=your_weather_underground_api_key

# Optional — override default Weather Underground station IDs
WU_STATION_ID=IGRIKA1
WU_SEVASTO_STATION_ID=ISEVAS24
```

### Airtable setup

The bot expects a table (default name: `Participants`) with at least these fields:

| Field | Type | Purpose |
|---|---|---|
| `Telegram_ID` | Text/Number | Telegram numeric user ID (fallback lookup key) |
| `Telegram_Username` | Text | Telegram `@username` (primary lookup key, case-insensitive, `@` optional) |
| `Custom_CheckInOut_URL` | URL | Personalized smart link that routes the participant to the right Check-In or Check-Out form |

Field names, the table name, the Airtable base ID, and a few other constants (weather log chat/topic IDs, site coordinates and elevations, the daily schedule time) are defined near the top of the main script and can be adjusted there.

### Weather log destination

Group/topic weather commands post to a single hardcoded chat + topic (`WEATHER_LOG_CHAT_ID` / `WEATHER_LOG_THREAD_ID`) so the community group stays uncluttered. Update these constants to point at your own group and topic.

---

## Running

```bash
python bot.py
```

The bot runs with long polling (`application.run_polling()`). On startup, if the job-queue extra is installed, it schedules the daily `/weather_all` post; otherwise it logs a warning and skips scheduling (the bot still runs, just without the daily post).

---

## Commands Reference

| Command | Description |
|---|---|
| `/start` | Opens a private chat and shows the **My Code** button |
| `/codes` | Sends your personal Check-In/Check-Out link + QR code (always via DM) |
| `/help` | Shows command help |
| `/weather_para` | Current weather for Paramythia, Greece |
| `/weather_gri` | Current + daily-max weather for the Grika station |
| `/weather_sev` | Current + daily-max weather for the Sevasto station |
| `/weather_all` | Combined report from all three sources |

---

## Notes

- The bot never posts anything back into a group in response to `/codes` — it always attempts to DM the user, and silently logs if it can't (e.g. the user has never started a private chat with the bot).
- Weather sources are fetched independently in `/weather_all` and the daily job, so a failure in one source doesn't prevent the others from being reported — it's shown as "Unavailable" instead.
- Cloudbase estimates use Espy's equation (`h = 125 × (T − Td)`, meters AGL) with a dew point derived via the Magnus-Tetens approximation where needed, converted to meters ASL using each location's configured ground elevation. This is a rough estimate, not a substitute for official soaring/weather briefings.

## License

Add a license of your choice (e.g. MIT) before publishing.