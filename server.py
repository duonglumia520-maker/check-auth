from flask import Flask, request, jsonify
import json
from datetime import datetime
import os

app = Flask(__name__)

CODES_PATH = "codes.json"
USED_PATH = "used.json"
LOG_PATH = "log.txt"

# Load mã hợp lệ
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

# Ghi log xác thực
def ghi_log(user, code, status):
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{time_str}] Người dùng: {user} | Mã: {code} | Trạng thái: {status}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(log_line)

# Kiểm tra mã xác thực
@app.route('/check', methods=['POST'])
def check_code():
    data = request.get_json()
    code = data.get("verify_code")
    user = data.get("user_id")

    if code not in VALID_CODES:
        ghi_log(user, code, "❌ mã không tồn tại")
        return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 403

    if code in USED_CODES:
        if USED_CODES[code]["user"] != user:
            ghi_log(user, code, "❌ mã đã bị người khác dùng")
            return jsonify({"status": "error", "message": "Mã đã bị người khác dùng"}), 403
        else:
            ghi_log(user, code, "✅ hợp lệ (đã dùng bởi chính người đó)")
            return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

    # Lưu mã đã dùng
    USED_CODES[code] = {
        "user": user,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(USED_PATH, "w") as f:
        json.dump(USED_CODES, f, indent=2)

    ghi_log(user, code, "✅ hợp lệ (lần đầu dùng)")
    return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

# Xem log xác thực
@app.route('/log', methods=['GET'])
def get_log():
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"log": content}), 200
    except:
        return jsonify({"error": "Không có log"}), 404

# Cho phép Render chạy đúng cổng
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
