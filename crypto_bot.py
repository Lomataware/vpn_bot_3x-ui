import httpx
import logging
from config import Config

logger = logging.getLogger(__name__)


class CryptoBotAPI:
    MAINNET_URL = "https://pay.crypt.bot/api"
    TESTNET_URL = "https://testnet-pay.crypt.bot/api"

    ACCEPTED_ASSETS = "USDT,TON,SOL,TRX,BTC,ETH,DOGE,LTC,BNB,USDC"

    def __init__(self):
        self.token = Config.CRYPTOBOT_TOKEN
        self.base_url = self.TESTNET_URL if Config.CRYPTOBOT_TESTNET else self.MAINNET_URL

    def _headers(self):
        return {
            "Crypto-Pay-API-Token": self.token,
            "Accept": "application/json"
        }

    async def create_invoice(self, amount: float, currency: str = "RUB",
                             description: str = "", user_id: int = None) -> dict:
        """Создает FIAT-счет в CryptoBot."""
        if not self.token:
            return {"success": False, "error": "CryptoBot token not configured"}

        payload = {
            "currency_type": "fiat",
            "fiat": currency,
            "amount": str(amount),
            "accepted_assets": self.ACCEPTED_ASSETS,
            "description": description or "VPN subscription",
            "payload": str(user_id) if user_id else "",
            "expires_in": "3600"
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/createInvoice",
                    params=payload,
                    headers=self._headers()
                )
            data = response.json()
            if data.get("ok"):
                inv = data["result"]
                logger.info(
                    f"CryptoBot: создан FIAT-инвойс {inv.get('invoice_id')} "
                    f"на сумму {inv.get('amount')} {inv.get('fiat')}"
                )
                return {
                    "success": True,
                    "invoice_id": inv["invoice_id"],
                    "pay_url": inv["pay_url"],
                    "amount": inv.get("amount", str(amount)),
                    "currency": inv.get("fiat") or currency
                }
            else:
                logger.error(f"CryptoBot createInvoice error: {data}")
                return {"success": False, "error": str(data.get("error") or data)}
        except Exception as e:
            logger.error(f"CryptoBot exception: {e}")
            return {"success": False, "error": str(e)}

    async def get_invoice(self, invoice_id: int) -> dict:
        """Проверяет статус инвойса в CryptoBot."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/getInvoices",
                    params={"invoice_ids": invoice_id},
                    headers=self._headers()
                )
            data = response.json()
            if data.get("ok") and data.get("result", {}).get("invoices"):
                inv = data["result"]["invoices"][0]
                return {"success": True, "status": inv.get("status"), "invoice": inv}
            return {"success": False, "error": "Invoice not found"}
        except Exception as e:
            logger.error(f"CryptoBot getInvoices exception: {e}")
            return {"success": False, "error": str(e)}


crypto_bot = CryptoBotAPI()
