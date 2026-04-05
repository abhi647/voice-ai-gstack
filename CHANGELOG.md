# Changelog

All notable changes to this project are documented here.

## [0.0.2.0] - 2026-04-05

### Security

- **SEC-1**: Twilio request signature verification on `POST /twilio/voice` and
  `POST /twilio/status`. New `app/middleware/twilio_auth.py` validates
  `X-Twilio-Signature` using `twilio.request_validator.RequestValidator`. Any
  request without a valid signature returns 403. Skipped automatically when
  `TWILIO_AUTH_TOKEN` is not set (dev mode). Handles Azure's reverse proxy
  by reconstructing the public URL from `X-Forwarded-Proto` and
  `X-Forwarded-Host` headers before validation.
- **SEC-2**: `POST /internal/escalate` and `POST /internal/finalize_call` now
  require an `X-Internal-Secret` header matching `INTERNAL_SECRET` env var.
  Uses constant-time comparison (`secrets.compare_digest`) to prevent timing
  attacks. Skipped when `INTERNAL_SECRET` is unset. Stream handler now
  includes the header in all internal POSTs.
- **SEC-3**: Added `asyncio.Lock` (`self._transcript_lock`) to
  `CallHandler._on_final_transcript`. Two rapid Deepgram final transcripts
  can arrive concurrently and interleave `user→user` messages in Claude
  history, causing a 400 from the Anthropic API. The lock serialises
  transcript processing to one at a time.
- **SEC-4**: Claude message history guard in `CallHandler._respond`. Previously
  the user message was appended to `self.messages` before calling Claude —
  if Claude threw (rate limit, network error), the unanswered user message
  stayed in history and the next turn sent consecutive `user→user` messages
  (→ 400). Now Claude is called with an ephemeral copy; `self.messages` is
  only mutated after a successful response.

### Added

- `INTERNAL_SECRET` config field in `app/config.py`.
- 4 new regression tests in `tests/test_calls.py` covering SEC-1: missing
  signature → 403, invalid signature → 403, no token configured → skip
  check, valid signature → 200.
- `autouse` fixture in `test_calls.py` to bypass Twilio signature check in
  existing routing tests (the check is tested separately in
  `TestTwilioSignatureVerification`).
- Updated `test_stream.py` FakeClient to accept `headers` kwarg so
  `test_trigger_escalation_sends_escalation_number` passes.

## [0.0.1.0] - 2026-04-02

### Added
- Twilio Media Streams voice pipeline (`app/routers/stream.py`): full per-call
  STT→LLM→TTS pipeline over WebSocket. Deepgram nova-2 streaming STT, Claude
  claude-sonnet-4-6 conversation management with state machine, ElevenLabs TTS
  with correct μ-law 8kHz output format. Barge-in via task cancellation.
- New customer onboarding guide (`docs/NEW_CUSTOMER.md`): 8-step developer
  checklist from buying a Twilio number through client handoff, including full
  `PracticeConfig` reference, voice selection guide, test scenarios, and
  troubleshooting section.
- Mermaid architecture diagrams in README: system overview, call flow sequence,
  and conversation state machine.
- Test suite for Twilio Media Streams paths: `test_calls.py` updated for
  `<Connect><Stream>` TwiML, `test_stream.py` added with regression tests for
  ElevenLabs `output_format` query param, greeting content, and after-hours
  path.

### Changed
- Replaced LiveKit SIP integration with Twilio Media Streams. `calls.py` now
  returns `<Connect><Stream>` TwiML instead of `<Dial><Sip>`. No separate
  worker process needed — the voice pipeline runs inline per WebSocket
  connection.
- Dependencies: replaced `livekit-agents` and plugins with `anthropic`,
  `deepgram-sdk<4.0.0` (v6 removed `LiveOptions` from top-level package),
  and `elevenlabs`.
- `app/main.py`: registers stream router and configures `logging.basicConfig`
  so app-level log lines surface alongside Uvicorn in Azure log stream.

### Fixed
- ElevenLabs audio format: `output_format=ulaw_8000` must be a URL query
  parameter, not a request body field. Body placement is silently ignored —
  ElevenLabs returns MP3 by default, which sounds like pure noise when streamed
  as μ-law audio to Twilio.
- WebSocket CLOSE frame fragmentation (Twilio error 31924): removed all
  explicit `ws.close()` calls from inside WebSocket handlers. Azure App
  Service's reverse proxy fragments CLOSE control frames; Twilio rejects
  fragmented frames. Now just `break` or `return` and let the endpoint exit
  naturally.
- Deepgram SDK pinned to `<4.0.0`: v6 removed `LiveOptions` from the
  top-level `deepgram` package.
