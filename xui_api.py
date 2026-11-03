import json
import os
import sqlite3
import subprocess
import uuid
import secrets
import logging
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
import httpx
from config import Config

logger = logging.getLogger(__name__)


def _parse_json_field(value, default=None):
    """Безопасно парсит поле, которое может быть JSON-строкой (3x-ui v2) или dict (3x-ui v3)."""
    if default is None:
        default = {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else default
        except (json.JSONDecodeError, TypeError, ValueError):
            return default
    return default


class XUIAPI:
    """Асинхронный клиент для работы с панелью 3x-ui (совместим с v2.x и v3.x)."""

    def __init__(self):
        self.base_url = Config.XUI_URL.rstrip("/")
        self.username = Config.XUI_USERNAME
        self.password = Config.XUI_PASSWORD
        self.inbound_id = Config.XUI_INBOUND_ID
        self.hysteria_inbound_id = Config.XUI_HYSTERIA_INBOUND_ID or None
        self.cookies = {}
        self.csrf_token = None
        self.cookie_expires = None

    def _discover_local_xui_urls(self) -> list[str]:
        """Формирует список резервных URL панели (чтение /etc/x-ui/x-ui.db и смена http/https)."""
        candidates = [self.base_url]
        if self.base_url.startswith("http://"):
            candidates.append("https://" + self.base_url[len("http://"):])
        elif self.base_url.startswith("https://"):
            candidates.append("http://" + self.base_url[len("https://"):])

        db_path = "/etc/x-ui/x-ui.db"
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
                conn.close()
                # Первое вхождение ключа — то, которое читает GORM в 3x-ui
                first_settings = {}
                for k, v in rows:
                    if k not in first_settings:
                        first_settings[k] = v
                port = first_settings.get("webPort") or "2053"
                base_path = (first_settings.get("webBasePath") or "/").strip()
                if not base_path.startswith("/"):
                    base_path = "/" + base_path
                base_path = base_path.rstrip("/")
                has_cert = bool(first_settings.get("webCertFile"))
                schemes = ["https", "http"] if has_cert else ["http", "https"]
                for sch in schemes:
                    url = f"{sch}://127.0.0.1:{port}{base_path}".rstrip("/")
                    if url not in candidates:
                        candidates.append(url)
            except Exception as e:
                logger.debug(f"Не удалось прочитать {db_path}: {e}")
        return candidates

    async def _fetch_csrf(self, client: httpx.AsyncClient, base_url: str):
        """Получает CSRF-токен для 3x-ui v3.0+ (если эндпоинт поддерживается)."""
        try:
            resp = await client.get(f"{base_url}/csrf-token")
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("obj") or data.get("csrfToken") or resp.headers.get("X-CSRF-Token")
                if token and isinstance(token, str):
                    self.csrf_token = token
            for name, val in resp.cookies.items():
                if "csrf" in name.lower() and not self.csrf_token:
                    self.csrf_token = val
        except Exception:
            pass

    async def _try_login_url(self, target_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=12.0, verify=False, follow_redirects=True) as client:
                await self._fetch_csrf(client, target_url)
                headers = {"X-Requested-With": "XMLHttpRequest"}
                if self.csrf_token:
                    headers["X-CSRF-Token"] = self.csrf_token

                response = await client.post(
                    f"{target_url}/login",
                    data={"username": self.username, "password": self.password},
                    headers=headers,
                )

                if response.status_code in (400, 403, 415):
                    await self._fetch_csrf(client, target_url)
                    if self.csrf_token:
                        headers["X-CSRF-Token"] = self.csrf_token
                    response = await client.post(
                        f"{target_url}/login",
                        json={"username": self.username, "password": self.password},
                        headers=headers,
                    )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        merged_cookies = {}
                        for k, v in client.cookies.items():
                            merged_cookies[k] = v
                        for k, v in response.cookies.items():
                            merged_cookies[k] = v
                        await self._fetch_csrf(client, target_url)
                        for k, v in client.cookies.items():
                            merged_cookies[k] = v
                        self.cookies = merged_cookies
                        self.base_url = target_url
                        self.cookie_expires = datetime.now() + timedelta(hours=6)
                        logger.info(f"3x-ui: успешная авторизация ({self.base_url})")
                        return True
                    logger.error(f"3x-ui: отказ при входе ({target_url}): {data.get('msg')}")
                else:
                    logger.warning(f"3x-ui: HTTP {response.status_code} при авторизации ({target_url})")
        except Exception as e:
            logger.warning(f"3x-ui: ошибка подключения к {target_url}: {e}")
        return False

    async def _login(self) -> bool:
        """Авторизация в 3x-ui с автоопределением схемы/пути и сохранением сессионных cookies."""
        for candidate_url in self._discover_local_xui_urls():
            if await self._try_login_url(candidate_url):
                return True
        return False

    async def _ensure_auth(self):
        if self.cookies and self.cookie_expires and datetime.now() < self.cookie_expires:
            return
        if not await self._login():
            raise RuntimeError("Не удалось авторизоваться в панели 3x-ui (проверьте XUI_URL, логин и пароль)")

    def _build_headers(self, extra: dict = None) -> dict:
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        if extra:
            headers.update(extra)
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Выполняет запрос к API 3x-ui с автоматическим переподключением при истечении сессии."""
        await self._ensure_auth()
        url = f"{self.base_url}{path}"
        req_headers = self._build_headers(kwargs.pop("headers", None))

        async with httpx.AsyncClient(timeout=15.0, verify=False, cookies=self.cookies) as client:
            response = await client.request(method, url, headers=req_headers, **kwargs)
            if response.headers.get("X-CSRF-Token"):
                self.csrf_token = response.headers["X-CSRF-Token"]

            if response.status_code in (401, 403, 302, 307) or "login" in str(response.url):
                if await self._login():
                    url = f"{self.base_url}{path}"
                    req_headers = self._build_headers()
                    async with httpx.AsyncClient(timeout=15.0, verify=False, cookies=self.cookies) as retry_client:
                        response = await retry_client.request(method, url, headers=req_headers, **kwargs)

        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code} ({path}): {response.text[:200]}")

        try:
            return response.json()
        except Exception:
            if await self._login():
                url = f"{self.base_url}{path}"
                req_headers = self._build_headers()
                async with httpx.AsyncClient(timeout=15.0, verify=False, cookies=self.cookies) as retry_client:
                    response = await retry_client.request(method, url, headers=req_headers, **kwargs)
                return response.json()
            raise RuntimeError(f"Некорректный JSON-ответ от 3x-ui ({path})")

    def _resolve_host(self) -> str:
        if Config.XUI_PUBLIC_HOST:
            return Config.XUI_PUBLIC_HOST.strip()
        parsed = urlparse(self.base_url)
        return parsed.hostname or "127.0.0.1"

    def _build_direct_link(self, inbound: dict, client: dict) -> str:
        """Формирует прямую ссылку подключения (vless://, hysteria2://, trojan://)."""
        protocol = (inbound.get("protocol") or "vless").lower()
        port = inbound.get("port", 443)
        host = self._resolve_host()
        remark = quote(f"{Config.SERVER_LABEL} ({client.get('email', 'VPN')})")

        stream = _parse_json_field(inbound.get("streamSettings"))
        network = stream.get("network", "tcp")
        security = stream.get("security", "none")

        if protocol == "vless":
            client_uuid = client.get("id") or client.get("uuid") or ""
            params = [f"type={network}", "encryption=none"]

            if security == "reality":
                params.append("security=reality")
                reality = stream.get("realitySettings") or {}
                r_settings = reality.get("settings") or {}
                pbk = r_settings.get("publicKey") or reality.get("publicKey") or ""
                fp = r_settings.get("fingerprint") or "chrome"
                spx = r_settings.get("spiderX") or "/"
                server_names = reality.get("serverNames") or [""]
                short_ids = reality.get("shortIds") or [""]
                sni = server_names[0] if server_names else ""
                sid = short_ids[0] if short_ids else ""

                if pbk:
                    params.append(f"pbk={quote(str(pbk))}")
                if fp:
                    params.append(f"fp={quote(str(fp))}")
                if sni:
                    params.append(f"sni={quote(str(sni))}")
                if sid:
                    params.append(f"sid={quote(str(sid))}")
                if spx:
                    params.append(f"spx={quote(str(spx))}")
            elif security == "tls":
                params.append("security=tls")
                tls_settings = stream.get("tlsSettings") or {}
                server_name = tls_settings.get("serverName") or host
                params.append(f"sni={quote(str(server_name))}")

            flow = client.get("flow", "")
            if flow:
                params.append(f"flow={quote(str(flow))}")

            if network == "ws":
                ws = stream.get("wsSettings") or {}
                path = ws.get("path", "/")
                params.append(f"path={quote(str(path))}")
            elif network == "grpc":
                grpc = stream.get("grpcSettings") or {}
                service_name = grpc.get("serviceName", "")
                if service_name:
                    params.append(f"serviceName={quote(str(service_name))}")

            query = "&".join(params)
            return f"vless://{client_uuid}@{host}:{port}?{query}#{remark}"

        elif protocol.startswith("hysteria"):
            password = client.get("password") or client.get("id") or ""
            params = []
            if security == "tls":
                tls_settings = _parse_json_field(stream.get("tlsSettings"))
                server_name = tls_settings.get("serverName") or host
                params.append(f"sni={quote(str(server_name))}")
                alpn = tls_settings.get("alpn") or []
                if alpn:
                    params.append(f"alpn={quote(','.join(alpn))}")
                if tls_settings.get("rejectUnknownCert") or tls_settings.get("insecure"):
                    params.append("insecure=1")
            query = "&".join(params)
            sep = f"{query}&" if query else ""
            # Пустой fragment в конце обязателен для корректного импорта ссылки
            return f"hysteria2://{quote(str(password))}@{host}:{port}?{sep}#{remark}"

        elif protocol == "trojan":
            password = client.get("password") or client.get("id") or ""
            return f"trojan://{quote(str(password))}@{host}:{port}?security={security}&type={network}#{remark}"

        sub_id = client.get("subId", "")
        return f"{self.base_url}/sub/{sub_id}" if sub_id else ""

    def _build_client_link(self, inbound: dict, client: dict) -> str:
        """Возвращает основную ссылку (ссылку подписки 3x-ui, если включен Sub-сервер).

        Ссылка подписки вида {XUI_SUB_URL}/{subId} содержит ВСЕ протоколы клиента
        (vless + hysteria2), поэтому при наличии subId она всегда в приоритете.
        """
        sub_id = client.get("subId", "")
        if Config.XUI_SUB_URL and sub_id:
            return f"{Config.XUI_SUB_URL}/{sub_id}"
        direct_link = self._build_direct_link(inbound, client)
        if direct_link.startswith(("vless://", "hysteria2://", "trojan://")):
            return direct_link
        return direct_link

    # ===== Hysteria2 (второй инбаунд) =========================================

    async def _list_inbounds(self) -> list:
        res = await self._request("GET", "/panel/api/inbounds/list")
        inbounds = res.get("obj") or []
        # В 3x-ui v3 список может быть вложен в {"inbounds": [...]}
        if isinstance(inbounds, dict):
            inbounds = inbounds.get("inbounds") or inbounds.get("data") or []
        return [self._normalize_inbound(i) for i in inbounds]

    @staticmethod
    def _is_hysteria(inb: dict) -> bool:
        """Hysteria2-инбаунд? 3x-ui сохраняет протокол как 'hysteria' (Hy2 — единственный
        поддерживаемый вариант), поэтому принимаем любое имя, начинающееся с 'hysteria'."""
        proto = (inb.get("protocol") or "").lower()
        return proto.startswith("hysteria")

    async def _find_hysteria_in_list(self) -> dict | None:
        """Ищет hysteria2-инбаунд (протокол 'hysteria'/'hysteria2') в списке панели."""
        for inb in await self._list_inbounds():
            if self._is_hysteria(inb):
                self.hysteria_inbound_id = inb.get("id")
                logger.info(f"3x-ui: найден Hysteria2-инбаунд ID={inb.get('id')} ({inb.get('remark')})")
                return inb
        # Фоллбэк: инбаунд с 'hysteria' в названии
        for inb in await self._list_inbounds():
            if "hysteria" in (inb.get("remark") or "").lower():
                self.hysteria_inbound_id = inb.get("id")
                return inb
        return None

    @staticmethod
    def _normalize_inbound(obj: dict) -> dict:
        """Приводит инбаунд из ответа API к единому виду (v2/v3)."""
        if not isinstance(obj, dict):
            return {}
        inner = obj.get("inbound")
        if isinstance(inner, dict):
            merged = dict(inner)
            merged.setdefault("clientStats", obj.get("clientStats"))
            return merged
        return obj

    async def get_hysteria_inbound(self) -> dict | None:
        """Находит (или автоматически создает) инбаунд Hysteria2 в панели 3x-ui."""
        if not Config.XUI_ENABLE_HYSTERIA:
            return None

        # 1. По явно заданному ID (XUI_HYSTERIA_INBOUND_ID)
        if self.hysteria_inbound_id:
            hid = self.hysteria_inbound_id
            obj = {}
            try:
                res = await self._request("GET", f"/panel/api/inbounds/get/{hid}")
                obj = self._normalize_inbound(res.get("obj") or {})
            except Exception as e:
                logger.debug(f"3x-ui: get hysteria inbound {hid}: {e}")

            proto = (obj.get("protocol") or "").lower()
            if self._is_hysteria(obj):
                return obj
            if proto and proto not in ("vless", "trojan", "shadowsocks", "vmess", "wireguard"):
                # 3x-ui может называть протокол 'hysteria' — это и есть Hysteria2
                self.hysteria_inbound_id = hid
                return obj
            if proto:
                logger.warning(
                    f"3x-ui: XUI_HYSTERIA_INBOUND_ID={hid} указывает на инбаунд "
                    f"протокола '{proto}', а не hysteria2 — игнорирую этот ID и ищу hysteria2-инбаунд сам"
                )
                self.hysteria_inbound_id = None
            else:
                # Инбаунд с таким ID не найден (пустой ответ / нет такого ID) —
                # пробуем найти настоящий hysteria2-инбаунд в списке
                try:
                    found = await self._find_hysteria_in_list()
                    if found:
                        return found
                    logger.warning(
                        f"3x-ui: инбаунд с ID={hid} не найден и hysteria2-инбаундов в списке нет"
                    )
                except Exception as e:
                    logger.warning(f"3x-ui: не удалось получить список инбаундов: {e}")
                    return None

        # 2. Поиск любого hysteria2-инбаунда в списке
        try:
            found = await self._find_hysteria_in_list()
            if found:
                return found
        except Exception as e:
            logger.warning(f"3x-ui: не удалось получить список инбаундов: {e}")
            return None

        # 3. Авто-создание
        try:
            return await self._auto_create_hysteria_inbound()
        except Exception as e:
            logger.warning(f"3x-ui: не удалось создать Hysteria2 инбаунд автоматически: {e}")
            return None

    async def _auto_create_hysteria_inbound(self) -> dict | None:
        """Создает инбаунд Hysteria2 (порт XUI_HYSTERIA_PORT/udp) с TLS-сертификатом панели."""
        host = self._resolve_host()
        cert_path = f"/etc/letsencrypt/live/{host}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{host}/privkey.pem"
        has_cert = os.path.exists(cert_path) and os.path.exists(key_path)

        settings_json = json.dumps({"clients": []})
        if has_cert:
            stream_json = json.dumps({
                "network": "raw",
                "security": "tls",
                "externalProxy": [],
                "tlsSettings": {
                    "serverName": host,
                    "alpn": ["h3"],
                    "certificates": [{"certFile": cert_path, "keyFile": key_path}],
                    "rejectUnknownCert": False,
                    "maxVersion": "1.3",
                    "minVersion": "1.2",
                },
            })
        else:
            # Без сертификата — самоподписанный режим (обязательно insecure=1 у клиента)
            stream_json = json.dumps({
                "network": "raw",
                "security": "tls",
                "externalProxy": [],
                "tlsSettings": {
                    "serverName": host,
                    "alpn": ["h3"],
                    "rejectUnknownCert": True,
                    "insecure": True,
                    "maxVersion": "1.3",
                    "minVersion": "1.2",
                },
            })

        payload = {
            "up": 0,
            "down": 0,
            "total": 0,
            "remark": f"{Config.SERVER_LABEL}-Hysteria2",
            "enable": True,
            "expiryTime": 0,
            "listen": "",
            "port": Config.XUI_HYSTERIA_PORT,
            "protocol": "hysteria2",
            "settings": settings_json,
            "streamSettings": stream_json,
            "sniffing": json.dumps({"enabled": False}),
        }
        res = await self._request("POST", "/panel/api/inbounds/add", json=payload)
        if res.get("success"):
            inb = res.get("obj") or {}
            if not inb.get("id"):
                # some versions return no object on add — find it via list
                for inb_cand in await self._list_inbounds():
                    if self._is_hysteria(inb_cand):
                        inb = inb_cand
                        break
            self.hysteria_inbound_id = inb.get("id")
            logger.info(f"3x-ui: автоматически создан инбаунд Hysteria2 (ID={self.hysteria_inbound_id})")
            return inb
        logger.warning(f"3x-ui: создание Hysteria2 инбаунда отклонено: {res.get('msg')}")
        return None

    async def _ensure_hysteria_client(self, email: str, password: str, expiry_ms: int,
                                       total_bytes: int, user_id: int,
                                       sub_id: str) -> bool:
        """Добавляет/обновляет клиента в Hysteria2-инбаунде с тем же subId и паролем."""
        inbound = await self.get_hysteria_inbound()
        if not inbound:
            return False

        hy_client = {
            "email": email,
            "password": password,
            "flow": "",
            "limitIp": Config.LIMIT_IP,
            "totalGB": total_bytes,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": int(user_id) if user_id else 0,
            "subId": sub_id,
            "comment": "",
            "reset": 0,
        }

        existing = self._find_client_in_inbound(inbound, email)
        hid = inbound.get("id", self.hysteria_inbound_id)
        self.hysteria_inbound_id = hid

        if existing:
            updated = dict(existing)
            updated.update({
                "password": password,
                "totalGB": total_bytes,
                "expiryTime": expiry_ms,
                "enable": True,
                "subId": updated.get("subId") or sub_id,
            })
            payload = {"id": hid, "settings": json.dumps({"clients": [updated]})}
            key = updated.get("password") or updated.get("id") or email
            for path in (f"/panel/api/inbounds/updateClient/{quote(str(key))}",
                         f"/panel/api/clients/update/{quote(email)}"):
                try:
                    res = await self._request("POST", path, json=payload if "inbounds" in path else updated)
                    if res.get("success"):
                        return True
                except Exception as e:
                    logger.debug(f"3x-ui hysteria update ({path}): {e}")
            return False

        for tg_val in (int(user_id) if user_id else 0, str(user_id) if user_id else ""):
            hy_client["tgId"] = tg_val
            payload = {"id": hid, "settings": json.dumps({"clients": [hy_client]})}
            try:
                res = await self._request("POST", "/panel/api/inbounds/addClient", json=payload)
                if res.get("success"):
                    return True
            except Exception as e:
                logger.debug(f"3x-ui hysteria addClient attempt ({type(tg_val).__name__}): {e}")

        # Фоллбэк для 3x-ui v3.x
        try:
            res = await self._request(
                "POST", "/panel/api/clients/add",
                json={"client": hy_client, "inboundIds": [hid]},
            )
            return bool(res.get("success"))
        except Exception as e:
            logger.warning(f"3x-ui: не удалось добавить Hysteria2-клиента {email}: {e}")
            return False

    async def _get_hysteria_password(self, email: str) -> str | None:
        """Возвращает пароль существующего Hysteria2-клиента (чтобы не менять его при продлении)."""
        inbound = await self.get_hysteria_inbound()
        if not inbound:
            return None
        client = self._find_client_in_inbound(inbound, email)
        if client:
            return client.get("password") or client.get("id")
        return None

    async def _try_reset_hysteria_traffic(self, email: str):
        inbound = await self.get_hysteria_inbound()
        if not inbound:
            return
        hid = inbound.get("id", self.hysteria_inbound_id)
        for path in (f"/panel/api/inbounds/{hid}/resetClientTraffic/{quote(email)}",
                     f"/panel/api/clients/resetTraffic/{quote(email)}"):
            try:
                await self._request("POST", path)
                return
            except Exception:
                continue

    async def sync_all_hysteria_clients(self) -> dict:
        """Синхронизирует ВСЕ существующие клиенты VLESS-инбаунда с Hysteria2-инбаундом.

        Вызывается при старте бота: старые подписки получают hysteria2-клиента
        с тем же subId, после чего ссылка /sub/{subId} начинает отдавать vless+hysteria2.
        """
        result = {"checked": 0, "added": 0, "updated": 0, "failed": 0,
                  "hysteria_inbound": None, "error": ""}
        try:
            hy_inbound = await self.get_hysteria_inbound()
            if not hy_inbound:
                result["error"] = "Hysteria2-инбаунд не найден и не может быть создан"
                logger.warning("3x-ui sync: %s", result["error"])
                return result
            result["hysteria_inbound"] = hy_inbound.get("id")

            vless_inbound = await self.get_inbound()
            clients = _parse_json_field(vless_inbound.get("settings")).get("clients") or []
            for c in clients:
                email = c.get("email") or ""
                if not email:
                    continue
                result["checked"] += 1
                sub_id = c.get("subId") or secrets.token_hex(8)
                expiry_ms = c.get("expiryTime") or 0
                total_bytes = c.get("totalGB") or 0
                tg_id = c.get("tgId") or 0
                try:
                    uid = int(tg_id) if str(tg_id).isdigit() else 0
                except Exception:
                    uid = 0
                existing_hy = self._find_client_in_inbound(hy_inbound, email)
                password = (existing_hy or {}).get("password") or secrets.token_urlsafe(18)
                try:
                    ok = await self._ensure_hysteria_client(
                        email, password, expiry_ms, total_bytes, uid, sub_id
                    )
                    if ok:
                        if existing_hy:
                            result["updated"] += 1
                        else:
                            result["added"] += 1
                            # обновим локальную копию инбаунда, чтобы не добавлять дважды
                            hy_inbound.setdefault("_synced", []).append(email)
                    else:
                        result["failed"] += 1
                        logger.warning(f"3x-ui sync: не удалось синхронизировать {email}")
                except Exception as e:
                    result["failed"] += 1
                    logger.warning(f"3x-ui sync ({email}) error: {e}")

            logger.info(
                f"3x-ui sync Hysteria2: проверено={result['checked']} "
                f"добавлено={result['added']} обновлено={result['updated']} ошибок={result['failed']}"
            )
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"3x-ui sync_all_hysteria_clients error: {e}")
        return result

    async def diagnose_hysteria(self) -> list[str]:
        """Возвращает список строк диагностики состояния Hysteria2 (для /diag у админа)."""
        lines = []
        try:
            inbounds = await self._list_inbounds()
            lines.append(f"Инбаунды в панели ({len(inbounds)}):")
            for inb in inbounds:
                n_clients = len(_parse_json_field(inb.get("settings")).get("clients") or [])
                lines.append(
                    f"  ID={inb.get('id')} proto={inb.get('protocol')} port={inb.get('port')} "
                    f"remark='{inb.get('remark')}' enable={inb.get('enable')} clients={n_clients}"
                )
            hy = await self.get_hysteria_inbound()
            if hy:
                lines.append(f"Hysteria2-инбаунд: ID={hy.get('id')}, порт={hy.get('port')}, "
                             f"enable={hy.get('enable')}")
                stream = _parse_json_field(hy.get("streamSettings"))
                tls = _parse_json_field(stream.get("tlsSettings"))
                lines.append(f"  stream: network={stream.get('network')} security={stream.get('security')} "
                             f"sni={tls.get('serverName')}")
                hyc = _parse_json_field(hy.get("settings")).get("clients") or []
                lines.append(f"  клиентов в Hysteria2: {len(hyc)}")
            else:
                lines.append("Hysteria2-инбаунд НЕ найден (XUI_ENABLE_HYSTERIA="
                             f"{Config.XUI_ENABLE_HYSTERIA})")
            lines.append(f"XUI_SUB_URL={Config.XUI_SUB_URL}")
        except Exception as e:
            lines.append(f"Ошибка диагностики: {e}")
        return lines

    def _collect_result_links(self, vless_inbound: dict, hy_inbound: dict | None,
                              client_obj: dict, sub_id: str) -> dict:
        """Формирует набор ссылок для ответа боту: подписка (оба протокола) + прямые ключи."""
        main_url = self._build_client_link(vless_inbound, client_obj)
        sub_page_url = f"{Config.XUI_SUB_URL}/{sub_id}" if (Config.XUI_SUB_URL and sub_id) else ""
        vless_link = self._build_direct_link(vless_inbound, client_obj)
        hysteria_link = ""
        if hy_inbound:
            hysteria_link = self._build_direct_link(hy_inbound, client_obj)
        return {
            "subscription_url": main_url,
            "sub_page_url": sub_page_url,
            "vless_link": vless_link if vless_link.startswith("vless://") else "",
            "hysteria_link": hysteria_link if hysteria_link.startswith("hysteria2://") else "",
        }


    async def _generate_x25519_keys(self) -> tuple[str, str]:
        """Генерирует пару ключей X25519 через API 3x-ui или бинарник xray."""
        for endpoint in ("/panel/api/server/getNewX25519Cert", "/server/getNewX25519Cert"):
            try:
                res = await self._request("POST", endpoint)
                obj = res.get("obj") or {}
                priv = obj.get("privateKey")
                pub = obj.get("publicKey")
                if priv and pub:
                    return priv, pub
            except Exception:
                pass
            try:
                res = await self._request("GET", endpoint)
                obj = res.get("obj") or {}
                priv = obj.get("privateKey")
                pub = obj.get("publicKey")
                if priv and pub:
                    return priv, pub
            except Exception:
                pass

        # Фоллбэк на локальный бинарник xray
        try:
            xray_bins = [
                "/usr/local/x-ui/bin/xray-linux-amd64",
                "/usr/local/x-ui/bin/xray-linux-arm64",
            ]
            for b in xray_bins:
                if os.path.exists(b):
                    out = subprocess.check_output([b, "x25519"], text=True, timeout=5)
                    priv, pub = "", ""
                    for line in out.splitlines():
                        low = line.lower()
                        if "private" in low:
                            priv = line.split(":")[-1].strip()
                        elif "public" in low or "password" in low:
                            if not pub:
                                pub = line.split(":")[-1].strip()
                    if priv and pub:
                        return priv, pub
        except Exception as e:
            logger.warning(f"Не удалось сгенерировать ключи через xray CLI: {e}")

        raise RuntimeError("Не удалось сгенерировать ключи Reality X25519")

    async def _auto_create_default_inbound(self) -> dict:
        """Автоматически создает инбаунд VLESS + Reality (порт 443), если в панели 0 инбаундов."""
        priv_key, pub_key = await self._generate_x25519_keys()
        short_id = secrets.token_hex(8)
        dest_host = "www.microsoft.com"

        settings_json = json.dumps({
            "clients": [],
            "decryption": "none",
            "fallbacks": []
        })
        stream_json = json.dumps({
            "network": "tcp",
            "security": "reality",
            "externalProxy": [],
            "realitySettings": {
                "show": False,
                "xver": 0,
                "dest": f"{dest_host}:443",
                "serverNames": [dest_host],
                "privateKey": priv_key,
                "minClient": "",
                "maxClient": "",
                "maxTimediff": 0,
                "shortIds": [short_id],
                "settings": {
                    "publicKey": pub_key,
                    "fingerprint": "chrome",
                    "serverName": "",
                    "spiderX": "/"
                }
            },
            "tcpSettings": {
                "acceptProxyProtocol": False,
                "header": {"type": "none"}
            }
        })
        sniffing_json = json.dumps({
            "enabled": True,
            "destOverride": ["http", "tls", "quic", "fakedns"],
            "metadataOnly": False,
            "routeOnly": False
        })

        payload = {
            "up": 0,
            "down": 0,
            "total": 0,
            "remark": f"{Config.SERVER_LABEL}-VLESS-Reality",
            "enable": True,
            "expiryTime": 0,
            "listen": "",
            "port": 443,
            "protocol": "vless",
            "settings": settings_json,
            "streamSettings": stream_json,
            "sniffing": sniffing_json
        }
        res = await self._request("POST", "/panel/api/inbounds/add", json=payload)
        if res.get("success") and res.get("obj"):
            inb = res["obj"]
            self.inbound_id = inb.get("id", 1)
            logger.info(f"3x-ui: автоматически создан инбаунд VLESS+Reality (ID={self.inbound_id})")
            return inb

        list_res = await self._request("GET", "/panel/api/inbounds/list")
        inbounds = list_res.get("obj") or []
        if inbounds:
            self.inbound_id = inbounds[0]["id"]
            return inbounds[0]
        raise RuntimeError(f"Не удалось автоматически создать инбаунд: {res.get('msg')}")

    async def get_inbound(self) -> dict:
        """Получает объект инбаунда по XUI_INBOUND_ID (или автоматически находит/создает его)."""
        try:
            res = await self._request("GET", f"/panel/api/inbounds/get/{self.inbound_id}")
            if res.get("success") and res.get("obj"):
                return self._normalize_inbound(res["obj"])
        except Exception as e:
            logger.warning(f"3x-ui get/{self.inbound_id} fallback to list: {e}")

        list_res = await self._request("GET", "/panel/api/inbounds/list")
        inbounds = list_res.get("obj") or []
        for inb in inbounds:
            if inb.get("id") == self.inbound_id:
                return inb
        if inbounds:
            self.inbound_id = inbounds[0]["id"]
            logger.info(f"3x-ui: переключен inbound_id на {self.inbound_id}")
            return inbounds[0]

        logger.warning("3x-ui: в панели нет инбаундов, запускаю автоматическое создание VLESS+Reality...")
        return await self._auto_create_default_inbound()

    def _find_client_in_inbound(self, inbound: dict, email: str) -> dict | None:
        settings = _parse_json_field(inbound.get("settings"))
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

            t_obj = {}
            for stat in inbound.get("clientStats") or []:
                if stat.get("email") == email:
                    t_obj = stat
                    break

            if not t_obj:
                try:
                    traffic_res = await self._request(
                        "GET", f"/panel/api/inbounds/getClientTraffics/{email}"
                    )
                    t_obj = traffic_res.get("obj") or {}
                except Exception:
                    t_obj = {}

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
            sub_id = client.get("subId", "")
            sub_page_url = f"{Config.XUI_SUB_URL}/{sub_id}" if (Config.XUI_SUB_URL and sub_id) else ""

            # Прямые ссылки vless + hysteria2 (гистерия-клиент с тем же subId/email)
            vless_link = self._build_direct_link(inbound, client)
            hysteria_link = ""
            hy_inbound = await self.get_hysteria_inbound()
            if hy_inbound:
                hy_client = self._find_client_in_inbound(hy_inbound, email)
                if hy_client:
                    hysteria_link = self._build_direct_link(hy_inbound, hy_client)

            return {
                "success": True,
                "data": {
                    "username": email,
                    "uuid": client.get("id") or client.get("uuid"),
                    "sub_id": sub_id,
                    "status": status,
                    "expire": expire_sec,
                    "used_traffic": used_traffic,
                    "data_limit": total_bytes,
                    "raw_client": client,
                },
                "subscription_url": sub_url,
                "sub_page_url": sub_page_url,
                "vless_link": vless_link if vless_link.startswith("vless://") else "",
                "hysteria_link": hysteria_link,
            }
        except Exception as e:
            logger.error(f"3x-ui get_user({email}) error: {e}")
            return {"success": False, "error": str(e)}

    async def _post_update_client(self, client_uuid: str, email: str, updated_client: dict, user_id: int) -> dict:
        """Обновляет клиента с автоматическим подбором типа tgId (int для v3 / str для v2)."""
        for tg_val in (int(user_id) if user_id else 0, str(user_id) if user_id else ""):
            updated_client["tgId"] = tg_val
            payload = {
                "id": self.inbound_id,
                "settings": json.dumps({"clients": [updated_client]}),
            }
            try:
                res = await self._request(
                    "POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json=payload
                )
                if res.get("success"):
                    return res
            except Exception as e:
                logger.warning(f"updateClient attempt ({type(tg_val).__name__}) error: {e}")

        # Фоллбэк на эндпоинт 3x-ui v3.x
        updated_client["tgId"] = int(user_id) if user_id else 0
        return await self._request(
            "POST", f"/panel/api/clients/update/{quote(email)}", json=updated_client
        )

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
            client_uuid = raw_client.get("id") or raw_client.get("uuid")

            now_ts = int(datetime.now().timestamp())
            current_expire = user_data.get("expire") or 0
            base_ts = max(current_expire, now_ts)
            new_expire_sec = base_ts + int(timedelta(days=days).total_seconds())
            new_expire_ms = new_expire_sec * 1000

            updated_client = dict(raw_client)
            updated_client["enable"] = True
            updated_client["expiryTime"] = new_expire_ms
            updated_client["totalGB"] = total_bytes
            if not updated_client.get("subId"):
                updated_client["subId"] = secrets.token_hex(8)

            try:
                await self._request(
                    "POST",
                    f"/panel/api/inbounds/{self.inbound_id}/resetClientTraffic/{email}",
                )
            except Exception:
                try:
                    await self._request("POST", f"/panel/api/clients/resetTraffic/{quote(email)}")
                except Exception as e:
                    logger.warning(f"3x-ui: не удалось сбросить трафик {email}: {e}")

            try:
                res = await self._post_update_client(client_uuid, email, updated_client, user_id)
                if res.get("success"):
                    sub_id = updated_client.get("subId", "")

                    # Синхронизируем клиента с Hysteria2-инбаундом (тот же subId -> обе ссылки в подписке)
                    hy_password = await self._get_hysteria_password(email) or secrets.token_urlsafe(18)
                    hy_ok = False
                    try:
                        hy_ok = await self._ensure_hysteria_client(
                            email, hy_password, new_expire_ms, total_bytes, user_id, sub_id
                        )
                        if hy_ok:
                            await self._try_reset_hysteria_traffic(email)
                    except Exception as he:
                        logger.warning(f"3x-ui: не удалось синхронизировать Hysteria2-клиента {email}: {he}")

                    inbound = await self.get_inbound()
                    hy_inbound = await self.get_hysteria_inbound() if hy_ok else None
                    link_client = dict(updated_client)
                    link_client["password"] = hy_password
                    links = self._collect_result_links(inbound, hy_inbound, link_client, sub_id)

                    logger.info(f"3x-ui: продлена подписка {email} на {days} дн. (hysteria2: {'OK' if hy_ok else 'нет'})")
                    return {
                        "success": True,
                        "username": email,
                        "uuid": client_uuid,
                        "sub_id": sub_id,
                        **links,
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
            stream = _parse_json_field(inbound.get("streamSettings"))

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
                "tgId": int(user_id) if user_id else 0,
                "subId": sub_id,
                "comment": "",
                "reset": 0,
            }

            res = None
            for tg_val in (int(user_id) if user_id else 0, str(user_id) if user_id else ""):
                client_obj["tgId"] = tg_val
                payload = {
                    "id": self.inbound_id,
                    "settings": json.dumps({"clients": [client_obj]}),
                }
                try:
                    res = await self._request("POST", "/panel/api/inbounds/addClient", json=payload)
                    if res.get("success"):
                        break
                except Exception as e:
                    logger.warning(f"addClient attempt ({type(tg_val).__name__}) error: {e}")

            # Фоллбэк для 3x-ui v3.x (/panel/api/clients/add)
            if not res or not res.get("success"):
                v3_client = dict(client_obj)
                v3_client["uuid"] = client_uuid
                v3_client["tgId"] = int(user_id) if user_id else 0
                v3_payload = {"client": v3_client, "inboundIds": [self.inbound_id]}
                try:
                    res = await self._request("POST", "/panel/api/clients/add", json=v3_payload)
                except Exception as v3_err:
                    if not res:
                        raise v3_err

            if res and res.get("success"):
                # Добавляем того же клиента в Hysteria2-инбаунд с тем же subId,
                # чтобы в подписке 3x-ui отдавались обе ссылки: vless + hysteria2.
                hy_password = secrets.token_urlsafe(18)
                hy_ok = False
                try:
                    hy_ok = await self._ensure_hysteria_client(
                        email, hy_password, expire_ms, total_bytes, user_id, sub_id
                    )
                except Exception as he:
                    logger.warning(f"3x-ui: не удалось создать Hysteria2-клиента {email}: {he}")

                hy_inbound = await self.get_hysteria_inbound() if hy_ok else None
                link_client = dict(client_obj)
                link_client["password"] = hy_password
                links = self._collect_result_links(inbound, hy_inbound, link_client, sub_id)

                logger.info(
                    f"3x-ui: создан клиент {email} (UUID={client_uuid}, "
                    f"hysteria2: {'OK' if hy_ok else 'нет'})"
                )
                return {
                    "success": True,
                    "username": email,
                    "uuid": client_uuid,
                    "sub_id": sub_id,
                    **links,
                    "expire": expire_sec,
                    "data_limit": total_bytes,
                    "extended": False,
                }
            logger.error(f"3x-ui: ошибка создания клиента {email}: {res}")
            return {"success": False, "error": (res or {}).get("msg") or "addClient failed"}
        except Exception as e:
            logger.error(f"3x-ui: исключение при создании клиента {email}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def revoke_subscription(self, email: str) -> bool:
        """Отключает клиента в 3x-ui (включая Hysteria2-инбаунд)."""
        try:
            user_res = await self.get_user(email)
            if not user_res["success"]:
                return False
            raw_client = dict(user_res["data"]["raw_client"])
            client_uuid = raw_client.get("id") or raw_client.get("uuid")
            raw_client["enable"] = False
            tg_id = raw_client.get("tgId") or 0
            res = await self._post_update_client(
                client_uuid, email, raw_client, int(tg_id) if str(tg_id).isdigit() else 0
            )

            # Отключаем того же клиента и в Hysteria2-инбаунде
            try:
                hy_inbound = await self.get_hysteria_inbound()
                if hy_inbound:
                    hy_client = self._find_client_in_inbound(hy_inbound, email)
                    if hy_client:
                        hid = hy_inbound.get("id", self.hysteria_inbound_id)
                        disabled = dict(hy_client)
                        disabled["enable"] = False
                        payload = {"id": hid, "settings": json.dumps({"clients": [disabled]})}
                        key = disabled.get("password") or disabled.get("id") or email
                        try:
                            await self._request(
                                "POST", f"/panel/api/inbounds/updateClient/{quote(str(key))}", json=payload
                            )
                        except Exception:
                            await self._request(
                                "POST", f"/panel/api/clients/update/{quote(email)}", json=disabled
                            )
            except Exception as he:
                logger.warning(f"3x-ui: не удалось отключить Hysteria2-клиента {email}: {he}")

            return bool(res.get("success"))
        except Exception as e:
            logger.error(f"3x-ui revoke error ({email}): {e}")
            return False


xui = XUIAPI()
