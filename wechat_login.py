# -*- coding: utf-8 -*-
# 复刻 cli-wechat-bridge 的 iLink 登录流程：取二维码内容 -> 生成清晰 PNG -> 轮询状态 -> 存 account.json
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.parse

BASE = "https://ilinkai.weixin.qq.com/"
BOT_TYPE = "3"
DATA_DIR = os.path.join(os.path.expanduser("~"), ".cli-bridge")
CRED_FILE = os.path.join(DATA_DIR, "account.json")
# 二维码 PNG 存到本脚本同目录(自动定位, 不写死)
PNG_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "wechat-login-qr.png")

# 强制直连，绕过系统代理（直连实测 0.2s 通；微信走代理可能被带去国外）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers=headers or {})
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    # 1. 取二维码
    qr = get_json(BASE + "ilink/bot/get_bot_qrcode?bot_type=" +
                  BOT_TYPE, timeout=20)
    content = qr.get("qrcode_img_content")
    token = qr.get("qrcode")
    if not content or not token:
        print("QR_FETCH_FAIL " + json.dumps(qr)[:200], flush=True)
        return 5

    # 2. 生成清晰 PNG
    import qrcode
    img = qrcode.make(content)
    os.makedirs(os.path.dirname(PNG_PATH), exist_ok=True)
    img.save(PNG_PATH)
    print("QR_PNG_READY " + PNG_PATH, flush=True)
    print("用手机微信扫描上面这个 PNG 图片里的二维码, 然后在手机上点【确认登录】。", flush=True)

    # 3. 轮询登录状态
    deadline = time.time() + 480
    scanned = False
    while time.time() < deadline:
        try:
            st = get_json(BASE + "ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(token),
                          headers={"iLink-App-ClientVersion": "1"}, timeout=40)
        except Exception as e:
            print("POLL_RETRY " + str(e)[:120], flush=True)
            time.sleep(1.5)
            continue
        s = st.get("status")
        if s == "confirmed":
            if not st.get("ilink_bot_id") or not st.get("bot_token"):
                print("LOGIN_FAIL missing_creds", flush=True)
                return 2
            account = {
                "token": st["bot_token"],
                "baseUrl": st.get("baseurl") or BASE,
                "accountId": st["ilink_bot_id"],
                "userId": st.get("ilink_user_id"),
                "savedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(CRED_FILE, "w", encoding="utf-8") as f:
                json.dump(account, f, ensure_ascii=False, indent=2)
            # 不打印 token；只打印非敏感标识
            print("LOGIN_CONFIRMED account=%s user=%s" %
                  (account["accountId"], account["userId"]), flush=True)
            return 0
        elif s == "expired":
            print("QR_EXPIRED", flush=True)
            return 3
        elif s == "scaned" and not scanned:
            scanned = True
            print("QR_SCANNED_waiting_confirm", flush=True)
        time.sleep(1.5)
    print("TIMEOUT", flush=True)
    return 4


if __name__ == "__main__":
    sys.exit(main())
