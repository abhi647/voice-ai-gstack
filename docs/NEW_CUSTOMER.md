# Onboarding a New Customer

This is the end-to-end checklist for adding a new dental practice to the platform. Takes about 30–45 minutes. Everything runs against the shared Azure deployment — no new infrastructure needed per customer.

---

## What you'll need from the practice

Before you start, collect these from the client:

| Item | Example | Notes |
|---|---|---|
| Practice name | `Riverside Dental` | Used in agent greeting and emails |
| US state | `CA` | Drives HIPAA disclosure wording (two-party consent in CA) |
| Timezone | `America/Los_Angeles` | Used for business hours check |
| Staff email | `front@riversidedental.com` | Receives booking notifications and weekly digest |
| Business hours | Mon–Fri 9AM–6PM | Or 24/7 if they want to always answer |
| Escalation phone | `+12025559999` | Where to transfer emergency/urgent calls |
| Agent name | `Aria` (default) | What the AI calls itself on the call |
| ElevenLabs voice | Rachel (default) | Voice ID from ElevenLabs — can audition voices at elevenlabs.io |

---

## Step 1 — Buy a Twilio phone number

1. Log in to [console.twilio.com](https://console.twilio.com)
2. Go to **Phone Numbers → Manage → Buy a Number**
3. Search by area code matching the practice's city
4. Buy a local number (not toll-free — patients trust local numbers more)
5. Note the number, e.g. `+12025551234`

You don't need to configure the number's webhook in Twilio yet — the `provision-practice` command does that automatically.

---

## Step 2 — Provision the practice in the database

SSH into the app or run locally with the production `DATABASE_URL`:

```bash
python -m app.cli provision-practice \
  --name "Riverside Dental" \
  --twilio-number "+12025551234" \
  --state CA \
  --timezone "America/Los_Angeles" \
  --staff-email "front@riversidedental.com" \
  --escalation-number "+12025559999"
```

This creates a `practices` row with a UUID, writes the default `PracticeConfig` as JSONB, and sets the Twilio webhook URL on the number automatically.

You'll see output like:

```
Practice created: f9a333fc-59d9-4f80-ba9f-438d6ac10c32
Twilio webhook configured: https://voice-ai-app.azurewebsites.net/twilio/voice
Done.
```

Save that UUID — you'll use it in the next steps.

---

## Step 3 — Customize the practice config

The default config (Mon–Fri 9AM–5PM, agent name Aria, Rachel voice) works for most practices. Customize what the client asked for.

Connect to the database and update the config JSON. The easiest way:

```bash
python -m app.cli update-practice <practice-uuid> \
  --agent-name "Sofia" \
  --business-hours '{"monday":["09:00","18:00"],"tuesday":["09:00","18:00"],"wednesday":["09:00","18:00"],"thursday":["09:00","18:00"],"friday":["09:00","17:00"],"saturday":null,"sunday":null}' \
  --after-hours-message "Thanks for calling Riverside Dental. We're currently closed. Our hours are Monday through Friday, 9am to 6pm. Please call back during business hours, or press 0 to leave a message for our team."
```

Or update directly via SQL if you prefer:

```sql
UPDATE practices
SET config = jsonb_set(
  config,
  '{business_hours}',
  '{"monday":["09:00","18:00"],"tuesday":["09:00","18:00"],"wednesday":["09:00","18:00"],"thursday":["09:00","18:00"],"friday":["09:00","17:00"],"saturday":null,"sunday":null}'::jsonb
)
WHERE id = 'f9a333fc-59d9-4f80-ba9f-438d6ac10c32';
```

**Full config reference:**

```json
{
  "agent_name": "Aria",
  "tts_voice_id": "21m00Tcm4TlvDq8ikWAM",
  "llm_model": "claude-sonnet-4-6",
  "ehr_adapter": "notify",
  "sms_enabled": true,
  "business_hours": {
    "monday":    ["09:00", "17:00"],
    "tuesday":   ["09:00", "17:00"],
    "wednesday": ["09:00", "17:00"],
    "thursday":  ["09:00", "17:00"],
    "friday":    ["09:00", "17:00"],
    "saturday":  null,
    "sunday":    null
  },
  "after_hours_message": "Our office is currently closed. Please call back during business hours.",
  "services": [
    "cleaning and hygiene",
    "checkup and exam",
    "filling",
    "crown",
    "extraction",
    "root canal",
    "teeth whitening",
    "Invisalign consultation",
    "dental emergency"
  ],
  "custom_instructions": ""
}
```

**Notes on each field:**

- `tts_voice_id` — find voice IDs at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library). Rachel (`21m00Tcm4TlvDq8ikWAM`) is the default — warm, professional.
- `ehr_adapter` — keep as `notify` for v0.1 (SMS + email to staff). Set to `dentrix` or `opendental` when those integrations are built.
- `after_hours_message` — set to `""` (empty string) to make the agent always answer regardless of time.
- `services` — the agent uses this list to anchor the LLM to what the practice actually offers. Remove anything they don't do (e.g. remove `"Invisalign consultation"` if they don't offer it).
- `custom_instructions` — free-text appended to the system prompt. Use for practice-specific rules: `"Always mention that we accept most major insurance plans."` or `"Do not schedule root canals on Fridays — Dr. Chen is not available."` Changes take effect on the next call, no deploy needed.

---

## Step 4 — Choose a voice

1. Go to [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library)
2. Audition voices — look for warm, clear, professional-sounding voices
3. Click a voice → copy the Voice ID from the URL or the voice settings panel
4. Update `tts_voice_id` in the config (Step 3)

