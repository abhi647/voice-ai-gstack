# Voice AI Receptionist

An AI phone receptionist for dental practices. When a patient calls, Aria answers, understands what they need, and either captures a booking or transfers to staff. No hold music. No voicemail. Always on.

Built on Twilio Media Streams + Deepgram STT + Claude + ElevenLabs TTS, deployed on Azure.

---

## How it works

```
Patient calls Twilio number
        ↓
POST /twilio/voice — practice lookup, return <Connect><Stream>
        ↓
WebSocket /twilio/stream — bidirectional μ-law 8kHz audio
        ↓
Deepgram streaming STT → Claude (claude-sonnet-4-6) → ElevenLabs TTS
        ↓
Audio chunks stream back to Twilio in real time
```

Each call runs entirely in-process — no separate worker, no message queue. The WebSocket handler in `app/routers/stream.py` owns the full STT→LLM→TTS pipeline for its call duration.

Barge-in works: if the patient starts talking while Aria is speaking, the current TTS task is cancelled and Twilio's buffer is cleared.

---

## Stack

| Layer | Technology |
|---|---|
| Phone | Twilio (inbound calls, Media Streams WebSocket) |
| STT | Deepgram streaming (`nova-2`, `mulaw` 8kHz) |
| LLM | Anthropic Claude (`claude-sonnet-4-6`, max 256 tokens/turn) |
| TTS | ElevenLabs (`eleven_turbo_v2_5`, `ulaw_8000` output) |
| API | FastAPI + Uvicorn (2 workers) |
| Database | PostgreSQL + SQLAlchemy async + asyncpg |
| Storage | Azure Blob Storage (call transcripts) |
| Deploy | Azure Container Registry + Azure App Service |
| Notifications | SendGrid (booking confirmations, weekly digest) |

---

## Project layout

```
app/
  routers/
    calls.py        — POST /twilio/voice: practice lookup, return TwiML
    stream.py       — WebSocket /twilio/stream: STT→LLM→TTS pipeline
    internal.py     — POST /internal/finalize_call: save transcript, send emails
  agent/
    prompts.py      — system prompt builder (per-practice, per-state)
    state.py        — conversation state machine + escalation keywords
    disclosures.py  — HIPAA recording disclosure by state
  models/
    practice.py     — Practice DB model
    practice_config.py — per-practice config (hours, voice, EHR adapter)
  ehr/
    base.py         — EHRAdapter protocol
    notify.py       — NotifyAdapter: SMS + email to staff (v0.1 default)
    factory.py      — picks adapter based on config
  storage/
    s3.py           — Azure Blob Storage upload (named s3.py for historical reasons)
  digest.py         — weekly metrics email
  cli.py            — `python -m app.cli` commands for provisioning
bin/
  deploy            — Docker Compose deploy script
  weekly-digest     — cron entrypoint
migrations/         — Alembic migrations
tests/              — pytest suite
```

---

## Local development

**Prerequisites:** Docker, Python 3.11+, a Twilio account with a phone number, API keys for Deepgram/Anthropic/ElevenLabs.

```bash
cp .env.example .env
# fill in .env with your API keys

docker compose up
```

This starts PostgreSQL, runs migrations, and starts the API on port 8000.

To expose your local server to Twilio, use ngrok:

```bash
ngrok http 8000
# Set your Twilio number's webhook to: https://<ngrok-id>.ngrok.io/twilio/voice
```

**Run tests:**

```bash
docker compose run --rm api python -m pytest
# or locally:
pip install -e ".[dev]"
pytest
```

---

## Provisioning a practice

Each practice gets its own Twilio number. After buying the number in Twilio, provision it:

```bash
python -m app.cli provision-practice \
  --name "Riverside Dental" \
  --twilio-number "+12025551234" \
  --state CA \
  --timezone "America/Los_Angeles" \
  --staff-email "front@riversidedental.com"
```

This creates a practice row and its default config (agent name Aria, Mon–Fri 9AM–5PM, EHR adapter set to `notify`).

**Per-practice config** (stored as JSONB on the `practices` table, no redeploy needed):

| Field | Default | Notes |
|---|---|---|
| `agent_name` | `"Aria"` | What the agent calls itself |
| `business_hours` | Mon–Fri 9–17 | Dict of day → `[open, close]` in HH:MM; set to 24/7 or null per day |
| `after_hours_message` | Closed message | Empty string = always answer |
| `tts_voice_id` | Rachel (ElevenLabs) | Any ElevenLabs voice ID |
| `llm_model` | `claude-sonnet-4-6` | Any Anthropic model |
| `ehr_adapter` | `"notify"` | `notify` / `dentrix` / `opendental` |
| `sms_enabled` | `true` | Adds SMS opt-in to HIPAA disclosure |

