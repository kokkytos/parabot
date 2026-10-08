# Paragliding Community Telegram Bot

A Telegram bot for a paragliding community that handles:

- **Private Check-In/Check-Out codes** for participants, looked up in Airtable and delivered via a personalized link + QR code.
- **New member onboarding** in group chats, with a temporary welcome message and a deep link to a private welcome DM.
- **Weather reporting** for a flying site (Paramythia, Greece) and two personal Weather Underground stations, including an estimated cloudbase, with both on-demand commands and a daily scheduled post.
- **Live group location map** — members share their live Telegram location and see everyone on an interactive Leaflet map served as a Telegram Mini App, complete with profile picture avatars and altitude from the device GPS.

Built with [python-telegram-bot](https://docs.python-telegram-bot.org/), [aiohttp](https://docs.aiohttp.org/), [httpx](https://www.python-httpx.org/), and the [Airtable](https://airtable.com/), [OpenWeather](https://openweathermap.org/api), and [Weather Underground](https://www.wunderground.com/) APIs.

Deployed on **Google Cloud Run** (webhook mode). The daily weather report is triggered by a **GitHub Actions scheduled workflow** rather than an in-process scheduler.

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
- `/weather_all` — Runs all three reports and posts them together in a single combined message. This is also posted **automatically every day at 12:00 (Europe/Athens time)** via the GitHub Actions scheduled workflow.
- Every report includes an estimated **cloudbase (Espy's equation)** in meters ASL, calculated from temperature and dew point.
- **Where replies go:**
  - Run in a **private chat** with the bot → the reply is sent there.
  - Run in a **group/topic** → the command message is deleted and the reply is posted to a single, pre-configured "weather log" topic, keeping the rest of the group tidy.

### Live location map
- `/map` — Opens an interactive live group map as a **Telegram Mini App** (Web App). Members share their **live location** (not a static pin) via the Telegram attachment menu (**Attachment → Location → Share Live Location**, choose a duration); the map updates every 3 seconds showing real-time movement.
- Each member appears as a **circular avatar marker** showing their Telegram profile picture. Tapping a marker shows their name, username, altitude, and a link to follow them in Google Maps.
- **Altitude** is captured from the device's GPS via the browser Geolocation API and merged with the Telegram live-location data, so it works even when Telegram's native location share doesn't expose altitude.
- Stale entries (no update for more than 3 minutes) are automatically removed from the map.
- The map is served by the same aiohttp server as the bot, at `GET /`. Profile pictures are cached locally in `avatars/` and served at `GET /avatars/{filename}`.
- **Important:** Only "Share Live Location" works — "Send Current Location" (static pin) sends no updates and won't appear on the map.

### `/help`
Shows an in-chat summary of all commands and behavior.

---

## Architecture

```
GitHub (main branch push)
  └─▶ deploy.yml workflow
        ├─ Builds Docker image
        ├─ Pushes to Artifact Registry
        └─ Deploys to Cloud Run (two-pass: deploy then patch WEBHOOK_URL)

Telegram
  └─▶ HTTPS POST /webhook/<token>
        └─ bot.py (aiohttp + python-telegram-bot)

  └─▶ Mini App (GET /)
        └─ bot.py serves Leaflet map page
              ├─ GET  /api/locations       → active live locations (JSON)
              ├─ POST /api/update_location → altitude push from Mini App
              └─ GET  /avatars/{filename}  → cached profile pictures

GitHub Actions cron (09:00 UTC = 12:00 Athens)
  └─▶ daily_weather.yml workflow
        └─ POST /trigger_daily_weather  (X-Trigger-Token header)
              └─ bot.py posts combined weather to the group topic
```

The bot runs in **webhook mode**: Telegram pushes updates to the Cloud Run service over HTTPS. Cloud Run scales to zero when there is no traffic — `--min-instances=1` keeps one instance warm so the first message isn't delayed by a cold start.

### First-deploy WEBHOOK_URL handling

Cloud Run's service URL is not known until after the first deploy. The workflow handles this with a two-pass approach:

1. The deploy step uses the pre-existing service URL (empty on the very first deploy). The bot tolerates an empty `WEBHOOK_URL` at startup and simply skips webhook registration.
2. Immediately after deploy, a `Fix WEBHOOK_URL and migrate traffic` step patches the real URL into the service's environment and migrates 100% traffic to the new revision. The bot restarts with the correct URL and registers the webhook.

On all subsequent deploys the URL is known in advance, so this is a transparent no-op.

---

## Repository structure

```
.
├── bot.py                          # Application source
├── Dockerfile                      # Two-stage image for Cloud Run
├── requirements.txt                # Pinned dependencies
├── .env.example                    # Template — copy to .env for local dev
├── avatars/                        # Runtime cache for user profile pictures (git-ignored)
├── .github/
│   └── workflows/
│       ├── deploy.yml              # Build & deploy on push to main
│       └── daily_weather.yml       # Scheduled daily weather trigger
└── README.md
```

---

## Local development

### Prerequisites

- Python 3.10+
- A public HTTPS URL that Telegram can reach (e.g. via [ngrok](https://ngrok.com/))

### Setup

```bash
git clone <this-repo-url>
cd Parabot
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Start an ngrok tunnel (in a separate terminal):

```bash
ngrok http 8080
```

Set both `WEBHOOK_URL` and `MINI_APP_URL` in `.env` to the ngrok HTTPS URL (e.g. `https://abc123.ngrok.io`), then run the bot:

```bash
python bot.py
```

The live map will be accessible at `https://abc123.ngrok.io/` and the `/map` command will open it as a Mini App inside Telegram.

To test the daily trigger locally:

```bash
curl -X POST http://localhost:8080/trigger_daily_weather \
  -H "X-Trigger-Token: <your DAILY_TRIGGER_TOKEN>"
```

---

## Configuration

All configuration is via environment variables. In Cloud Run these are injected from Secret Manager (see deployment below). For local dev, use a `.env` file.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `AIRTABLE_TOKEN` | ✅ | — | Airtable personal access token |
| `AIRTABLE_BASE_ID` | ✅ | — | Airtable base ID (`app…`) |
| `WEBHOOK_URL` | ✅ | — | Public HTTPS base URL (no trailing slash). In Cloud Run this is set automatically by the deploy workflow. For local dev use an ngrok URL. |
| `MINI_APP_URL` | map | `""` | Public HTTPS URL Telegram opens as the Web App when a user taps `/map`. Typically the same as `WEBHOOK_URL` since the map is served at `GET /`. |
| `STADIA_API_KEY` | map | `""` | Stadia Maps API key for Stamen Terrain tiles in the live map. Get a free key at [stadiamaps.com](https://client.stadiamaps.com/signup/). If not set, the map falls back to OpenStreetMap only. |
| `WEBHOOK_SECRET` | recommended | `""` | Secret token for Telegram webhook verification. Generate with `openssl rand -hex 32`. |
| `DAILY_TRIGGER_TOKEN` | recommended | `""` | Token for the `/trigger_daily_weather` endpoint. Generate with `openssl rand -hex 32`. Must match the `DAILY_TRIGGER_TOKEN` GitHub Actions secret. |
| `OPENWEATHER_API_KEY` | weather | — | For `/weather_para` |
| `WU_API_KEY` | weather | — | For `/weather_gri`, `/weather_sev`, `/weather_all` |
| `WU_STATION_ID` | — | `IGRIKA1` | Grika WU station ID |
| `WU_SEVASTO_STATION_ID` | — | `ISEVAS24` | Sevasto WU station ID |
| `PORT` | — | `8080` | Port the server listens on (Cloud Run sets this automatically) |

Constants that are not environment variables (weather log chat/topic IDs, site coordinates and elevations, the daily schedule time, stale-location timeout) are defined near the top of `bot.py` and can be edited there.

### Airtable setup

The bot expects a table (default name: `Participants`) with at least these fields:

| Field | Type | Purpose |
|---|---|---|
| `Telegram_ID` | Text/Number | Telegram numeric user ID (fallback lookup key) |
| `Telegram_Username` | Text | Telegram `@username` (primary lookup key, case-insensitive, `@` optional) |
| `Custom_CheckInOut_URL` | URL | Personalized smart link routing to the correct Check-In or Check-Out form |

### Mini App setup

For the `/map` command to open the map inside Telegram, the URL set in `MINI_APP_URL` must be registered as a **Web App** with [@BotFather](https://t.me/BotFather):

1. Open @BotFather → `/mybots` → select your bot → **Bot Settings** → **Menu Button** (or **Configure Mini App**).
2. Set the URL to your `MINI_APP_URL` value.

Alternatively the map opens fine from the inline button produced by `/map` without registering a menu button — registration just adds a persistent shortcut icon in the chat input bar.

---

## Deployment (Google Cloud Run)

### One-time GCP setup

Replace the placeholders with your own values.

```bash
PROJECT_ID=your-project-id
REGION=europe-west1
SA_NAME=parabot-deployer
REPO_NAME=parabot
SERVICE_NAME=parabot
```

**1. Enable APIs**

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  --project "$PROJECT_ID"
```

**2. Create an Artifact Registry repository**

```bash
gcloud artifacts repositories create "$REPO_NAME" \
  --repository-format=docker \
  --location="$REGION" \
  --project "$PROJECT_ID"
```

**3. Create a dedicated service account for GitHub Actions**

```bash
gcloud iam service-accounts create "$SA_NAME" \
  --display-name "Parabot GitHub Actions deployer" \
  --project "$PROJECT_ID"

for ROLE in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/iam.serviceAccountUser \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role "$ROLE"
done
```

**4. Set up Workload Identity Federation** (no long-lived keys)

```bash
POOL_NAME=github-actions-pool
PROVIDER_NAME=github-provider
GITHUB_REPO=your-github-username/parabot   # lowercase, e.g. jdoe/parabot

gcloud iam workload-identity-pools create "$POOL_NAME" \
  --location=global \
  --display-name="GitHub Actions Pool" \
  --project "$PROJECT_ID"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
  --workload-identity-pool="$POOL_NAME" \
  --location=global \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="attribute.repository=='${GITHUB_REPO}'" \
  --project "$PROJECT_ID"

POOL_ID=$(gcloud iam workload-identity-pools describe "$POOL_NAME" \
  --location=global --project "$PROJECT_ID" \
  --format="value(name)")

gcloud iam service-accounts add-iam-policy-binding \
  "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${GITHUB_REPO}" \
  --project "$PROJECT_ID"
```

> **Note:** The `GITHUB_REPO` value must be **lowercase** and match exactly what GitHub sends in the OIDC token (e.g. `jdoe/parabot`, not `jdoe/Parabot`). Verify with:
> ```bash
> gcloud iam service-accounts get-iam-policy \
>   "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
>   --project "$PROJECT_ID"
> ```

**5. Store secrets in Secret Manager**

```bash
for SECRET in \
  TELEGRAM_BOT_TOKEN \
  AIRTABLE_TOKEN \
  AIRTABLE_BASE_ID \
  OPENWEATHER_API_KEY \
  WU_API_KEY \
  WEBHOOK_SECRET \
  DAILY_TRIGGER_TOKEN \
  STADIA_API_KEY; do
  gcloud secrets create "$SECRET" --replication-policy=automatic --project "$PROJECT_ID"
  echo -n "your-secret-value" | \
    gcloud secrets versions add "$SECRET" --data-file=- --project "$PROJECT_ID"
done
```

**6. Grant the Cloud Run service account access to the secrets**

```bash
COMPUTE_SA=$(gcloud iam service-accounts list \
  --filter="displayName:Compute Engine default service account" \
  --format="value(email)" \
  --project "$PROJECT_ID")

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${COMPUTE_SA}" \
  --role roles/secretmanager.secretAccessor
```

### GitHub repository setup

Add these **secrets** (`Settings → Secrets and variables → Actions → Secrets`):

| Secret | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Output of: `gcloud iam workload-identity-pools providers describe "$PROVIDER_NAME" --workload-identity-pool="$POOL_NAME" --location=global --project "$PROJECT_ID" --format="value(name)"` |
| `GCP_SERVICE_ACCOUNT` | `parabot-deployer@<PROJECT_ID>.iam.gserviceaccount.com` |
| `DAILY_TRIGGER_TOKEN` | Same value you stored in Secret Manager |
| `CLOUD_RUN_URL` | Cloud Run service URL — set this after the first deploy (e.g. `https://parabot-abc123-ew.a.run.app`) |

Add these **variables** (`Settings → Secrets and variables → Actions → Variables`):

| Variable | Value |
|---|---|
| `GCP_REGION` | e.g. `europe-west1` |

> **Note:** `GCP_PROJECT_ID` is hardcoded in `deploy.yml`. Update it there if you move to a different project.

### First deploy

Push to `main`. The `deploy.yml` workflow:

1. Builds the Docker image and pushes it to Artifact Registry.
2. Deploys to Cloud Run with `--no-traffic` (the service URL isn't known yet so `WEBHOOK_URL` is empty; the bot starts but skips webhook registration).
3. Patches `WEBHOOK_URL` (and `MINI_APP_URL`) with the real service URL and migrates 100% traffic to the new revision. The bot restarts and registers the webhook with Telegram.

After the first successful deploy:

1. Copy the Cloud Run service URL from the workflow output.
2. Add it as the `CLOUD_RUN_URL` Actions secret (used by the daily weather workflow).

### Subsequent deploys

Push to `main`. The workflow runs automatically. `WEBHOOK_URL` is read from the existing service before deploy, so the two-pass mechanism is a transparent no-op.

---

## HTTP endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | None | Liveness / readiness probe (returns `200 ok`) |
| `GET` | `/` | None | Live map Mini App HTML page |
| `GET` | `/api/locations` | None | Active live locations as JSON (polled by the map every 3 s) |
| `POST` | `/api/update_location` | None | Altitude + position push from the Mini App browser |
| `GET` | `/avatars/{filename}` | None | Cached Telegram profile pictures |
| `POST` | `/webhook/<token>` | Telegram secret token header | Receives Telegram updates |
| `POST` | `/trigger_daily_weather` | `X-Trigger-Token` header | Fires the daily weather report |

---

## Commands reference

| Command | Description |
|---|---|
| `/start` | Opens a private chat and shows the **My Code** button |
| `/codes` | Sends your personal Check-In/Check-Out link + QR code (always via DM) |
| `/map` | Opens the interactive live group location map as a Telegram Mini App |
| `/help` | Shows command help |
| `/weather_para` | Current weather for Paramythia, Greece |
| `/weather_gri` | Current + daily-max weather for the Grika station |
| `/weather_sev` | Current + daily-max weather for the Sevasto station |
| `/weather_all` | Combined report from all three sources |

---

## Notes

- The bot never posts anything back into a group in response to `/codes` — it always attempts to DM the user, and silently logs if it can't (e.g. the user has never started a private chat with the bot).
- Weather sources are fetched independently in `/weather_all` and the daily trigger, so a failure in one source doesn't prevent the others from being reported — it's shown as "Unavailable" instead.
- Cloudbase estimates use Espy's equation (`h = 125 × (T − Td)`, meters AGL) converted to meters ASL using each location's configured ground elevation. This is a rough estimate, not a substitute for official soaring/weather briefings.
- The live map stores locations in memory only — they are lost on a server restart. This is intentional; live location sharing is a real-time feature and stale data from a previous session would be misleading.
- The `/api/locations` and `/api/update_location` endpoints have no authentication. They are only suitable for a private community bot where the Mini App URL is not publicly advertised. If you need to restrict access, add a shared secret check in `handle_api_update_location`.
- GitHub Actions cron schedules run at UTC. The workflow uses `0 9 * * *` (09:00 UTC = 12:00 Athens summer time, UTC+3). Adjust the hour for winter (UTC+2 → use `0 10 * * *`) or add a second cron entry to cover both offsets.

## License

Add a license of your choice (e.g. MIT) before publishing.
