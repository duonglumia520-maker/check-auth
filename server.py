from flask import Flask, request, jsonify
import json
import datetime
import os

app = Flask(__name__)

CODES_PATH = "codes.json"
LOG_PATH = "log.txt"
USED_PATH = "used.json"

# Load mã xác thực
try:
    with open(CODES_PATH) as f:
        VALID_CODES = json.load(f)
except:
    VALID_CODES = []

# Load mã đã dùng
try:
    with open(USED_PATH) as f:
        USED_CODES = json.load(f)
except:
    USED_CODES = {}

# Ghi log
def ghi_log(user, code, status):
    time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{time_str}] Người dùng: {user} | Mã: {code} | Trạng thái: {status}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(log_line)

# Kiểm tra mã đã dùng
def is_code_valid(code, user):
    now = datetime.datetime.now()

    if code not in USED_CODES:
        return "first_use"

    info = USED_CODES[code]
    used_by = info["user"]
    used_time = datetime.datetime.strptime(info["time"], "%Y-%m-%d %H:%M:%S")

    if user != used_by:
        return "used_by_other"
    elif (now - used_time).total_seconds() > 86400:
        return "expired"
    else:
        return "valid"

@app.route('/check', methods=['POST'])
def check_code():
    data = request.get_json()
    code = data.get("verify_code")
    user = data.get("user_id")

    if code not in VALID_CODES:
        ghi_log(user, code, "❌ mã không tồn tại")
        return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 403

    status = is_code_valid(code, user)

    if status == "used_by_other":
        ghi_log(user, code, "❌ mã đã bị người khác dùng")
        return jsonify({"status": "error", "message": "Mã đã bị người khác dùng"}), 403

    elif status == "expired":
        VALID_CODES.remove(code)
        with open(CODES_PATH, "w") as f:
            json.dump(VALID_CODES, f)

        del USED_CODES[code]
        with open(USED_PATH, "w") as f:
            json.dump(USED_CODES, f)

        ghi_log(user, code, "🗑 mã đã hết hạn và bị xóa")
        return jsonify({"status": "error", "message": "Mã đã hết hạn"}), 403

    elif status == "valid":
        ghi_log(user, code, "✅ hợp lệ (trong thời hạn)")
        return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

    else:  # first_use
        USED_CODES[code] = {
            "user": user,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(USED_PATH, "w") as f:
            json.dump(USED_CODES, f, indent=2)

        ghi_log(user, code, "✅ hợp lệ (lần đầu dùng)")
        return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

@app.route('/log', methods=['GET'])
def get_log():
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"log": content}), 200
    except:
        return jsonify({"error": "Không có log"}), 404

# ✅ Sửa phần này để Render nhận PORT
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
