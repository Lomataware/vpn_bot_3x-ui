"""Тест: регулирование длительности подписки (MONTH_DAYS и extend_from).

Проверяет:
1. Новая выдача — срок = сейчас + days (ровно, без «32 дней»).
2. Продление активной подписки ("auto") — прибавляется к остатку.
3. Продление истекшей подписки ("auto") — отсчитывается от текущего момента.
4. "now" — полная замена срока от текущей даты даже при активном остатке.
5. Config.MONTH_DAYS читается из окружения (регулировка длины «месяца»).
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

os.environ["XUI_ENABLE_HYSTERIA"] = "false"
os.environ["XUI_SUB_URL"] = "https://example.com/sub"
sys.path.insert(0, os.path.dirname(__file__))

from config import Config  # noqa: E402
import xui_api  # noqa: E402


class MockPanel:
    def __init__(self):
        self.inbounds = {
            1: {
                "id": 1, "remark": "VLESS", "protocol": "vless", "port": 443,
                "enable": True,
                "settings": json.dumps({"clients": []}),
                "streamSettings": json.dumps({"network": "tcp", "security": "reality",
                                              "realitySettings": {"publicKey": "PUB",
                                                                  "serverNames": ["x"],
                                                                  "shortIds": ["aa"],
                                                                  "settings": {"fingerprint": "chrome"}}}),
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
        return {"success": True, "obj": panel.inbounds[int(path.rsplit("/", 1)[-1])]}
    if path == "/panel/api/inbounds/addClient":
        payload = kwargs.get("json") or {}
        cur = panel.clients_of(payload["id"])
        cur.extend(json.loads(payload["settings"]).get("clients", []))
        panel.set_clients(payload["id"], cur)
        return {"success": True}
    if path.startswith("/panel/api/inbounds/updateClient/"):
        payload = kwargs.get("json") or {}
        upd = json.loads(payload["settings"])["clients"][0]
        cur = panel.clients_of(payload["id"])
        for i, c in enumerate(cur):
            if c.get("email") == upd.get("email"):
                cur[i] = upd
        panel.set_clients(payload["id"], cur)
        return {"success": True}
    if "resetClientTraffic" in path:
        return {"success": True}
    raise AssertionError(f"unexpected path {path}")


def expire_ms_of(email):
    for c in panel.clients_of(1):
        if c.get("email") == email:
            return c.get("expiryTime", 0)
    raise AssertionError(f"client {email} not found")


async def main():
    xui_api.XUIAPI._request = mock_request
    api = xui_api.XUIAPI()
    api.cookie_expires = None
    DAY = 86400

    now = int(datetime.now().timestamp())

    # --- 1. Новая выдача на 30 дней: ровно now+30d (допускаем секунды на исполнение)
    r = await api.create_or_extend_subscription(user_id=1, days=30, existing_username="tg_a")
    assert r["success"], r
    delta = r["expire"] - now
    assert 29 * DAY < delta <= 30 * DAY, f"новая выдача должна быть ~30 дней, получили {delta/DAY:.2f}"
    print(f"OK 1: новая выдача = {delta/DAY:.2f} дн. от текущего момента")

    # --- 2. Продление активной подписки ("auto"): остаток + 30
    old_exp = expire_ms_of("tg_a") // 1000
    r2 = await api.create_or_extend_subscription(user_id=1, days=30, existing_username="tg_a")
    assert r2["success"] and r2["extended"]
    assert abs(r2["expire"] - (old_exp + 30 * DAY)) <= 5, \
        f"ожидалось {old_exp + 30*DAY}, получено {r2['expire']}"
    print("OK 2: продление активной подписки прибавляет дни к остатку")

    # --- 3. Истекшая подписка + "auto": отсчёт от текущего момента, а не от просрочки
    cl = panel.clients_of(1)[0]
    past = (now - 10 * DAY) * 1000
    cl["expiryTime"] = past
    panel.set_clients(1, [cl])
    r3 = await api.create_or_extend_subscription(user_id=1, days=30, existing_username="tg_a")
    assert r3["success"]
    assert 29 * DAY < r3["expire"] - now <= 30 * DAY, \
        f"истекшая подписка должна продлеваться от 'сейчас', дельта={(r3['expire']-now)/DAY:.2f} дн."
    print("OK 3: истекшая подписка ('auto') — 30 дней от текущего момента (не 20)")

    # --- 4. "now" — полная замена: даже с активным остатком срок = now + days
    future = (now + 25 * DAY) * 1000
    cl = panel.clients_of(1)[0]
    cl["expiryTime"] = future
    panel.set_clients(1, [cl])
    r4 = await api.create_or_extend_subscription(
        user_id=1, days=30, existing_username="tg_a", extend_from="now")
    assert r4["success"]
    assert 29 * DAY < r4["expire"] - now <= 30 * DAY, \
        f"'now' должен игнорировать остаток, дельта={(r4['expire']-now)/DAY:.2f} дн."
    print("OK 4: extend_from='now' заменяет срок на now+days независимо от остатка")

    # --- 5. MONTH_DAYS настраивается через env
    os.environ["MONTH_DAYS"] = "31"
    import importlib
    cfg = importlib.reload(sys.modules["config"])
    assert cfg.Config.MONTH_DAYS == 31
    os.environ.pop("MONTH_DAYS")
    importlib.reload(sys.modules["config"])
    from config import Config as C2
    assert C2.MONTH_DAYS == 30
    print("OK 5: MONTH_DAYS читается из .env (по умолчанию 30)")

    print("\nВсе проверки длительности пройдены.")


asyncio.run(main())
