"""Интеграционный тест: hysteria2 присутствует в подписке (mock 3x-ui панели).

Проверяет сценарии бага:
1. 3x-ui сохраняет протокол как 'hysteria' (не 'hysteria2') — бот должен находить инбаунд.
2. XUI_HYSTERIA_INBOUND_ID указывает на неверный/несуществующий ID — поиск по списку.
3. Hysteria-клиент существует, но subId пустой/отличающийся — get_user чинит subId,
   иначе /sub/{subId} не отдаст hysteria2.
4. Hysteria-клиента нет вовсе — get_user создаёт его с тем же subId.
"""
import asyncio
import json
import os
import sys

os.environ["XUI_ENABLE_HYSTERIA"] = "true"
os.environ["XUI_SUB_URL"] = "https://example.com/sub"
os.environ["XUI_HYSTERIA_INBOUND_ID"] = "999"  # заведомо битый ID (нет такого)
sys.path.insert(0, os.path.dirname(__file__))

from config import Config  # noqa: E402
import xui_api  # noqa: E402


class MockPanel:
    """Мок 3x-ui v2.x: протокол hysteria2 хранится как 'hysteria'."""

    def __init__(self):
        self.inbounds = {
            1: {
                "id": 1, "remark": "VLESS-Reality", "protocol": "vless", "port": 443,
                "enable": True,
                "settings": json.dumps({"clients": [
                    {"id": "uuid-1", "email": "tg_100", "flow": "", "subId": "abc123",
                     "expiryTime": 0, "totalGB": 0, "enable": True, "tgId": 100, "limitIp": 0},
                ]}),
                "streamSettings": json.dumps({"network": "tcp", "security": "reality",
                                              "realitySettings": {"publicKey": "PUB",
                                                                  "serverNames": ["www.microsoft.com"],
                                                                  "shortIds": ["aa"],
                                                                  "settings": {"fingerprint": "chrome"}}}),
                "clientStats": [],
            },
            2: {
                "id": 2, "remark": "VPN-Hysteria2", "protocol": "hysteria", "port": 443,
                "enable": True,
                "settings": json.dumps({"clients": []}),
                "streamSettings": json.dumps({"network": "raw", "security": "tls",
                                              "tlsSettings": {"serverName": "example.com",
                                                              "alpn": ["h3"]}}),
                "clientStats": [],
            },
        }

    def clients_of(self, iid):
        return json.loads(self.inbounds[iid]["settings"]).get("clients", [])

    def set_clients(self, iid, clients):
        self.inbounds[iid]["settings"] = json.dumps({"clients": clients})


panel = MockPanel()


async def mock_request(self, method, path, **kwargs):
    if path == "/panel/api/inbounds/list":
        return {"success": True, "obj": list(panel.inbounds.values())}
    if path.startswith("/panel/api/inbounds/get/"):
        iid = int(path.rsplit("/", 1)[-1])
        inb = panel.inbounds.get(iid)
        if inb is None:
            return {"success": False, "msg": "not found"}
        return {"success": True, "obj": inb}
    if path == "/panel/api/inbounds/addClient":
        payload = kwargs.get("json") or {}
        iid = payload.get("id")
        new_clients = json.loads(payload.get("settings", "{}")).get("clients", [])
        cur = panel.clients_of(iid)
        cur.extend(new_clients)
        panel.set_clients(iid, cur)
        return {"success": True}
    if path.startswith("/panel/api/inbounds/updateClient/"):
        payload = kwargs.get("json") or {}
        iid = payload.get("id")
        upd = json.loads(payload.get("settings", "{}")).get("clients", [{}])[0]
        cur = panel.clients_of(iid)
        for i, c in enumerate(cur):
            if c.get("email") == upd.get("email"):
                cur[i] = upd
        else:
            cur.append(upd)
        panel.set_clients(iid, cur)
        return {"success": True}
    if path.startswith("/panel/api/inbounds/") and "resetClientTraffic" in path:
        return {"success": True}
    raise AssertionError(f"unexpected path {path}")


async def main():
    xui_api.XUIAPI._request = mock_request
    api = xui_api.XUIAPI()
    api.cookie_expires = None

    # --- 1. Инбаунд с protocol='hysteria' найден, несмотря на битый ID=999
    hy = await api.get_hysteria_inbound()
    assert hy and hy.get("id") == 2, f"Hysteria-инбаунд не найден: {hy}"
    print("OK 1: инбаунд protocol='hysteria' распознан как Hysteria2 (ID=2)")

    # --- 2. Прямая ссылка hysteria2 строится для protocol='hysteria'
    link = api._build_direct_link(hy, {"password": "secret-pass"})
    assert link.startswith("hysteria2://secret-pass@") and ":443" in link, link
    print("OK 2: прямая ссылка hysteria2:// формируется:", link[:60], "...")

    # --- 3. get_user: hysteria-клиента нет -> создаётся с тем же subId
    ud = await api.get_user("tg_100")
    assert ud["success"], ud
    assert ud["subscription_url"] == "https://example.com/sub/abc123", ud["subscription_url"]
    assert ud["hysteria_link"].startswith("hysteria2://"), \
        f"hysteria_link отсутствует в подписке: {ud}"
    hy_clients = panel.clients_of(2)
    assert len(hy_clients) == 1 and hy_clients[0]["subId"] == "abc123", hy_clients
    print("OK 3: отсутствующий hysteria-клиент создан с subId из VLESS-клиента")

    # --- 4. get_user: у hysteria-клиента пустой subId -> исправляется
    hc = panel.clients_of(2)[0]
    hc["subId"] = ""
    panel.set_clients(2, [hc])
    ud2 = await api.get_user("tg_100")
    assert ud2["hysteria_link"].startswith("hysteria2://"), ud2
    hc2 = panel.clients_of(2)[0]
    assert hc2["subId"] == "abc123", f"subId не починен: {hc2}"
    print("OK 4: пустой subId hysteria-клиента синхронизирован с VLESS (подписка отдаст обе ссылки)")

    # --- 5. _ensure_hysteria_client никогда не оставляет subId пустым
    ok = await api._ensure_hysteria_client("tg_777", "pw", 0, 0, 777, "")
    assert ok
    new_c = [c for c in panel.clients_of(2) if c["email"] == "tg_777"][0]
    assert new_c["subId"], "subId остался пустым — подписка не отдаст hysteria2"
    print("OK 5: при отсутствии subId генерируется новый (подписка не ломается)")

    print("\nВсе проверки пройдены: hysteria2 присутствует в подписке.")


asyncio.run(main())
