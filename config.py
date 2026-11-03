import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ===== BOT =====
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

    # ===== SUPPORT =====
    SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
    SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "")
    BANK_NAME = os.getenv("BANK_NAME", "СБП")

    # ===== 3X-UI =====
    XUI_URL = os.getenv("XUI_URL", "http://127.0.0.1:2053").rstrip("/")
    XUI_USERNAME = os.getenv("XUI_USERNAME", "admin")
    XUI_PASSWORD = os.getenv("XUI_PASSWORD", "")
    XUI_INBOUND_ID = int(os.getenv("XUI_INBOUND_ID", "1"))
    # Дополнительный инбаунд Hysteria2 (0 = искать/создавать автоматически)
    XUI_HYSTERIA_INBOUND_ID = int(os.getenv("XUI_HYSTERIA_INBOUND_ID", "0"))
    XUI_HYSTERIA_PORT = int(os.getenv("XUI_HYSTERIA_PORT", "443"))
    XUI_ENABLE_HYSTERIA = os.getenv("XUI_ENABLE_HYSTERIA", "true").lower() == "true"
    XUI_PUBLIC_HOST = os.getenv("XUI_PUBLIC_HOST", "")
    XUI_SUB_URL = os.getenv("XUI_SUB_URL", "").rstrip("/")
    DEFAULT_DATA_LIMIT_GB = int(os.getenv("DEFAULT_DATA_LIMIT_GB", "100"))
    LIMIT_IP = int(os.getenv("LIMIT_IP", "0"))
    SERVER_LABEL = os.getenv("SERVER_LABEL", "VPN")

    # ===== CRYPTOBOT =====
    CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
    CRYPTOBOT_ENABLED = os.getenv("CRYPTOBOT_ENABLED", "false").lower() == "true"
    CRYPTOBOT_TESTNET = os.getenv("CRYPTOBOT_TESTNET", "true").lower() == "true"

    # ===== TRIAL & PRICES =====
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))
    PRICE_1_MONTH = int(os.getenv("PRICE_1_MONTH", "100"))
    # Длительность тарифа «1 месяц» в днях (по умолчанию 30, а не календарный месяц)
    MONTH_DAYS = int(os.getenv("MONTH_DAYS", "30"))

    # ===== ADMIN COMMANDS =====
    ADDITIONAL_ADMINS = [
        int(x.strip())
        for x in os.getenv("ADDITIONAL_ADMINS", "").split(",")
        if x.strip().isdigit()
    ]

    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        return user_id == cls.ADMIN_ID or user_id in cls.ADDITIONAL_ADMINS
