import json
import uuid
import secrets
import logging
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
import httpx
from config import Config

logger = logging.getLogger(__name__)


class XUIAPI:
    """Асинхронный клиент для работы с панелью 3x-ui (MHSanaei/3x-ui)."""

    def __init__(self):
        self.base_url = Config.XUI_URL.rstrip("/")
        self.username = Config.XUI_USERNAME
        self.password = Config.XUI_PASSWORD
        self.inbound_id = Config.XUI_INBOUND_ID
        self.cookies = None
        self.cookie_expires = None

    async def _login(self) -> bool:
        """Авторизация в 3x-ui и сохранение сессионной cookie."""
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                response = await client.post(
                    f"{self.base_url}/login",
                    data={"username": self.username, "password": self.password},
                )
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    self.cookies = response.cookies
                    self.cookie_expires = datetime.now() + timedelta(hours=11)
                    logger.info("3x-ui: успешная авторизация")
                    return True
                logger.error(f"3x-ui: ошибка входа: {data.get('msg')}")
            else:
                logger.error(f"3x-ui: HTTP {response.status_code} при авторизации")
        except Exception as e:
            logger.error(f"3x-ui: исключение при авторизации: {e}")
        return False

    async def _ensure_auth(self):
        if self.cookies and self.cookie_expires and datetime.now() < self.cookie_expires:
            return
        if not await self._login():
            raise RuntimeError("Не удалось авторизоваться в панели 3x-ui")

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Выполняет запрос к API 3x-ui с автоматическим переподключением при истечении сессии."""
        await self._ensure_auth()
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15.0, verify=False, cookies=self.cookies) as client:
            response = await client.request(method, url, **kwargs)
            # Если сессия истекла, пробуем перелогиниться один раз
            if response.status_code in (401, 403, 302, 307) or "login" in str(response.url):
                await self._login()
                response = await client.request(method, url, cookies=self.cookies, **kwargs)

        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            return response.json()
        except Exception:
            # Возможно, сессия слетела и вернулась HTML-страница логина
            if await self._login():
                async with httpx.AsyncClient(timeout=15.0, verify=False, cookies=self.cookies) as client:
                    response = await client.request(method, url, **kwargs)
                return response.json()
            raise RuntimeError("Некорректный JSON-ответ от 3x-ui")

    def _resolve_host(self) -> str:
        if Config.XUI_PUBLIC_HOST:
            return Config.XUI_PUBLIC_HOST.strip()
        parsed = urlparse(self.base_url)
        return parsed.hostname or "127.0.0.1"

    def _build_client_link(self, inbound: dict, client: dict) -> str:
        """Формирует ссылку подписки или прямую ссылку (VLESS / Hysteria2 / Trojan / VMess) из настроек инбаунда 3x-ui."""
        sub_id = client.get("subId", "")
        if Config.XUI_SUB_URL and sub_id:
            return f"{Config.XUI_SUB_URL}/{sub_id}"

        protocol = (inbound.get("protocol") or "vless").lower()
        port = inbound.get("port", 443)
        host = self._resolve_host()
        remark = quote(f"{Config.SERVER_LABEL} ({client.get('email', 'VPN')})")

        try:
            stream = json.loads(inbound.get("streamSettings") or "{}")
        except json.JSONDecodeError:
            stream = {}

        network = stream.get("network", "tcp")
        security = stream.get("security", "none")

        if protocol == "vless":
            client_uuid = client.get("id", "")
            params = [f"type={network}", "encryption=none"]

            if security == "reality":
                params.append("security=reality")
                reality = stream.get("realitySettings") or {}
                r_settings = reality.get("settings") or {}
                pbk = r_settings.get("publicKey", "")
                fp = r_settings.get("fingerprint") or "chrome"
                spx = r_settings.get("spiderX") or "/"
                server_names = reality.get("serverNames") or [""]
                short_ids = reality.get("shortIds") or [""]
                sni = server_names[0] if server_names else ""
                sid = short_ids[0] if short_ids else ""

                if pbk:
                    params.append(f"pbk={quote(pbk)}")
                if fp:
                    params.append(f"fp={quote(fp)}")
                if sni:
                    params.append(f"sni={quote(sni)}")
                if sid:
                    params.append(f"sid={quote(sid)}")
                if spx:
                    params.append(f"spx={quote(spx)}")
            elif security == "tls":
                params.append("security=tls")
                tls_settings = stream.get("tlsSettings") or {}
                server_name = tls_settings.get("serverName") or host
                params.append(f"sni={quote(server_name)}")

            flow = client.get("flow", "")
            if flow:
                params.append(f"flow={quote(flow)}")

            if network == "ws":
                ws = stream.get("wsSettings") or {}
                path = ws.get("path", "/")
                params.append(f"path={quote(path)}")
            elif network == "grpc":
                grpc = stream.get("grpcSettings") or {}
                service_name = grpc.get("serviceName", "")
                if service_name:
                    params.append(f"serviceName={quote(service_name)}")

            query = "&".join(params)
            return f"vless://{client_uuid}@{host}:{port}?{query}#{remark}"

        elif protocol == "hysteria2":
            password = client.get("password") or client.get("id") or ""
            tls_settings = stream.get("tlsSettings") or {}
            sni = tls_settings.get("serverName") or host
            return f"hysteria2://{quote(password)}@{host}:{port}?sni={quote(sni)}&insecure=0#{remark}"

        elif protocol == "trojan":
            password = client.get("password") or client.get("id") or ""
            return f"trojan://{quote(password)}@{host}:{port}?security={security}&type={network}#{remark}"

        return f"{self.base_url}/sub/{sub_id}" if sub_id else ""

    async def get_inbound(self) -> dict:
        """Получает объект инбаунда по XUI_INBOUND_ID."""
        res = await self._request("GET", f"/panel/api/inbounds/get/{self.inbound_id}")
        if not res.get("success") or not res.get("obj"):
            raise RuntimeError(res.get("msg") or f"Инбаунд ID={self.inbound_id} не найден")
        return res["obj"]

    def _find_client_in_inbound(self, inbound: dict, email: str) -> dict | None:
        try:
            settings = json.loads(inbound.get("settings") or "{}")
        except json.JSONDecodeError:
            return None
        for c in settings.get("clients") or []:
            if c.get("email") == email:
                return c
        return None

    async def get_user(self, email: str) -> dict:
        """Возвращает информацию о клиенте и его трафике из 3x-ui."""
        try:
            inbound = await self.get_inbound()
            client = self._find_client_in_inbound(inbound, email)
            if not client:
                return {"success": False, "error": "User not found"}

            # Запрашиваем статистику трафика
            traffic_res = await self._request(
                "GET", f"/panel/api/inbounds/getClientTraffics/{email}"
            )
            t_obj = traffic_res.get("obj") or {}

            up = t_obj.get("up") or 0
            down = t_obj.get("down") or 0
            used_traffic = up + down

            expiry_ms = client.get("expiryTime") or t_obj.get("expiryTime") or 0
            expire_sec = int(expiry_ms / 1000) if expiry_ms > 0 else 0
            total_bytes = client.get("totalGB") or t_obj.get("total") or 0

            enabled = bool(client.get("enable", True))
            now_sec = int(datetime.now().timestamp())
            is_expired = expire_sec > 0 and expire_sec < now_sec
            is_over_limit = total_bytes > 0 and used_traffic >= total_bytes

            if enabled and not is_expired and not is_over_limit:
                status = "active"
            elif is_expired:
                status = "expired"
            else:
                status = "disabled"

            sub_url = self._build_client_link(inbound, client)

            return {
                "success": True,
                "data": {
                    "username": email,
                    "uuid": client.get("id"),
                    "sub_id": client.get("subId"),
                    "status": status,
                    "expire": expire_sec,
                    "used_traffic": used_traffic,
                    "data_limit": total_bytes,
                    "raw_client": client,
                },
                "subscription_url": sub_url,
            }
        except Exception as e:
            logger.error(f"3x-ui get_user({email}) error: {e}")
            return {"success": False, "error": str(e)}

    async def create_or_extend_subscription(
        self,
        user_id: int,
        days: int,
        existing_username: str = None,
        data_limit_gb: int = None,
    ) -> dict:
        """Создает нового клиента в 3x-ui или продлевает существующего."""
        email = existing_username or f"tg_{user_id}"
        limit_gb = data_limit_gb if data_limit_gb is not None else Config.DEFAULT_DATA_LIMIT_GB
        total_bytes = limit_gb * 1024 * 1024 * 1024 if limit_gb > 0 else 0

        existing = await self.get_user(email)
        if existing["success"]:
            user_data = existing["data"]
            raw_client = user_data["raw_client"]
            client_uuid = raw_client.get("id")

            now_ts = int(datetime.now().timestamp())
            current_expire = user_data.get("expire") or 0
            base_ts = max(current_expire, now_ts)
            new_expire_sec = base_ts + int(timedelta(days=days).total_seconds())
            new_expire_ms = new_expire_sec * 1000

            updated_client = dict(raw_client)
            updated_client["enable"] = True
            updated_client["expiryTime"] = new_expire_ms
            updated_client["totalGB"] = total_bytes

            try:
                # Сбрасываем использованный трафик при продлении
                await self._request(
                    "POST",
                    f"/panel/api/inbounds/{self.inbound_id}/resetClientTraffic/{email}",
                )
            except Exception as e:
                logger.warning(f"3x-ui: не удалось сбросить трафик {email}: {e}")

            try:
                payload = {
                    "id": self.inbound_id,
                    "settings": json.dumps({"clients": [updated_client]}),
                }
                res = await self._request(
                    "POST",
                    f"/panel/api/inbounds/updateClient/{client_uuid}",
                    json=payload,
                )
                if res.get("success"):
                    inbound = await self.get_inbound()
                    sub_url = self._build_client_link(inbound, updated_client)
                    logger.info(f"3x-ui: продлена подписка {email} на {days} дн.")
                    return {
                        "success": True,
                        "username": email,
                        "uuid": client_uuid,
                        "sub_id": updated_client.get("subId", ""),
                        "subscription_url": sub_url,
                        "expire": new_expire_sec,
                        "data_limit": total_bytes,
                        "extended": True,
                    }
                return {"success": False, "error": res.get("msg") or "updateClient failed"}
            except Exception as e:
                logger.error(f"3x-ui: ошибка продления {email}: {e}")
                return {"success": False, "error": str(e)}

        return await self.create_subscription(user_id, email, days, data_limit_gb)

    async def create_subscription(
        self, user_id: int, email: str, days: int, data_limit_gb: int = None
    ) -> dict:
        """Создает нового клиента в инбаунде 3x-ui."""
        expire_sec = int((datetime.now() + timedelta(days=days)).timestamp())
        expire_ms = expire_sec * 1000
        limit_gb = data_limit_gb if data_limit_gb is not None else Config.DEFAULT_DATA_LIMIT_GB
        total_bytes = limit_gb * 1024 * 1024 * 1024 if limit_gb > 0 else 0

        client_uuid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)

        try:
            inbound = await self.get_inbound()
            protocol = (inbound.get("protocol") or "vless").lower()
            try:
                stream = json.loads(inbound.get("streamSettings") or "{}")
            except json.JSONDecodeError:
                stream = {}

            # Для VLESS + TCP + Reality/TLS включаем xtls-rprx-vision
            flow = ""
            if protocol == "vless" and stream.get("network") == "tcp" and stream.get("security") in ("reality", "tls"):
                flow = "xtls-rprx-vision"

            client_obj = {
                "id": client_uuid,
                "flow": flow,
                "email": email,
                "limitIp": Config.LIMIT_IP,
                "totalGB": total_bytes,
                "expiryTime": expire_ms,
                "enable": True,
                "tgId": str(user_id),
                "subId": sub_id,
                "reset": 0,
            }

            payload = {
                "id": self.inbound_id,
                "settings": json.dumps({"clients": [client_obj]}),
            }

            res = await self._request("POST", "/panel/api/inbounds/addClient", json=payload)
            if res.get("success"):
                sub_url = self._build_client_link(inbound, client_obj)
                logger.info(f"3x-ui: создан клиент {email} (UUID={client_uuid})")
                return {
                    "success": True,
                    "username": email,
                    "uuid": client_uuid,
                    "sub_id": sub_id,
                    "subscription_url": sub_url,
                    "expire": expire_sec,
                    "data_limit": total_bytes,
                    "extended": False,
                }
            logger.error(f"3x-ui: ошибка создания клиента {email}: {res}")
            return {"success": False, "error": res.get("msg") or "addClient failed"}
        except Exception as e:
            logger.error(f"3x-ui: исключение при создании клиента {email}: {e}")
            return {"success": False, "error": str(e)}

    async def revoke_subscription(self, email: str) -> bool:
        """Отключает клиента в 3x-ui."""
        try:
            user_res = await self.get_user(email)
            if not user_res["success"]:
                return False
            raw_client = dict(user_res["data"]["raw_client"])
            client_uuid = raw_client.get("id")
            raw_client["enable"] = False

            payload = {
                "id": self.inbound_id,
                "settings": json.dumps({"clients": [raw_client]}),
            }
            res = await self._request(
                "POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json=payload
            )
            return bool(res.get("success"))
        except Exception as e:
            logger.error(f"3x-ui revoke error ({email}): {e}")
            return False


xui = XUIAPI()
