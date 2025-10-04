from flask import Flask, request, jsonify
import json
from datetime import datetime, timedelta
import os
import threading
import psycopg2 # ✅ Thư viện cho PostgreSQL
import sys # Để xử lý lỗi khởi tạo DB

app = Flask(__name__)

# --- CẤU HÌNH PATH & BÍ MẬT ---
CODES_PATH = "codes.json"
LOG_PATH = "log.txt"
LOG_ACCESS_SECRET = "43991201" 

# Lấy Chuỗi Kết nối DB từ biến môi trường (CẦN ĐẶT TRÊN RENDER: DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL")

# Khóa để ngăn chặn race condition khi ghi file/DB
file_lock = threading.Lock()


# --- CHỨC NĂNG DATABASE ---

def get_db_connection():
    """Tạo và trả về kết nối tới PostgreSQL."""
    if not DATABASE_URL:
        # Nếu thiếu DB URL, in lỗi và thoát ứng dụng
        print("LỖI: Thiếu biến môi trường DATABASE_URL!")
        sys.exit(1)
    # Kết nối bằng chuỗi URL được cung cấp bởi Render
    conn = psycopg2.connect(DATABASE_URL) 
    return conn

def init_db():
    """Khởi tạo bảng code_status nếu chưa tồn tại."""
    if not DATABASE_URL: return
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Bảng code_status: lưu mã, user, thời điểm dùng, và trạng thái (ACTIVE/EXPIRED)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_status (
                code TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                used_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                status TEXT NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"LỖI KHỞI TẠO DATABASE. Vui lòng kiểm tra lại cấu hình DB: {e}")
        sys.exit(1)

# ⭐ Tự động gọi init_db khi ứng dụng khởi động lần đầu
with app.app_context():
    init_db()


# --- CHỨC NĂNG FILE SYSTEM ---

# Ghi log xác thực (Giữ nguyên)
def ghi_log(user, code, status):
    """Ghi log vào file log.txt."""
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{time_str}] Người dùng: {user} | Mã: {code} | Trạng thái: {status}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(log_line)

def xoa_ma_khoi_codes_json(code_to_delete, current_codes_list):
    """Xóa mã khỏi codes.json sau lần kích hoạt đầu tiên."""
    try:
        if code_to_delete in current_codes_list:
            current_codes_list.remove(code_to_delete)
            with open(CODES_PATH, "w") as f:
                json.dump(current_codes_list, f, indent=2)
            return True
        return False
    except Exception as e:
        print(f"LỖI khi xóa mã khỏi codes.json: {e}")
        return False


# --- ENDPOINT CHÍNH: /check ---

@app.route('/check', methods=['POST'])
def check_code():
    conn = None
    cur = None
    
    # Đọc codes.json (Danh sách mã CHƯA KÍCH HOẠT)
    try:
        with open(CODES_PATH) as f:
            valid_codes_list = json.load(f)
    except:
        valid_codes_list = []

    data = request.get_json()
    code = data.get("verify_code")
    user = data.get("user_id")

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # BƯỚC 1: KIỂM TRA DATABASE (Ưu tiên Lịch sử Sử dụng)
        # Tìm mã trong bảng code_status
        cur.execute("SELECT user_id, used_at, status FROM code_status WHERE code = %s", (code,))
        db_record = cur.fetchone()
        
        # --- TRƯỜNG HỢP A: MÃ ĐÃ CÓ TRONG LỊCH SỬ (Đã kích hoạt trước đây) ---
        if db_record:
            db_user, db_time, db_status = db_record

            if db_status == 'EXPIRED':
                ghi_log(user, code, "❌ mã đã bị chặn vĩnh viễn")
                return jsonify({"status": "error", "message": "Mã đã hết hạn"}), 403
            
            # Mã ACTIVE: Kiểm tra người dùng
            if db_user != user:
                ghi_log(user, code, "❌ mã đã bị người khác dùng")
                return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 403
            
            # Kiểm tra 24 giờ
            if db_time + timedelta(hours=24) < datetime.now():                    
                # HẾT HẠN: Cập nhật trạng thái thành EXPIRED (Chặn vĩnh viễn)
                cur.execute("UPDATE code_status SET status = 'EXPIRED' WHERE code = %s", (code,))
                conn.commit()
                ghi_log(user, code, "❌ mã đã hết hạn 24 giờ (đã chặn vĩnh viễn)")
                return jsonify({"status": "error", "message": "Mã đã hết hạn"}), 403
            else:
                ghi_log(user, code, "✅ hợp lệ (còn hạn 24 giờ)")
                return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

        # --- TRƯỜNG HỢP B: MÃ CHƯA TỪNG ĐƯỢC KÍCH HOẠT (Kiểm tra codes.json) ---
        
        if code not in valid_codes_list:
            ghi_log(user, code, "❌ mã chưa từng kích hoạt và không tồn tại")
            return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 403
        
        # Mã mới và hợp lệ: Kích hoạt lần đầu
        with file_lock:
            # 1. Xóa mã khỏi codes.json (DÙNG 1 LẦN)
            xoa_ma_khoi_codes_json(code, valid_codes_list) 
            
            # 2. INSERT vào Database với trạng thái ACTIVE
            now = datetime.now()
            cur.execute(
                "INSERT INTO code_status (code, user_id, used_at, status) VALUES (%s, %s, %s, 'ACTIVE')",
                (code, user, now)
            )
            conn.commit()
        
        ghi_log(user, code, "✅ hợp lệ (Lần đầu kích hoạt, đã xóa khỏi codes.json)")
        return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

    except Exception as e:
        print(f"LỖI Server/DB: {e}")
        # Ghi log lỗi server nhưng trả về thông báo chung cho người dùng
        ghi_log(user, code, f"❌ Lỗi server nội bộ: {e}")
        return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 500
    finally:
        # Đảm bảo đóng kết nối DB
        if cur: cur.close()
        if conn: conn.close()


# --- ENDPOINT LOG (Giữ nguyên) ---

@app.route('/log', methods=['GET'])
def get_log():
    # Thêm cơ chế xác thực cho endpoint /log
    secret_key = request.args.get('secret')
    if secret_key != LOG_ACCESS_SECRET:
        return jsonify({"error": "Truy cập bị từ chối"}), 403

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return app.response_class(content, mimetype='text/plain')
    except:
        return jsonify({"error": "Không có log"}), 404

# Cho phép Render chạy đúng cổng
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)        }
        with open(USED_PATH, "w") as f:
            json.dump(USED_CODES, f, indent=2)

    ghi_log(user, code, "✅ hợp lệ (lần đầu dùng)")
    return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

# Xem log xác thực
@app.route('/log', methods=['GET'])
def get_log():
    # ⭐ SỬA ĐỔI: Thêm cơ chế xác thực cho endpoint /log
    secret_key = request.args.get('secret')
    if secret_key != LOG_ACCESS_SECRET:
        return jsonify({"error": "Truy cập bị từ chối"}), 403

    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # Trả về nội dung dạng text/plain để dễ đọc trên trình duyệt
        return app.response_class(content, mimetype='text/plain')
    except:
        return jsonify({"error": "Không có log"}), 404

# Cho phép Render chạy đúng cổng
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
