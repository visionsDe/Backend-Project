from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MYSQL_USER: str
    MYSQL_PASSWORD: str
    MYSQL_HOST: str
    MYSQL_DB: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 8640
    VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 1440
    RESET_PASS_TOKEN_EXPIRE_MINUTES: int = 60
    EMAIL_USER: str
    EMAIL_PASSWORD: str
    EMAIL_PORT: int
    EMAIL_HOST: str
    BASE_URL: str
    DEFAULT_LANGUAGE:str
    MEI_STRIPE_SECRET_KEY:str
    MEI_STRIPE_WEBHOOK_SECRET_KEY:str
    REMEMBER_ME_EXPIRE_DAYS: int = 30
    FILE_DIR_PATH: str
    FIREBASE_CREDENTIALS_FILE: str
    DASHBOARD_BASE_URL: str
    ENC_SECURE_KEY: str
    OLD_ENC_SECURE_KEY: str
    API_BASE_URL: str
    GOOGLE_CREDENTIALS_FILE: str
    GOOGLE_TOKEN_SCOPE: str
    GOOGLE_SUBSCRIPTION_URL: str
    APPLE_SUBSCRIPTION_URL: str
    APPLE_SECRET_TOKEN: str
    STRIPE_DEFAULT_WEBSITE: str
    LIVE_SITE_URL: str
    GOOGLE_CHECK_SUBSCRIPTION_URL: str
    GOOGLE_PLAY_OFFERS_URL: str = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{package_name}/subscriptions/{product_id}/basePlans/-/offers"
    APPLE_API_KEY_ID: str
    APPLE_API_ISSUER_ID: str
    APPLE_CHECK_SUBSCRIPTION_URL: str
    APPLE_SUBSCRIPTION_GROUP_ID: str
    APPLE_PRIVATE_KEY_FILE: str
    DOC_READER_SDK_URL: str
    FACE_SDK_URL: str
    OPENAI_API_KEY: str
    # ── Shared AI client ──────────────────────────────────────────────
    # Provider slug the shared AIClient uses
    AI_PROVIDER_DEFAULT: str = "openai"
    # Per-feature request-per-minute cap. Features omitted from this dict are
    # unlimited. Keys must match Feature enum values in services.ai_client.
    # Set to a runaway-safety-net level — comfortably above real traffic so
    # normal use never hits it, but a buggy loop still can't hammer OpenAI.
    AI_RATE_LIMITS: dict[str, int] = {
        "moderation": 600,
    }
    # Comma-separated list of IPs allowed to register with disposable emails
    # (QA bypass on dev/staging). MUST be empty on production.
    QA_BYPASS_IPS: str = ""
    # Comma-separated list of browser origins allowed to call the API.
    # Enumerated explicitly rather than "*" because credentialed CORS
    # requests are only permitted against an exact-origin allowlist.
    ALLOWED_ORIGINS: str = ""
    # Startup-time toggles — off by default so tests / local runs stay quiet.
    ENABLE_SCHEDULER: bool = False
    ENABLE_EXCEPTION_LOGGING: bool = False
    # ── Subscription webhook configuration ─────────────────────────────
    # Apple App Store Server Notifications V2
    APPLE_BUNDLE_ID: str
    APPLE_APP_APPLE_ID: int
    APPLE_ROOT_CAS_DIR: str
    APPLE_WEBHOOK_ENVIRONMENT: str
    GOOGLE_WEBHOOK_AUDIENCE: str
    GOOGLE_WEBHOOK_SERVICE_ACCOUNT_EMAIL: str = ""
    # ── Stripe multi-platform configuration ─────────────────────────────
    # Per-country Stripe platform accounts. Existing rows in this table
    # (MEI_STRIPE_SECRET_KEY, MEI_STRIPE_WEBHOOK_SECRET_KEY, STRIPE_DEFAULT_WEBSITE)
    # remain the canonical Brazil platform until STRIPE_BR_* are populated; the
    # helper in app/utils/stripe_client.py falls back to the legacy keys when the
    # BR-specific values are empty so this config is safe to deploy before .env
    # is updated.
    STRIPE_BR_SECRET_KEY: str = ""
    STRIPE_BR_WEBHOOK_SECRET: str = ""
    STRIPE_BR_WEBSITE: str = ""
    STRIPE_CH_SECRET_KEY: str = ""
    STRIPE_CH_WEBHOOK_SECRET: str = ""
    STRIPE_CH_WEBSITE: str = ""

    # ── Apple StoreKit promotional-offer signing (org free-trial gating) ──
    APPLE_PROMO_OFFER_KEY_FILE: str = ""
    APPLE_PROMO_OFFER_KEY_ID: str = ""
    # ── App Store Connect API (custom offer codes, campaign lookup) ──
    APP_STORE_CONNECT_BASE_URL: str = "https://api.appstoreconnect.apple.com"
    APPLE_OFFER_CODE_TOTAL_CODES: int = 5000
    APPLE_OFFER_CODE_EXPIRATION_DAYS: int = 180

    class Config:
        env_file = ".env"

settings = Settings()