If the client wants a custom voice (cloned from their own staff), ElevenLabs supports this on paid plans. The voice clone needs ~10 minutes of clean audio. Voice ID works the same way once created.

---

## Step 5 — Test the number

Call the Twilio number from your phone. You should hear:

> "Thank you for calling [Practice Name]. This call may be recorded for quality assurance purposes. My name is [Agent Name]. How can I help you today?"

Test these scenarios before handing off to the client:

- [ ] Greeting plays clearly (no noise, no distortion, correct practice name)
- [ ] Agent understands your name when you say it
- [ ] Agent responds sensibly to "I'd like to book a cleaning"
- [ ] Agent asks follow-up questions (preferred time, new or existing patient)
- [ ] Saying "emergency" or "urgent" triggers the escalation message
- [ ] Call outside business hours plays the after-hours message
- [ ] Hanging up mid-conversation doesn't cause any errors (check logs)

Check logs during testing:

```bash
az webapp log tail --name voice-ai-app --resource-group voice-ai-rg
```

Look for `PATIENT [CallSid]:` and `AGENT [CallSid]:` lines to see the transcript in real time.

---

## Step 6 — Verify transcript storage

After a test call, check that the transcript was saved:

```bash
# Check the database
psql $DATABASE_URL -c "SELECT call_sid, disposition, created_at FROM calls ORDER BY created_at DESC LIMIT 5;"

# Check Azure Blob Storage
az storage blob list \
  --container-name voiceaiblob \
  --connection-string "$AZURE_STORAGE_CONNECTION_STRING" \
  --prefix "practices/<practice-uuid>/calls/" \
  --output table
```

Each call creates a file at `practices/<practice-uuid>/calls/<call-sid>/transcript.txt`.

---

## Step 7 — Set up the weekly digest

The practice gets a Monday morning email with call metrics from the past week. This runs automatically if you have a cron job set up. Verify the staff email receives the digest correctly by triggering it manually:

```bash
bin/weekly-digest
```

Check that the email arrives at the practice's staff email address. If SendGrid shows delivery issues, double-check the `SENDGRID_FROM_EMAIL` matches a verified sender in your SendGrid account.

---

## Step 8 — Hand off to the client

Send the practice manager:

1. The phone number (already bought in Step 1)
2. A brief explainer of what happens when a patient calls
3. How to reach you if the agent says something wrong (you can update `custom_instructions` in the DB immediately — no redeploy)
4. The weekly digest schedule (every Monday morning)

---

## Customization reference

Everything that changes per-customer lives in the `config` JSONB column on the `practices` table. No code changes, no deploys.

### Change the agent's name and voice

```sql
UPDATE practices
SET config = config
  || '{"agent_name": "Sofia"}'::jsonb
  || '{"tts_voice_id": "your-elevenlabs-voice-id"}'::jsonb
WHERE id = '<practice-uuid>';
```

### Add custom instructions

```sql
UPDATE practices
SET config = jsonb_set(
  config,
  '{custom_instructions}',
  '"Always mention that we offer free consultations for new patients. Do not schedule appointments on holidays."'
)
WHERE id = '<practice-uuid>';
```

### Set to 24/7 (always answer)

```sql
UPDATE practices
SET config = config
  || '{"after_hours_message": ""}'::jsonb
  || '{"business_hours": {"monday":["00:00","23:59"],"tuesday":["00:00","23:59"],"wednesday":["00:00","23:59"],"thursday":["00:00","23:59"],"friday":["00:00","23:59"],"saturday":["00:00","23:59"],"sunday":["00:00","23:59"]}}'::jsonb
WHERE id = '<practice-uuid>';
```

### Temporarily disable (maintenance / client pause)

```sql
UPDATE practices SET is_active = false WHERE id = '<practice-uuid>';
```

Inbound calls to the number will get a "This number is not in service" message. Re-enable:

```sql
UPDATE practices SET is_active = true WHERE id = '<practice-uuid>';
```

---

## Troubleshooting

**"This number is not in service"**
Practice row is missing or `is_active = false`. Check:
```sql
SELECT id, name, is_active, twilio_number FROM practices WHERE twilio_number = '+12025551234';
```

**Audio plays but sounds like noise/distortion**
The `output_format=ulaw_8000` query parameter must be in the ElevenLabs URL, not the request body. Check `app/routers/stream.py` → `_tts_stream()`.

**Agent doesn't respond after greeting**
Deepgram connection likely failed. Check logs for `Failed to connect to Deepgram` or `ImportError`. The `deepgram-sdk` must be pinned to `<4.0.0`.

**Call connects but immediately drops**
Check Twilio error logs at [console.twilio.com/monitor/errors](https://console.twilio.com/monitor/errors). Error 31924 means a WebSocket protocol error — usually caused by calling `ws.close()` explicitly from inside the handler. Don't do that; let the function return naturally.

**After-hours message not playing**
Check the `business_hours` and `after_hours_message` in the config. If `after_hours_message` is empty string, the agent always answers. Verify the practice timezone is set correctly (`America/New_York`, `America/Chicago`, etc.).

**Transcript not saving to Blob Storage**
The `voiceaiblob` container might not exist. Create it:
```bash
az storage container create --name voiceaiblob --connection-string "$AZURE_STORAGE_CONNECTION_STRING"
```

**Weekly digest not sending**
Check SendGrid activity feed for bounces. The `SENDGRID_FROM_EMAIL` must be a verified sender. Also verify `staff_email` is set on the practice row:
```sql
SELECT name, staff_email FROM practices WHERE id = '<practice-uuid>';
```
