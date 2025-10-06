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
# (Giữ nguyên trả về JSON)

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


# --- ENDPOINT 1: XEM LOG XÁC THỰC TỪ DB (/logs) ---

@app.route('/logs', methods=['GET'])
def get_db_logs():
    conn = None
    cur = None
    
    # Kiểm tra mật khẩu bí mật
    secret_key = request.args.get('secret')
    if secret_key != LOG_ACCESS_SECRET:
        # Trả về Plain Text lỗi
        return "Truy cập bị từ chối.", 403, {'Content-Type': 'text/plain'}

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Truy vấn 30 bản ghi log gần nhất
        cur.execute("""
            SELECT log_time, user_id, code, status
            FROM auth_logs
            ORDER BY log_time DESC
            LIMIT %s;
        """, (DISPLAY_LOG_ENTRIES,))
        logs_raw = cur.fetchall()
        
        # ⭐ TẠO CHUỖI VĂN BẢN (TXT) CHO LOG XÁC THỰC (Đã tối ưu căn chỉnh) ⭐
        log_lines = ["--- LOG XÁC THỰC GẦN ĐÂY NHẤT ---", f"Thời gian server: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
        
        # Định dạng Header
        header = f"| {'Thời Gian':<20} | {'User ID':<20} | {'Code':<25} | {'Trạng Thái':<50} |"
        separator = "-" * len(header)
        log_lines.extend([header, separator])

        # Định dạng từng dòng Log
        for log in logs_raw:
            log_time = log[0].strftime("%Y-%m-%d %H:%M:%S")
            user_id = log[1]
            code = log[2] or "N/A"
            # Thay thế ký tự xuống dòng/khoảng trắng trong status để căn chỉnh không bị lệch
            status = log[3].replace('\n', ' ').replace('\r', ' ') 
            
            line = f"| {log_time:<20} | {user_id:<20} | {code:<25} | {status:<50} |"
            log_lines.append(line)

        log_lines.append(separator)
        
        final_log_text = "\n".join(log_lines)

        # TRẢ VỀ VĂN BẢN THUẦN TÚY (Plain Text)
        return final_log_text, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    
    except Exception as e:
        print(f"LỖI khi truy vấn log từ DB: {e}")
        # Trả về lỗi dưới dạng Plain Text
        return f"LỖI NỘI BỘ: Không thể truy vấn log. Lỗi: {e}", 500, {'Content-Type': 'text/plain'}
    finally:
        if cur: cur.close()
        if conn: conn.close()


# --- ENDPOINT 2: XEM MÃ ĐANG HOẠT ĐỘNG VÀ CÒN HẠN (/active_codes) ---

def format_timedelta(td):
    """Định dạng timedelta thành chuỗi D:H:M:S."""
    total_seconds = int(td.total_seconds())
    if total_seconds <= 0:
        return "00:00:00"
    
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    
    if days > 0:
        return f"{days} ngày {hours:02d} giờ {minutes:02d} phút"
    else:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

@app.route('/active_codes', methods=['GET'])
def get_active_codes():
    conn = None
    cur = None
    
    # Kiểm tra mật khẩu bí mật
    secret_key = request.args.get('secret')
    if secret_key != LOG_ACCESS_SECRET:
        # Trả về Plain Text lỗi
        return "Truy cập bị từ chối.", 403, {'Content-Type': 'text/plain'}

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        now = datetime.now()
        
        # Chỉ truy vấn các mã đang ACTIVE
        cur.execute("""
            SELECT code, user_id, used_at
            FROM code_status
            WHERE status = 'ACTIVE'
            ORDER BY used_at DESC;
        """)
        active_raw = cur.fetchall()
        
        # ⭐ TẠO CHUỖI VĂN BẢN (TXT) CHO MÃ HOẠT ĐỘNG (Đã tối ưu căn chỉnh) ⭐
        active_count = 0
        active_lines = []
        
        for record in active_raw:
            code, user_id, used_at = record
            
            expiry_time = used_at + timedelta(hours=24)
            time_remaining = expiry_time - now
            
            # Chỉ thêm vào danh sách nếu mã thực sự còn hạn 24 giờ
            if time_remaining > timedelta(0):
                active_count += 1
                
                # Định dạng log
                activated_at = used_at.strftime("%Y-%m-%d %H:%M:%S")
                time_left = format_timedelta(time_remaining)
                
                # Kích thước cột tối ưu hơn cho hiển thị TXT
                line = f"| {code:<25} | {user_id:<20} | {activated_at:<20} | {time_left:<25} |"
                active_lines.append(line)

        
        log_lines = ["--- DANH SÁCH MÃ ĐANG HOẠT ĐỘNG (ACTIVE) ---", 
                     f"Tổng cộng: {active_count} mã còn hạn.", ""]
        
        # Định dạng Header
        header = f"| {'Code':<25} | {'User ID Kích Hoạt':<20} | {'Kích Hoạt Lúc':<20} | {'Thời Gian Còn Lại':<25} |"
        separator = "-" * len(header)
        log_lines.extend([header, separator])
        log_lines.extend(active_lines)
        log_lines.append(separator)
        
        final_log_text = "\n".join(log_lines)
        
        # TRẢ VỀ VĂN BẢN THUẦN TÚY (Plain Text)
        return final_log_text, 200, {'Content-Type': 'text/plain; charset=utf-8'}
    
    except Exception as e:
        print(f"LỖI khi truy vấn mã ACTIVE từ DB: {e}")
        # Trả về lỗi dưới dạng Plain Text
        return f"LỖỖI NỘI BỘ: Không thể truy vấn mã ACTIVE. Lỗi: {e}", 500, {'Content-Type': 'text/plain'}
    finally:
        if cur: cur.close()
        if conn: conn.close()


# Cho phép Render chạy đúng cổng
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
