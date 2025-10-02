from flask import Flask, request, jsonify
import json
from datetime import datetime, timedelta # ✅ Thêm timedelta để tính toán thời gian
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
        used_info = USED_CODES[code]
        if used_info["user"] != user:
            ghi_log(user, code, "❌ mã đã bị người khác dùng")
            return jsonify({"status": "error", "message": "Mã đã bị người khác dùng"}), 403
        else:
            # ⭐ SỬA ĐỔI: Thêm logic kiểm tra 24 giờ
            try:
                used_time = datetime.strptime(used_info["time"], "%Y-%m-%d %H:%M:%S")
                # Nếu thời gian đã dùng + 24 giờ mà vẫn nhỏ hơn thời gian hiện tại -> Mã đã hết hạn
                if used_time + timedelta(hours=24) < datetime.now():
                    ghi_log(user, code, "❌ mã đã hết hạn sau 24 giờ")
                    return jsonify({"status": "error", "message": "Mã đã hết hạn"}), 403
                else:
                    ghi_log(user, code, "✅ hợp lệ (đã dùng bởi chính người đó, còn hạn)")
                    return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200
            except (ValueError, KeyError):
                # Xử lý trường hợp dữ liệu cũ không có 'time' hoặc định dạng sai
                ghi_log(user, code, "⚠️ lỗi định dạng thời gian, chấp nhận tạm thời")
                return jsonify({"status": "ok", "message": "Mã hợp lệ (lỗi định dạng thời gian)"}), 200

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