---

## Deploy to Azure

The production setup uses Azure Container Registry + Azure App Service (container).

**Build and push:**

```bash
az acr build \
  --registry voiceairegistry \
  --image voice-ai:latest \
  --resource-group voice-ai-rg .
```

**Force App Service to pick up the new image:**

```bash
az webapp config container set \
  --name voice-ai-app \
  --resource-group voice-ai-rg \
  --container-image-name "voiceairegistry.azurecr.io/voice-ai@sha256:<digest>"

az webapp restart --name voice-ai-app --resource-group voice-ai-rg
```

**Required App Service environment variables** (set via Azure portal or `az webapp config appsettings set`):

```
DATABASE_URL
SECRET_KEY
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_SMS_FROM
ANTHROPIC_API_KEY
DEEPGRAM_API_KEY
ELEVENLABS_API_KEY
AZURE_STORAGE_CONNECTION_STRING / AZURE_STORAGE_CONTAINER
SENDGRID_API_KEY / SENDGRID_FROM_EMAIL
```

**Blob storage container** (create once):

```bash
az storage container create \
  --name voiceaiblob \
  --connection-string "<AZURE_STORAGE_CONNECTION_STRING>"
```

---

## Call flow in detail

1. Patient dials the practice's Twilio number.
2. Twilio POSTs to `/twilio/voice`. We look up the practice by `To` number. If not found, we hang up gracefully.
3. We return `<Connect><Stream>` TwiML. Twilio opens a WebSocket to `/twilio/stream`, sending `practice_id` and `patient_phone` as custom parameters.
4. `CallHandler.start()` runs:
   - If outside business hours and `after_hours_message` is set: play the message and end.
   - Otherwise: connect Deepgram, play the greeting (runs as a background task so audio starts immediately).
5. As audio chunks arrive, they're forwarded to Deepgram over its streaming connection.
6. Deepgram fires transcript callbacks. Interim results trigger barge-in (cancel TTS + clear Twilio buffer). Final results trigger a Claude call.
7. Claude returns a short reply (max 256 tokens). ElevenLabs streams back μ-law audio chunks which are base64-encoded and sent to Twilio as `media` events.
8. On `stop` event or disconnect, `CallHandler.stop()` posts the transcript to `/internal/finalize_call`, which saves to the DB and uploads to Azure Blob Storage.

---

## Escalation

Two triggers:
- **Keyword**: any of `emergency`, `chest pain`, `bleeding`, etc. → immediate transfer
- **Timeout**: 4 minutes without reaching COMPLETE state → transfer

On escalation, Aria says "Let me connect you with our team right now" and the call POSTs to `/internal/escalate` (currently sends an email/SMS via SendGrid).

---

## Weekly digest

Every Monday, staff get an email with the previous week's call metrics. Run manually:

```bash
bin/weekly-digest
```

Or set up a cron job pointing at that script.

---

## HIPAA notes

This is a v0.1 pilot. Before handling real patient calls:

- Sign BAAs with Anthropic, ElevenLabs, Deepgram, Twilio, and Azure.
- Sign a BAA with each practice client.
- Have a lawyer review the recording disclosure wording (especially CA two-party consent).
- Ensure Azure storage is in a HIPAA-eligible region with encryption at rest.
- Rotate all credentials out of `.env` into Azure Key Vault or equivalent.
- Never commit `.env` with real values.

---

## Architecture decisions

**Why Twilio Media Streams instead of LiveKit SIP?**
LiveKit SIP requires a Pro plan. Free/starter tier returns "No trunk found" for all inbound calls regardless of configuration. Twilio Media Streams gives us bidirectional audio over WebSocket with no plan restrictions, and the μ-law 8kHz output from ElevenLabs matches Twilio's format exactly — no audio conversion needed.

**Why inline pipeline instead of a separate worker?**
Simplifies deployment (one process, no queue), and the latency budget for a phone call is generous enough that a single FastAPI process with async I/O handles the STT→LLM→TTS chain fine.

**Why `deepgram-sdk<4.0.0`?**
SDK 6.x removed `LiveOptions` from the top-level package. We pin to 3.x where the API we use is stable. Upgrade when we have time to rewrite the Deepgram integration to the v6 API.
