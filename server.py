from flask import Flask, request, jsonify
import json
from datetime import datetime, timedelta
import os
import threading
import psycopg2 
import sys 

app = Flask(__name__)

# --- CẤU HÌNH PATH & BÍ MẬT ---
CODES_PATH = "codes.json"
# LOG_PATH đã bị loại bỏ vì Log được lưu vào DB
LOG_ACCESS_SECRET = "43991201" 

# Giới hạn số lượng Log trong Database (Lưu không quá 50 dòng)
MAX_LOG_ENTRIES = 50
# Số lượng Log hiển thị trên Web
DISPLAY_LOG_ENTRIES = 30

# Lấy Chuỗi Kết nối DB từ biến môi trường
DATABASE_URL = os.environ.get("DATABASE_URL")

# Khóa để ngăn chặn race condition khi ghi file/DB
file_lock = threading.Lock()


# --- CHỨC NĂNG DATABASE ---

def get_db_connection():
    """Tạo và trả về kết nối tới PostgreSQL."""
    if not DATABASE_URL:
        print("LỖI: Thiếu biến môi trường DATABASE_URL!")
        sys.exit(1)
    conn = psycopg2.connect(DATABASE_URL) 
    return conn

def init_db():
    """Khởi tạo các bảng cần thiết nếu chưa tồn tại (code_status và auth_logs)."""
    if not DATABASE_URL: return
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Bảng 1: code_status (Lưu trạng thái sử dụng của mã)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS code_status (
                code TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                used_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                status TEXT NOT NULL
            );
        """)

        # Bảng 2: auth_logs (Lưu nhật ký xác thực)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auth_logs (
                id SERIAL PRIMARY KEY,
                log_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                user_id TEXT NOT NULL,
                code TEXT,
                status TEXT NOT NULL
            );
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("INFO: Khởi tạo DB thành công (code_status và auth_logs).")
    except Exception as e:
        print(f"LỖI KHỞI TẠO DATABASE. Vui lòng kiểm tra lại cấu hình DB: {e}")
        sys.exit(1)

def ghi_log_db(user_id, code, status):
    """Ghi log xác thực vào bảng auth_logs trong Database và giới hạn 50 dòng."""
    conn = None
    cur = None
    
    # Sử dụng lock để đảm bảo không có hai luồng ghi/xoá log cùng lúc
    with file_lock:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            now = datetime.now()
            
            # 1. INSERT log mới
            cur.execute(
                "INSERT INTO auth_logs (log_time, user_id, code, status) VALUES (%s, %s, %s, %s)",
                (now, user_id, code, status)
            )
            
            # 2. KIỂM TRA và DỌN DẸP nếu vượt quá MAX_LOG_ENTRIES (50)
            cur.execute("SELECT COUNT(*) FROM auth_logs;")
            count = cur.fetchone()[0]
            
            if count > MAX_LOG_ENTRIES:
                # Xóa các log cũ nhất, chỉ giữ lại 50 log gần nhất
                cur.execute("""
                    DELETE FROM auth_logs
                    WHERE id NOT IN (
                        SELECT id 
                        FROM auth_logs 
                        ORDER BY log_time DESC 
                        LIMIT %s
                    );
                """, (MAX_LOG_ENTRIES,))
                
            conn.commit()
        except Exception as e:
            print(f"CẢNH BÁO: Không thể ghi log vào DB: {e}")
        finally:
            if cur: cur.close()
            if conn: conn.close()


# ⭐ Tự động gọi init_db khi ứng dụng khởi động lần đầu
with app.app_context():
    init_db()


# --- CHỨC NĂNG FILE SYSTEM ---

# Hàm ghi log cũ (ghi_log) đã được loại bỏ

def xoa_ma_khoi_codes_json(code_to_delete, current_codes_list):
    """Xóa mã khỏi codes.json sau lần kích hoạt đầu tiên (Dùng 1 Lần)."""
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
    log_status = "Lỗi chưa xác định" # Khởi tạo để ghi log cuối cùng
    
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
        cur.execute("SELECT user_id, used_at, status FROM code_status WHERE code = %s", (code,))
        db_record = cur.fetchone()
        
        # --- TRƯỜNG HỢP A: MÃ ĐÃ CÓ TRONG LỊCH SỬ (Đã kích hoạt) ---
        if db_record:
            db_user, db_time, db_status = db_record

            if db_status == 'EXPIRED':
                log_status = "❌ mã đã bị chặn vĩnh viễn"
                return jsonify({"status": "error", "message": "Mã đã hết hạn"}), 403
            
            # Mã ACTIVE: Kiểm tra người dùng
            if db_user != user:
                log_status = "❌ mã đã bị người khác dùng"
                return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 403
            
            # Kiểm tra 24 giờ
            if db_time + timedelta(hours=24) < datetime.now():                    
                # HẾT HẠN: Cập nhật trạng thái thành EXPIRED (Chặn vĩnh viễn)
                cur.execute("UPDATE code_status SET status = 'EXPIRED' WHERE code = %s", (code,))
                conn.commit()
                log_status = "❌ mã đã hết hạn 24 giờ (đã chặn vĩnh viễn)"
                return jsonify({"status": "error", "message": "Mã đã hết hạn"}), 403
            else:
                log_status = "✅ hợp lệ (còn hạn 24 giờ)"
                return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

        # --- TRƯỜNG HỢP B: MÃ CHƯA TỪNG ĐƯỢC KÍCH HOẠT (Kiểm tra codes.json) ---
        
        if code not in valid_codes_list:
            log_status = "❌ mã chưa từng kích hoạt và không tồn tại"
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
        
        log_status = "✅ hợp lệ (Lần đầu kích hoạt, đã xóa khỏi codes.json)"
        return jsonify({"status": "ok", "message": "Mã hợp lệ"}), 200

    except Exception as e:
        print(f"LỖI Server/DB: {e}")
        log_status = f"❌ Lỗi server nội bộ: {e}"
        return jsonify({"status": "error", "message": "Mã không hợp lệ"}), 500
    finally:
        # Ghi log vào Database sau khi hoàn tất xử lý
        ghi_log_db(user, code, log_status)
        # Đảm bảo đóng kết nối DB
        if cur: cur.close()
        if conn: conn.close()


# --- ENDPOINT MỚI: XEM LOG XÁC THỰC TỪ DB (/logs) ---

@app.route('/logs', methods=['GET'])
def get_db_logs():
    conn = None
    cur = None
    
    # Kiểm tra mật khẩu bí mật
    secret_key = request.args.get('secret')
    if secret_key != LOG_ACCESS_SECRET:
        return jsonify({"error": "Truy cập bị từ chối"}), 403

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Truy vấn 30 bản ghi log gần nhất (sắp xếp theo thời gian giảm dần)
        cur.execute("""
            SELECT log_time, user_id, code, status
            FROM auth_logs
            ORDER BY log_time DESC
            LIMIT %s;
        """, (DISPLAY_LOG_ENTRIES,))
        logs_raw = cur.fetchall()
        
        # Chuyển đổi kết quả sang định dạng JSON
        logs = []
        for log in logs_raw:
            logs.append({
                "time": log[0].strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": log[1],
                "code": log[2] or "N/A", # Mã có thể là NULL trong một số trường hợp log
                "status": log[3]
            })

        return jsonify({"status": "ok", "logs": logs}), 200
    
    except Exception as e:
        print(f"LỖI khi truy vấn log từ DB: {e}")
        return jsonify({"status": "error", "message": "Lỗi truy vấn log nội bộ"}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# --- ENDPOINT LOG CŨ (/log) BỊ LOẠI BỎ ---

# Cho phép Render chạy đúng cổng
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
