from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/voice_ai"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    anthropic_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_sip_host: str = ""       # e.g. abc123.sip.livekit.cloud — from livekit_setup.py
    livekit_sip_trunk_id: str = ""   # set after running livekit_setup.py
    livekit_sip_username: str = ""   # SIP auth username for inbound trunk
    livekit_sip_password: str = ""   # SIP auth password for inbound trunk
    azure_storage_connection_string: str = ""  # from Azure portal → Storage account → Access keys
    azure_storage_container: str = "voice-ai-calls"  # container name in your storage account
    environment: str = "development"
    secret_key: str = "change-me-in-production"
    # Outbound notifications (NotifyAdapter)
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = "noreply@yourvoiceai.com"
    twilio_sms_from: str = ""  # E.164 number to send SMS from
    internal_secret: str = ""  # Shared secret for /internal/* endpoints — set in production


settings = Settings()
