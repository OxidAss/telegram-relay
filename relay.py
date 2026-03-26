from flask import Flask, request, jsonify
import requests
import os
import threading
import time
import atexit

app = Flask(__name__)

tg_to_mc = []
lock = threading.Lock()

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = str(os.environ.get("CHAT_ID", "")).strip()
if CHAT_ID.endswith("L"):
    CHAT_ID = CHAT_ID[:-1]
SECRET_KEY = os.environ.get("SECRET_KEY", "change_this_secret")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID is not set")

# Глобалы для защиты от дублей
poller_thread = None
poller_started = False
poller_stop = threading.Event()


def disable_webhook():
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=15
        )
        print("deleteWebhook:", r.status_code, r.text)
    except Exception as e:
        print("deleteWebhook error:", e)


def poll_telegram():
    offset = 0
    print(f"Telegram polling started (pid={os.getpid()})")
    while not poller_stop.is_set():
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "edited_message", "channel_post", "edited_channel_post"]
                },
                timeout=35
            )
            data = r.json()
            if not data.get("ok"):
                print("getUpdates not ok:", data)
                time.sleep(2)
                continue

            updates = data.get("result", [])
            if updates:
                print(f"getUpdates: {len(updates)} updates")

            for upd in updates:
                msg = upd.get("message") or upd.get("channel_post")
                if msg and "text" in msg:
                    chat_id = str(msg.get("chat", {}).get("id", "")).strip()
                    if chat_id == CHAT_ID:
                        frm = msg.get("from", {}) or {}
                        name = frm.get("username") or frm.get("first_name") or "TGUser"
                        text = msg.get("text", "")
                        with lock:
                            tg_to_mc.append({"player": name, "message": text})
                        print(f"Queued TG->MC: {name}: {text}")
                    else:
                        print(f"Skipped message from chat_id={chat_id}, expected={CHAT_ID}")

                offset = max(offset, upd["update_id"] + 1)

        except Exception as e:
            print("Poll error:", e)
            time.sleep(3)


def ensure_single_poller():
    global poller_thread, poller_started
    if poller_started:
        return
    poller_started = True
    disable_webhook()
    poller_thread = threading.Thread(target=poll_telegram, daemon=True, name="tg-poller")
    poller_thread.start()


@atexit.register
def _shutdown():
    poller_stop.set()


@app.before_request
def _boot_once():
    # Гарантирует старт в gunicorn worker после загрузки
    ensure_single_poller()


@app.route("/to-tg", methods=["POST"])
def to_tg():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "player" not in data or "message" not in data:
        return jsonify({"error": "invalid payload"}), 400

    text = f"*{data['player']}*: {data['message']}"
    try:
        rr = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=20
        )
        if rr.status_code != 200:
            return jsonify({"error": "telegram send failed", "body": rr.text}), 502
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/from-tg", methods=["GET"])
def from_tg():
    if request.headers.get("X-Secret-Key") != SECRET_KEY:
        return jsonify({"error": "unauthorized"}), 401

    with lock:
        out = tg_to_mc.copy()
        tg_to_mc.clear()
    return jsonify(out)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "pid": os.getpid()})


if __name__ == "__main__":
    ensure_single_poller()
    app.run(host="0.0.0.0", port=5000)
