import os
import sys
import time
import sqlite3
import threading
import subprocess
from datetime import datetime
import requests  
import gpiod
from gpiod.line import Direction, Value
from smbus2 import SMBus
from flask import Flask, jsonify, render_template_string, request  # Flask 백엔드 모듈 임포트

# ==========================================
# 1. 시스템 상태 기계 (State Machine) 상태 정의
# ==========================================
STATE_IDLE = "IDLE"              
STATE_SCANNING = "SCANNING"      
STATE_RESULT_SUCCESS = "SUCCESS"  
STATE_RESULT_ERROR = "ERROR"      

# ==========================================
# 2. 시스템 하드웨어 및 서버 API 설정
# ==========================================
CHIP_PATH = '/dev/gpiochip4'  

# 센서 핀 설정
TRIG_PIN = 23  
ECHO_PIN = 24  
DHT_PIN = 17   

# 상태 표시 LED 핀 설정 (상태등)
LED_PIN_R = 5
LED_PIN_Y = 12
LED_PIN_G = 6

# 3색 RGB LED 핀 설정 (조명용)
RGB_PIN_R = 16
RGB_PIN_G = 20
RGB_PIN_B = 21

# 서보모터 핀 설정 (분리배출 판 기울임용)
SERVO_PIN = 18

# 입구 개폐용 서보모터 핀 설정
ENTRY_SERVO_PIN = 19

# I2C LCD 설정
LCD_ADDRESS = 0x27
I2C_BUS = 1

# 파일 경로 및 서버 통신 설정
SAVE_DIR = "/opt/Desktop"
if not os.path.exists(SAVE_DIR):
    SAVE_DIR = os.getcwd()  

TEMP_IMAGE_PATH = os.path.join(SAVE_DIR, "captured_waste.jpg")
API_URL = "http://localhost:8000/predict"
CONFIDENCE_THRESHOLD = 0.5  
SERVO_DEBUG = True

# 데이터베이스 파일 설정 (4단계)
DB_PATH = "smart_recycling.db"

def debug_print(tag, message):
    if SERVO_DEBUG:
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}][{tag}] {message}", flush=True)

# ==========================================
# 서버 콘솔 로그 버퍼 (웹 대시보드 터미널 카드용)
# ==========================================
_console_lock = threading.Lock()
_console_lines = []      # 최근 콘솔 라인 버퍼
_console_base = 0        # _console_lines[0]의 전역 스트림 인덱스
_CONSOLE_MAX = 400       # 메모리 상한 (초과 시 앞에서 잘라냄)

def push_console(text):
    """웹 대시보드 터미널 카드(/api/console_log)에 표시할 서버 로그 한 줄을 적재"""
    global _console_base
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
    with _console_lock:
        _console_lines.append(line)
        if len(_console_lines) > _CONSOLE_MAX:
            drop = len(_console_lines) - _CONSOLE_MAX
            del _console_lines[:drop]
            _console_base += drop

# ==========================================
# 3. SQLite3 데이터베이스 및 테이블 초기화 (4단계)
# ==========================================
def init_db():
    """파일 기반 로컬 SQLite3 데이터베이스 생성 및 스키마 초기화"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 3-1. 환경 센싱 및 위생 상태 분석 데이터 로그 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS env_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            temperature REAL,
            humidity REAL,
            discomfort_index REAL,
            hygiene_status TEXT
        )
    ''')
    
    # 3-2. 쓰레기 AIoT 배출 분류 이력 로그 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS waste_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            waste_type TEXT,
            servo_angle INTEGER,
            confidence REAL
        )
    ''')
    # 기존 DB 호환: confidence 컬럼이 없으면 추가
    try:
        cursor.execute("ALTER TABLE waste_logs ADD COLUMN confidence REAL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    debug_print("DATABASE", "SQLite3 파일 데이터베이스 스키마 초기화 완료")

# DB 초기화 실행
init_db()

# ==========================================
# 4. I2C 16x2 LCD 드라이버 클래스
# ==========================================
class I2CLCD:
    def __init__(self, address=0x27, bus_num=1):
        self.address = address
        try:
            self.bus = SMBus(bus_num)
        except Exception as e:
            print(f"I2C 버스를 열 수 없습니다. LCD가 작동하지 않습니다. ({e})")
            self.bus = None
            return
            
        self.LCD_CHR = 1  
        self.LCD_CMD = 0  
        self.LCD_LINE_1 = 0x80  
        self.LCD_LINE_2 = 0xC0  
        self.LCD_BACKLIGHT = 0x08  
        self.ENABLE = 0b00000100  
        
        self.line1_text = ""
        self.line2_text = ""
        self.line1_scroll = False
        self.line2_scroll = False
        self.line1_idx = 0
        self.line2_idx = 0
        self.last_scroll_time = 0.0
        
        self.lcd_write(0x33, self.LCD_CMD)
        self.lcd_write(0x32, self.LCD_CMD)
        self.lcd_write(0x06, self.LCD_CMD)
        self.lcd_write(0x0C, self.LCD_CMD)
        self.lcd_write(0x28, self.LCD_CMD)
        self.lcd_write(0x01, self.LCD_CMD)
        time.sleep(0.005)

    def write_word(self, data):
        if self.bus:
            temp = data | self.LCD_BACKLIGHT
            self.bus.write_byte(self.address, temp)

    def send_pulse(self, data):
        self.write_word(data | self.ENABLE)
        time.sleep(0.0005)
        self.write_word(data & ~self.ENABLE)
        time.sleep(0.0001)

    def lcd_write(self, val, mode):
        high = mode | (val & 0xF0)
        self.write_word(high)
        self.send_pulse(high)
        low = mode | ((val << 4) & 0xF0)
        self.write_word(low)
        self.send_pulse(low)

    def set_message(self, text, line):
        if len(text) > 16:
            scroll_text = text + "    "  
            scroll = True
        else:
            scroll_text = text.ljust(16, " ")
            scroll = False

        if line == self.LCD_LINE_1:
            self.line1_text = scroll_text
            self.line1_scroll = scroll
            self.line1_idx = 0
            if not scroll:
                self.display_text_direct(scroll_text, line)
        elif line == self.LCD_LINE_2:
            self.line2_text = scroll_text
            self.line2_scroll = scroll
            self.line2_idx = 0
            if not scroll:
                self.display_text_direct(scroll_text, line)

    def display_text_direct(self, text, line):
        if not self.bus:
            return
        self.lcd_write(line, self.LCD_CMD)
        for char in text[:16]:
            self.lcd_write(ord(char), self.LCD_CHR)

    def update_scroll(self):
        curr = time.time()
        if curr - self.last_scroll_time < 0.35:
            return
        self.last_scroll_time = curr

        if self.line1_scroll:
            t = self.line1_text
            idx = self.line1_idx
            slice_text = (t[idx:] + t[:idx])[:16]
            self.display_text_direct(slice_text, self.LCD_LINE_1)
            self.line1_idx = (idx + 1) % len(t)

        if self.line2_scroll:
            t = self.line2_text
            idx = self.line2_idx
            slice_text = (t[idx:] + t[:idx])[:16]
            self.display_text_direct(slice_text, self.LCD_LINE_2)
            self.line2_idx = (idx + 1) % len(t)

    def clear(self):
        self.line1_text = ""
        self.line2_text = ""
        self.line1_scroll = False
        self.line2_scroll = False
        self.lcd_write(0x01, self.LCD_CMD)
        time.sleep(0.005)

# ==========================================
# 4. LED 상태 컨트롤러 클래스
# ==========================================
class LEDController:
    def __init__(self, chip_path, pin_r, pin_y, pin_g, rgb_r, rgb_g, rgb_b):
        self.pin_r = pin_r
        self.pin_y = pin_y
        self.pin_g = pin_g
        self.rgb_r = rgb_r
        self.rgb_g = rgb_g
        self.rgb_b = rgb_b
        
        self.req = gpiod.request_lines(
            chip_path,
            consumer="Status_And_RGB_LEDs",
            config={
                self.pin_r: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                self.pin_y: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                self.pin_g: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                self.rgb_r: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                self.rgb_g: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                self.rgb_b: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
            }
        )
        self.state = STATE_IDLE
        self.last_toggle_time = time.time()
        self.yellow_on = False
        
    def set_state(self, state):
        self.state = state
        if state == STATE_IDLE:
            self.req.set_values({self.pin_r: Value.INACTIVE, self.pin_y: Value.ACTIVE, self.pin_g: Value.INACTIVE})
            self.req.set_values({self.rgb_r: Value.ACTIVE, self.rgb_g: Value.ACTIVE, self.rgb_b: Value.ACTIVE})
            self.yellow_on = True
            self.last_toggle_time = time.time()
        elif state == STATE_SCANNING:
            self.req.set_values({self.pin_r: Value.INACTIVE, self.pin_y: Value.ACTIVE, self.pin_g: Value.INACTIVE})
            self.yellow_on = True
            self.last_toggle_time = time.time()
        elif state == STATE_RESULT_SUCCESS:
            self.req.set_values({self.pin_r: Value.INACTIVE, self.pin_y: Value.INACTIVE, self.pin_g: Value.ACTIVE})
            self.turn_off_rgb()
        elif state == STATE_RESULT_ERROR:
            self.req.set_values({self.pin_r: Value.ACTIVE, self.pin_y: Value.INACTIVE, self.pin_g: Value.INACTIVE})
            self.turn_off_rgb()

    def turn_off_rgb(self):
        self.req.set_values({self.rgb_r: Value.INACTIVE, self.rgb_g: Value.INACTIVE, self.rgb_b: Value.INACTIVE})

    def update(self):
        if self.state in [STATE_IDLE, STATE_SCANNING]:
            if time.time() - self.last_toggle_time >= 0.25:
                self.yellow_on = not self.yellow_on
                val = Value.ACTIVE if self.yellow_on else Value.INACTIVE
                self.req.set_value(self.pin_y, val)
                self.last_toggle_time = time.time()

    def cleanup(self):
        self.req.set_values({
            self.pin_r: Value.INACTIVE, self.pin_y: Value.INACTIVE, self.pin_g: Value.INACTIVE,
            self.rgb_r: Value.INACTIVE, self.rgb_g: Value.INACTIVE, self.rgb_b: Value.INACTIVE
        })
        self.req.release()

# ==========================================
# 5. 서보 모터 컨트롤러 클래스
# ==========================================
class ServoController:
    def __init__(self, chip_path, pin):
        self.pin = pin
        debug_print("SERVO", f"초기화 시작: chip_path={chip_path}, pin={pin}")
        try:
            self.req = gpiod.request_lines(
                chip_path,
                consumer="Servo",
                config={self.pin: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)}
            )
            debug_print("SERVO", "GPIO 라인 요청 성공")
        except Exception as e:
            debug_print("SERVO", f"GPIO 라인 요청 실패: {e}")
            raise
        
    def set_angle(self, angle, duration=1.0):
        debug_print("SERVO", f"set_angle 호출: angle={angle}, duration={duration}")

        if angle < 0:
            debug_print("SERVO", f"angle 값이 낮아 0도로 보정: {angle}")
            angle = 0
        elif angle > 180:
            debug_print("SERVO", f"angle 값이 높아 180도로 보정: {angle}")
            angle = 180

        # SG90 및 일반 서보모터 스펙 (0도: 0.5ms, 180도: 2.5ms) 적용
        pulse_width = 0.0005 + (angle / 180.0) * 0.002
        period = 0.020  # 50Hz
        low_time = period - pulse_width

        debug_print(
            "SERVO",
            f"PWM 계산 완료: pulse_width={pulse_width:.6f}s, low_time={low_time:.6f}s, period={period:.6f}s"
        )

        if low_time <= 0:
            debug_print("SERVO", f"비정상 PWM 계산: low_time={low_time:.6f}s, angle={angle}")
            return
        
        # duration 동안 소프트웨어 PWM 루프 실행 (판을 움직일 시간을 줌)
        end_time = time.time() + duration
        loop_count = 0
        while time.time() < end_time:
            loop_count += 1
            self.req.set_value(self.pin, Value.ACTIVE)
            time.sleep(pulse_width)
            self.req.set_value(self.pin, Value.INACTIVE)
            time.sleep(low_time)

        debug_print("SERVO", f"set_angle 종료: angle={angle}, duration={duration}, loop_count={loop_count}")

    def cleanup(self):
        debug_print("SERVO", "cleanup 시작")
        self.req.set_value(self.pin, Value.INACTIVE)
        self.req.release()
        debug_print("SERVO", "cleanup 완료")

# ==========================================
# 6. 온습도 센서 (DHT11) 함수 및 4단계 알고리즘 적재
# ==========================================
def read_dht11_detailed():
    timestamps = []
    values = []
    try:
        with gpiod.request_lines(
            CHIP_PATH,
            consumer="DHT11",
            config={DHT_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)}
        ) as lines:
            lines.set_value(DHT_PIN, Value.INACTIVE)
            time.sleep(0.018)
            lines.set_value(DHT_PIN, Value.ACTIVE)
            time.sleep(0.00004)
            lines.reconfigure_lines(config={DHT_PIN: gpiod.LineSettings(direction=Direction.INPUT)})
            
            get_val = lines.get_value
            pin = DHT_PIN
            last_val = get_val(pin).value
            timestamps.append(time.perf_counter_ns())
            values.append(last_val)
            
            timeout_ns = time.perf_counter_ns() + 100000000  
            while time.perf_counter_ns() < timeout_ns:
                curr_val = get_val(pin).value
                if curr_val != last_val:
                    timestamps.append(time.perf_counter_ns())
                    values.append(curr_val)
                    last_val = curr_val
                    if len(values) > 100: break
    except Exception as e:
        return None, None, f"GPIO 접근 실패 ({str(e)})"
                
    if len(values) < 10: return None, None, "센서 무반응"
        
    high_durations = []
    for i in range(len(values) - 1):
        if values[i] == 1:
            high_durations.append((timestamps[i+1] - timestamps[i]) / 1000.0)
            
    if len(high_durations) < 40: return None, None, "데이터 부족"
        
    bit_signals = high_durations[-40:]
    avg_len = sum(bit_signals) / 40.0
    
    data_bytes = [0, 0, 0, 0, 0]
    for i in range(40):
        byte_idx = i // 8
        data_bytes[byte_idx] <<= 1
        if bit_signals[i] > avg_len: data_bytes[byte_idx] |= 1
            
    checksum = (data_bytes[0] + data_bytes[1] + data_bytes[2] + data_bytes[3]) & 0xFF
    if data_bytes[4] == checksum:
        humidity = data_bytes[0] + (data_bytes[1] * 0.1)
        temperature = data_bytes[2] + (data_bytes[3] * 0.1)
        if humidity > 100.0 or temperature > 80.0: return None, None, "오독"
        return round(temperature, 1), round(humidity, 1), "SUCCESS"
    else: return None, None, "체크섬 불일치"


# ==========================================
# 4단계: 위생 상태 예측 알고리즘 & DB 로깅 구현
# ==========================================
def calculate_hygiene_and_log(temperature, humidity):
    """
    기상청 불쾌지수 공식 및 습도 임계치를 결합한 위생 위험 예측 모델
    산출된 로그를 SQLite3 env_logs 테이블에 주기적 적재합니다.
    """
    # 불쾌지수(DI) 계산식
    discomfort_index = 0.81 * temperature + 0.01 * humidity * (0.99 * temperature - 14.3) + 46.3
    discomfort_index = round(discomfort_index, 1)
    
    # 위생 지수 위험 등급 판독
    if discomfort_index >= 75 or temperature >= 28.0 or humidity >= 65.0:
        hygiene_status = "Warning"  # 위생 위험 경보
    elif discomfort_index >= 68:
        hygiene_status = "Caution"  # 위생 악취 주의
    else:
        hygiene_status = "Normal"   # 정상 쾌적 상태
        
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO env_logs (temperature, humidity, discomfort_index, hygiene_status)
            VALUES (?, ?, ?, ?)
        ''', (temperature, humidity, discomfort_index, hygiene_status))
        conn.commit()
        conn.close()
        debug_print("LOG_DB", f"위생 적재 완료: {temperature}°C, {humidity}%, DI={discomfort_index}, 상태={hygiene_status}")
    except Exception as e:
        debug_print("DB_ERROR", f"적재 실패: {e}")

    return discomfort_index, hygiene_status


def log_waste_event(waste_type, servo_angle, confidence=None):
    """AIoT 배출 성공 시 쓰레기 이벤트 내역을 waste_logs 테이블에 적재합니다."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO waste_logs (waste_type, servo_angle, confidence)
            VALUES (?, ?, ?)
        ''', (waste_type, servo_angle, confidence))
        conn.commit()
        conn.close()
        debug_print("LOG_DB", f"쓰레기 배출 로그 적재 완료: {waste_type} (서보 각도: {servo_angle}도)")
    except Exception as e:
        debug_print("DB_ERROR", f"쓰레기 적재 실패: {e}")

# ==========================================
# 7. 초음파 센서 (HC-SR04) 함수
# ==========================================
def get_ultrasonic_distance():
    try:
        with gpiod.request_lines(
            CHIP_PATH,
            consumer="Ultrasonic",
            config={
                TRIG_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                ECHO_PIN: gpiod.LineSettings(direction=Direction.INPUT)
            }
        ) as lines:
            lines.set_value(TRIG_PIN, Value.INACTIVE)
            time.sleep(0.05)  
            lines.set_value(TRIG_PIN, Value.ACTIVE)
            time.sleep(0.00001)
            lines.set_value(TRIG_PIN, Value.INACTIVE)
            
            start_time = time.time()
            timeout = start_time + 1.0  
            while lines.get_value(ECHO_PIN) == Value.INACTIVE:
                start_time = time.time()
                if start_time > timeout: return -1.0
                    
            stop_time = time.time()
            timeout = stop_time + 1.0
            while lines.get_value(ECHO_PIN) == Value.ACTIVE:
                stop_time = time.time()
                if stop_time > timeout: return -1.0
                    
            return round((stop_time - start_time) * 17150, 1)
    except Exception as e:
        print(f"초음파 제어 에러: {e}")
        return -1.0

# ==========================================
# 8. 카메라 제어 
# ==========================================
def capture_single_frame(output_path):
    print("[CAM_DEBUG] 시스템 기본 카메라 툴을 사용하여 캡처를 시도합니다...")
    commands = [
        ["rpicam-still", "-t", "500", "--immediate", "--width", "640", "--height", "480", "--rotation", "180", "-o", output_path],
        ["libcamera-still", "-t", "500", "--immediate", "--width", "640", "--height", "480", "--rotation", "180", "-o", output_path]
    ]
    for cmd in commands:
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                print(f"[CAM_DEBUG] [SUCCESS] 이미지 캡처 완료: {output_path}")
                return True
        except Exception as e:
            pass
    print("[CAM_DEBUG] [FATAL] 카메라 캡처에 실패했습니다.")
    return False


# ==========================================
# 5단계: Flask 로컬 웹 서버 대시보드 및 REST API 구축
# ==========================================
flask_app = Flask(__name__)

# REST API 엔드포인트: 실시간 스마트 관제 데이터 제공
@flask_app.route('/api/status')
def get_status():
    """SQLite3의 최신 데이터(온습도, 적재량, 최근 배출 이력 5개, 누적 통계)를 JSON으로 리턴"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 1. 최신 환경 및 위생 데이터 조회
        cursor.execute("SELECT timestamp, temperature, humidity, discomfort_index, hygiene_status FROM env_logs ORDER BY id DESC LIMIT 1")
        env_row = cursor.fetchone()
        
        # 2. 최근 배출 이력 조회 (신뢰도 포함)
        cursor.execute("SELECT timestamp, waste_type, servo_angle, confidence FROM waste_logs ORDER BY id DESC LIMIT 12")
        waste_rows = cursor.fetchall()
        
        # 3. 종류별 누적 배출 통계 카운트 계산
        cursor.execute("SELECT waste_type, COUNT(*) FROM waste_logs GROUP BY waste_type")
        stat_rows = cursor.fetchall()
        
        # 4. 전체 분리수거 처리 횟수 조회
        cursor.execute("SELECT COUNT(*) FROM waste_logs")
        total_recycling_count = cursor.fetchone()[0]
        
        conn.close()
        
        # 불쾌지수(DI)를 0~100 위생 위험 지수로 선형 변환 (DI 60→0, 85→100, clamp)
        di = env_row[3] if env_row else 0.0
        risk = 0
        if env_row and di:
            risk = max(0, min(100, int(round((di - 60.0) / 25.0 * 100))))

        # JSON 포맷 바인딩
        current_env = {
            "time": env_row[0].split()[-1] if env_row else "N/A",
            "temperature": env_row[1] if env_row else 0.0,
            "humidity": env_row[2] if env_row else 0.0,
            "risk": risk,
            "discomfort_index": di,
            "hygiene_status": env_row[4] if env_row else "Unknown"
        }

        recent_wastes = []
        for r in waste_rows:
            # 타임스탬프에서 시:분:초만 분리
            t_str = r[0].split()[-1] if ' ' in r[0] else r[0]
            # confidence(0~1)는 퍼센트로 변환, 없으면 None (프론트에서 angle fallback)
            conf_pct = round(r[3] * 100, 1) if r[3] is not None else None
            recent_wastes.append({
                "time": t_str,
                "type": r[1],
                "angle": r[2],
                "confidence": conf_pct
            })

        # 종류별 원시 누적 통계 (프론트의 classifyType()이 4개 버킷으로 폴딩)
        stats = {}
        for s in stat_rows:
            stats[s[0]] = s[1]

        return jsonify({
            "env": current_env,
            "recent": recent_wastes,
            "stats": stats,
            "total_count": total_recycling_count
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# REST API 엔드포인트: 서버 터미널 로그 증분 스트림 (대시보드 터미널 카드용)
@flask_app.route('/api/console_log')
def get_console_log():
    """since 인덱스 이후의 새 콘솔 라인만 반환. 응답: {lines, next_index}"""
    try:
        since = int(request.args.get('since', 0))
    except (TypeError, ValueError):
        since = 0
    with _console_lock:
        next_index = _console_base + len(_console_lines)
        start = since - _console_base
        if start < 0:
            start = 0
        lines = _console_lines[start:]
    return jsonify({"lines": lines, "next_index": next_index})


# 단일 웹 대시보드 렌더링용 HTML 소스 템플릿 (Linear 다크 테마)
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AIoT 스마트 분리수거 모니터</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        *{box-sizing:border-box}
        html,body{margin:0;padding:0;background:#010102}
        ::-webkit-scrollbar{width:8px;height:8px}
        ::-webkit-scrollbar-thumb{background:#23252a;border-radius:9999px}
        ::-webkit-scrollbar-track{background:transparent}
        @keyframes livepulse{0%,100%{opacity:1}50%{opacity:0.35}}
        .root{min-height:100vh;background:#010102;font-family:'Inter',-apple-system,system-ui,sans-serif;color:#f7f8f8;padding:26px 32px 40px}
        .wrap{max-width:1320px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
        .card{background:#0c0d10;border:1px solid #1d1f24;border-radius:12px;box-shadow:inset 0 1px 0 rgba(255,255,255,0.045)}
        .clabel{font-size:11px;font-weight:600;letter-spacing:0.6px;text-transform:uppercase;color:#8a8f98}
        .chip{font-size:10px;font-weight:500;letter-spacing:0.3px;color:#8a8f98;border:1px solid #23252a;border-radius:9999px;padding:2px 8px;font-family:'JetBrains Mono',monospace}
        .metric{font-size:40px;font-weight:600;letter-spacing:-1.5px;line-height:1;color:#f7f8f8}
        .unit{font-size:16px;font-weight:500;color:#8a8f98}
        .head-row{display:flex;align-items:center;justify-content:space-between}
        .row4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
        .row3{display:grid;grid-template-columns:0.82fr 1.3fr 1.3fr;gap:16px;align-items:stretch}
        .mono{font-family:'JetBrains Mono',monospace}
        @media (max-width:1080px){.row4{grid-template-columns:repeat(2,1fr)}.row3{grid-template-columns:1fr}}
    </style>
</head>
<body>
<div class="root">
  <div class="wrap">

    <header class="head-row" style="padding-bottom:2px">
      <div style="display:flex;align-items:center;gap:12px">
        <div style="width:30px;height:30px;border-radius:8px;background:#5e6ad2;display:flex;align-items:center;justify-content:center;box-shadow:inset 0 1px 0 rgba(255,255,255,0.22)">
          <div style="width:13px;height:13px;border:2px solid #fff;border-radius:4px;transform:rotate(45deg)"></div>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px">
          <span style="font-size:16px;font-weight:600;letter-spacing:-0.4px;color:#f7f8f8">AIoT 스마트 분리수거 모니터</span>
          <span style="font-size:12px;color:#8a8f98;letter-spacing:-0.1px">실시간 위생 · 배출 대시보드</span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:14px">
        <div style="display:flex;align-items:center;gap:7px;background:#0c0d10;border:1px solid #1d1f24;border-radius:9999px;padding:6px 12px">
          <span id="conn-dot" style="width:8px;height:8px;border-radius:9999px;background:#62666d"></span>
          <span id="conn-text" style="color:#8a8f98;font-size:12px;font-weight:500">연결 끊김</span>
        </div>
        <span id="clock" class="mono" style="font-size:13px;color:#8a8f98;min-width:80px;text-align:right">--:--:--</span>
      </div>
    </header>

    <div class="row4">

      <div class="card" style="padding:18px 18px 14px;display:flex;flex-direction:column;gap:10px;min-height:198px">
        <div class="head-row"><span class="clabel">온도</span><span class="chip">최근 10회</span></div>
        <div style="display:flex;align-items:baseline;gap:4px">
          <span class="metric" id="temp-val">--</span><span class="unit">°C</span>
        </div>
        <div style="position:relative;height:74px;margin-top:auto"><canvas id="temp-chart"></canvas></div>
      </div>

      <div class="card" style="padding:18px 18px 14px;display:flex;flex-direction:column;gap:10px;min-height:198px">
        <div class="head-row"><span class="clabel">습도</span><span class="chip">최근 10회</span></div>
        <div style="display:flex;align-items:baseline;gap:4px">
          <span class="metric" id="hum-val">--</span><span class="unit">%</span>
        </div>
        <div style="position:relative;height:74px;margin-top:auto"><canvas id="hum-chart"></canvas></div>
      </div>

      <div class="card" style="padding:18px;display:flex;flex-direction:column;gap:12px;min-height:198px">
        <div class="head-row"><span class="clabel">위생 위험 지수</span><span class="chip">0–100</span></div>
        <div style="display:flex;align-items:baseline;gap:5px">
          <span class="metric" id="risk-val">--</span><span style="font-size:15px;font-weight:500;color:#8a8f98">/ 100</span>
        </div>
        <div style="margin-top:auto;display:flex;flex-direction:column;gap:10px">
          <div style="height:8px;border-radius:9999px;background:#1c1d20;overflow:hidden">
            <div id="risk-bar" style="width:0%;height:100%;background:#27a644;border-radius:9999px;transition:width .4s ease,background .4s ease"></div>
          </div>
          <div style="display:flex;align-items:center;gap:7px">
            <span id="risk-dot" style="width:8px;height:8px;border-radius:9999px;background:#27a644"></span>
            <span id="risk-status" style="color:#27a644;font-size:13px;font-weight:600">--</span>
            <span id="risk-hint" class="mono" style="font-size:12px;color:#62666d;margin-left:auto">--</span>
          </div>
        </div>
      </div>

      <div class="card" style="padding:18px;display:flex;flex-direction:column;gap:12px;min-height:198px">
        <div class="head-row"><span class="clabel">위생 상태 등급</span><span class="chip" id="grade-chip">청결도 --</span></div>
        <div style="display:flex;align-items:baseline;gap:8px">
          <span id="grade-val" style="font-size:40px;font-weight:700;letter-spacing:-1px;line-height:1;color:#27a644">-</span>
          <span id="grade-status" style="font-size:15px;font-weight:600;color:#27a644">--</span>
        </div>
        <div style="margin-top:auto;display:flex;flex-direction:column;gap:8px">
          <div style="height:8px;border-radius:9999px;background:#1c1d20;overflow:hidden">
            <div id="grade-bar" style="width:0%;height:100%;background:#27a644;border-radius:9999px;transition:width .4s ease,background .4s ease"></div>
          </div>
          <span class="mono" style="font-size:12px;color:#62666d">청결도 점수 · 100점 만점</span>
        </div>
      </div>

    </div>

    <div class="row3">

      <div class="card" style="padding:18px;display:flex;flex-direction:column;gap:12px;height:432px">
        <div style="display:flex;align-items:flex-start;justify-content:space-between">
          <div style="display:flex;flex-direction:column;gap:3px">
            <span class="clabel">실시간 배출 로그</span>
            <span style="font-size:12px;color:#62666d">최근 분류 결과</span>
          </div>
          <span class="mono" style="display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:#27a644">
            <span style="width:7px;height:7px;border-radius:9999px;background:#27a644;animation:livepulse 1.6s ease-in-out infinite"></span>LIVE
          </span>
        </div>
        <div id="log-list" style="flex:1;overflow-y:auto;margin:0 -4px;padding:0 4px"></div>
      </div>

      <div class="card" style="padding:18px;display:flex;flex-direction:column;gap:14px;height:432px">
        <div style="display:flex;align-items:flex-start;justify-content:space-between">
          <div style="display:flex;flex-direction:column;gap:3px">
            <span class="clabel">누적 배출 히스토그램</span>
            <span style="font-size:12px;color:#62666d">분류별 누적 처리량</span>
          </div>
          <span id="total-badge" class="mono" style="font-size:12px;font-weight:600;color:#d0d6e0;background:#141517;border:1px solid #23252a;border-radius:9999px;padding:4px 12px">총 0건</span>
        </div>
        <div style="position:relative;flex:1;min-height:0"><canvas id="bar-chart"></canvas></div>
        <div style="display:flex;flex-wrap:wrap;gap:14px 18px">
          <div style="display:flex;align-items:center;gap:6px"><span style="width:9px;height:9px;border-radius:3px;background:#4ea7fc"></span><span style="font-size:12px;color:#8a8f98">🥤 플라스틱</span></div>
          <div style="display:flex;align-items:center;gap:6px"><span style="width:9px;height:9px;border-radius:3px;background:#e6a23c"></span><span style="font-size:12px;color:#8a8f98">🥫 캔</span></div>
          <div style="display:flex;align-items:center;gap:6px"><span style="width:9px;height:9px;border-radius:3px;background:#27a644"></span><span style="font-size:12px;color:#8a8f98">📦 종이</span></div>
          <div style="display:flex;align-items:center;gap:6px"><span style="width:9px;height:9px;border-radius:3px;background:#8a8f98"></span><span style="font-size:12px;color:#8a8f98">🗑️ 일반쓰레기</span></div>
        </div>
      </div>

      <div class="card" style="display:flex;flex-direction:column;height:432px;overflow:hidden">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border-bottom:1px solid #1d1f24;background:#141517">
          <div style="display:flex;align-items:center;gap:10px">
            <div style="display:flex;gap:7px">
              <span style="width:11px;height:11px;border-radius:9999px;background:#ed6a5e"></span>
              <span style="width:11px;height:11px;border-radius:9999px;background:#f4bf4f"></span>
              <span style="width:11px;height:11px;border-radius:9999px;background:#61c554"></span>
            </div>
            <span class="mono" style="font-size:12px;color:#8a8f98">server_terminal — bash</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <span id="term-dot" style="width:8px;height:8px;border-radius:9999px;background:#62666d"></span>
            <span id="term-conn" class="mono" style="font-size:11px;color:#8a8f98">연결 끊김</span>
          </div>
        </div>
        <div id="console-body" style="flex:1;overflow-y:auto;background:#060708;padding:12px 14px;font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.55"></div>
      </div>

    </div>

  </div>
</div>

<script>
  // 4개 분류 표시 메타 (디자인 핸드오프 사양)
  var CAT = {
    plastic: { emoji: '🥤', label: '플라스틱',   color: '#4ea7fc' },
    can:     { emoji: '🥫', label: '캔',         color: '#e6a23c' },
    paper:   { emoji: '📦', label: '종이',       color: '#27a644' },
    general: { emoji: '🗑️', label: '일반쓰레기', color: '#8a8f98' }
  };

  // 서버 응답 log.type 원시값을 4가지 분류 키로 매핑 (핸드오프 규칙과 동일)
  function classifyType(raw) {
    var r = String(raw == null ? '' : raw).toLowerCase().trim();
    if (r === 'plastico' || r === 'plastic') return 'plastic';
    if (r === 'metal' || r === 'can') return 'can';
    if (r === 'papel_y_carton' || r === 'paper') return 'paper';
    return 'general'; // general, vidrio, organico 등 그 외 전부
  }

  // 위험 지수 → 등급/색상 매핑
  function riskMeta(risk) {
    if (risk < 30) return { grade: 'A', status: '양호', color: '#27a644' };
    if (risk < 55) return { grade: 'B', status: '주의', color: '#e6a23c' };
    if (risk < 78) return { grade: 'C', status: '경고', color: '#f2994a' };
    return { grade: 'D', status: '위험', color: '#e5484d' };
  }

  function rgba(hex, a) {
    var h = hex.replace('#', '');
    if (h.length === 3) h = h.split('').map(function (c) { return c + c; }).join('');
    var n = parseInt(h, 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  function pad(n) { return String(n).padStart(2, '0'); }
  function nowStr() {
    var d = new Date();
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  // 콘솔 라인 키워드 → 색상
  function consoleColor(t) {
    var l = t.toLowerCase();
    if (l.indexOf('error') >= 0 || l.indexOf('traceback') >= 0 || l.indexOf('exception') >= 0) return '#e5484d';
    if (l.indexOf('warn') >= 0) return '#e6a23c';
    if (l.indexOf('info') >= 0) return '#4ea7fc';
    if (l.indexOf('success') >= 0 || t.indexOf('✅') >= 0 || l.indexOf('[ok]') >= 0) return '#27a644';
    return '#8a8f98';
  }

  var tempHistory = [], humHistory = [];
  var tempChart, humChart, barChart;

  function lineChart(canvas, color) {
    var ctx = canvas.getContext('2d');
    var g = ctx.createLinearGradient(0, 0, 0, 80);
    g.addColorStop(0, rgba(color, 0.30));
    g.addColorStop(1, rgba(color, 0));
    return new Chart(canvas, {
      type: 'line',
      data: { labels: [], datasets: [{ data: [], borderColor: color, borderWidth: 2, fill: true, backgroundColor: g, tension: 0.4, pointRadius: 0 }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
          x: { display: false },
          y: { display: true, position: 'right', grid: { color: 'rgba(255,255,255,0.05)' }, border: { display: false }, ticks: { color: '#62666d', font: { size: 9 }, maxTicksLimit: 3, padding: 3 } }
        }
      }
    });
  }

  function initCharts() {
    tempChart = lineChart(document.getElementById('temp-chart'), '#5e6ad2');
    humChart = lineChart(document.getElementById('hum-chart'), '#4ea7fc');
    barChart = new Chart(document.getElementById('bar-chart'), {
      type: 'bar',
      data: {
        labels: ['🥤 플라스틱', '🥫 캔', '📦 종이', '🗑️ 일반'],
        datasets: [{ data: [0, 0, 0, 0], backgroundColor: ['#4ea7fc', '#e6a23c', '#27a644', '#8a8f98'], borderRadius: 6, maxBarThickness: 72 }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: true, backgroundColor: '#141517', borderColor: '#23252a', borderWidth: 1, titleColor: '#f7f8f8', bodyColor: '#d0d6e0', padding: 10, displayColors: false } },
        scales: {
          x: { grid: { display: false }, border: { display: false }, ticks: { color: '#8a8f98', font: { size: 12 } } },
          y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.05)' }, border: { display: false }, ticks: { color: '#62666d', font: { size: 11 } } }
        }
      }
    });
  }

  var connected = false;
  function setConn(ok) {
    connected = ok;
    var dot = document.getElementById('conn-dot');
    var txt = document.getElementById('conn-text');
    var tdot = document.getElementById('term-dot');
    var tconn = document.getElementById('term-conn');
    var color = ok ? '#27a644' : '#62666d';
    var label = ok ? '수신 중' : '연결 끊김';
    var shadow = ok ? '0 0 8px rgba(39,166,68,0.6)' : 'none';
    dot.style.background = color; dot.style.boxShadow = shadow;
    txt.textContent = label; txt.style.color = ok ? '#27a644' : '#8a8f98';
    tdot.style.background = color; tdot.style.boxShadow = shadow;
    tconn.textContent = label; tconn.style.color = ok ? '#27a644' : '#8a8f98';
  }

  function renderLogs(recent) {
    var list = document.getElementById('log-list');
    if (!recent || recent.length === 0) {
      list.innerHTML = '<div style="text-align:center;color:#5b5f66;padding:40px 0;font-size:12px">배출 기록이 없습니다.</div>';
      return;
    }
    list.innerHTML = recent.map(function (r) {
      var m = CAT[classifyType(r.type)];
      // confidence 우선, 없으면 angle fallback
      var conf = (r.confidence != null) ? r.confidence : r.angle;
      var confText = '(' + Number(conf).toFixed(1) + '%)';
      return '<div style="display:flex;flex-direction:column;gap:3px;padding:9px 0;border-bottom:1px solid #16181c">'
        + '<div style="display:flex;align-items:baseline;gap:7px">'
        + '<span style="font-size:15px;line-height:1">' + m.emoji + '</span>'
        + '<span style="color:' + m.color + ';font-weight:600;font-size:14px">' + m.label + '</span>'
        + '<span class="mono" style="font-size:12px;color:#8a8f98;opacity:0.55">' + confText + '</span>'
        + '</div>'
        + '<div class="mono" style="font-size:11px;color:#5b5f66;letter-spacing:0.2px">' + r.time + '</div>'
        + '</div>';
    }).join('');
  }

  function updateStatus() {
    fetch('/api/status').then(function (res) { return res.json(); }).then(function (data) {
      setConn(true);
      var env = data.env || {};
      var temp = Number(env.temperature || 0);
      var hum = Number(env.humidity || 0);
      var risk = Math.round(Number(env.risk || 0));

      document.getElementById('temp-val').textContent = temp.toFixed(1);
      document.getElementById('hum-val').textContent = String(Math.round(hum));

      // 폴링 시 히스토리에 push, 10개 초과 시 shift
      tempHistory.push(temp); if (tempHistory.length > 10) tempHistory.shift();
      humHistory.push(hum); if (humHistory.length > 10) humHistory.shift();
      if (tempChart) {
        tempChart.data.labels = tempHistory.map(function (_, i) { return i + 1; });
        tempChart.data.datasets[0].data = tempHistory.slice();
        tempChart.update('none');
      }
      if (humChart) {
        humChart.data.labels = humHistory.map(function (_, i) { return i + 1; });
        humChart.data.datasets[0].data = humHistory.slice();
        humChart.update('none');
      }

      // 위험 지수 + 등급
      var meta = riskMeta(risk);
      var clean = Math.round(100 - risk);
      document.getElementById('risk-val').textContent = String(risk);
      var rb = document.getElementById('risk-bar'); rb.style.width = risk + '%'; rb.style.background = meta.color;
      document.getElementById('risk-dot').style.background = meta.color;
      var rs = document.getElementById('risk-status'); rs.textContent = meta.status; rs.style.color = meta.color;
      document.getElementById('risk-hint').textContent = risk < 55 ? '안정' : '점검 필요';

      var gv = document.getElementById('grade-val'); gv.textContent = meta.grade; gv.style.color = meta.color;
      var gs = document.getElementById('grade-status'); gs.textContent = meta.status; gs.style.color = meta.color;
      document.getElementById('grade-chip').textContent = '청결도 ' + clean;
      var gb = document.getElementById('grade-bar'); gb.style.width = clean + '%'; gb.style.background = meta.color;

      // 누적 통계 폴딩 (원시 type → 4 버킷)
      var counts = { plastic: 0, can: 0, paper: 0, general: 0 };
      var stats = data.stats || {};
      Object.keys(stats).forEach(function (k) { counts[classifyType(k)] += stats[k]; });
      if (barChart) {
        barChart.data.datasets[0].data = [counts.plastic, counts.can, counts.paper, counts.general];
        barChart.update('none');
      }
      document.getElementById('total-badge').textContent = '총 ' + Number(data.total_count || 0).toLocaleString() + '건';

      renderLogs(data.recent || []);
    }).catch(function () { setConn(false); });
  }

  var consoleIndex = 0;
  function appendConsole(lines) {
    var body = document.getElementById('console-body');
    lines.forEach(function (t) {
      var div = document.createElement('div');
      div.textContent = t;
      div.style.color = consoleColor(t);
      div.style.whiteSpace = 'pre-wrap';
      div.style.padding = '1px 0';
      body.appendChild(div);
    });
    while (body.childElementCount > 200) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
  }

  function updateConsole() {
    fetch('/api/console_log?since=' + consoleIndex).then(function (res) { return res.json(); }).then(function (data) {
      if (data.lines && data.lines.length) appendConsole(data.lines);
      if (typeof data.next_index === 'number') consoleIndex = data.next_index;
    }).catch(function () {});
  }

  function start() {
    if (typeof Chart !== 'undefined') {
      initCharts();
      document.getElementById('clock').textContent = nowStr();
      updateStatus();
      updateConsole();
      setInterval(updateStatus, 2000);
      setInterval(updateConsole, 2000);
      setInterval(function () { document.getElementById('clock').textContent = nowStr(); }, 1000);
    } else {
      setTimeout(start, 120);
    }
  }
  window.addEventListener('load', start);
</script>
</body>
</html>
"""

@flask_app.route('/')
def dashboard():
    """웹 관제 센터 프론트엔드 호스팅"""
    return render_template_string(DASHBOARD_HTML)


def start_flask_server():
    """Flask 웹 서버를 독립 포트로 백그라운드 구동"""
    debug_print("FLASK", "백그라운드 스레드에서 웹 서버 시동 중... [Port 5000]")
    # 로컬 AP망의 모든 기기에서 공유 접속을 허용하도록 host='0.0.0.0' 바인딩
    flask_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


# ==========================================
# 대기(IDLE) 화면: 온습도 + 안내 문구 표시
# ==========================================
def show_idle_screen(lcd, temp=None, hum=None):
    """대기 상태 LCD: 1행에 온습도(측정 전 안내), 2행에 투입 안내"""
    if temp is not None and hum is not None:
        lcd.set_message(f"T:{temp:.1f}C H:{int(round(hum))}%", lcd.LCD_LINE_1)
    else:
        lcd.set_message("PLACE WASTE HERE", lcd.LCD_LINE_1)
    lcd.set_message("APPROACH TO TRIG", lcd.LCD_LINE_2)


# ==========================================
# 9. 메인 통합 관제 및 상태 기계 구동 루프
# ==========================================
def main():
    print("=" * 60)
    print("AIoT 스마트 분리수거 시스템 (Camera + Servo + LED + SQLite3 + Flask)")
    print("=" * 60)
    
    # 5단계: Flask 웹 관제서버 백그라운드 스레드 시동
    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()

    push_console("[INFO] booting AIoT sorter node")
    push_console("✅ model endpoint ready: " + API_URL)
    push_console("[INFO] flask dashboard up @ :5000")

    lcd = I2CLCD(address=LCD_ADDRESS, bus_num=I2C_BUS)
    led = LEDController(CHIP_PATH, LED_PIN_R, LED_PIN_Y, LED_PIN_G, RGB_PIN_R, RGB_PIN_G, RGB_PIN_B)
    servo = ServoController(CHIP_PATH, SERVO_PIN)
    entry_servo = ServoController(CHIP_PATH, ENTRY_SERVO_PIN)

    lcd.set_message("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
    lcd.set_message("   INITIAL...   ", lcd.LCD_LINE_2)
    time.sleep(1.0)

    current_state = STATE_IDLE
    led.set_state(STATE_IDLE) 
    
    # 시스템 시작 시 분류 서보를 초기 위치(0도)로, 입구를 닫는 0도로 맞춤
    debug_print("SERVO", "시스템 시작 시 분류 서보 초기 위치(0도)로 이동")
    servo.set_angle(0, duration=1.0)
    debug_print("ENTRY_SERVO", "시스템 시작 시 입구를 닫는 0도로 설정")
    entry_servo.set_angle(0, duration=1.0)
    
    last_dht_time = 0      
    state_entry_time = 0
    result_displayed = False
    success_item_name = ""
    success_confidence = 0.0
    error_reason = ""
    last_temp = None      # 대기 화면에 표시할 최근 온도
    last_hum = None       # 대기 화면에 표시할 최근 습도

    show_idle_screen(lcd, last_temp, last_hum)
    print("\n상태 기계 구동 엔진 가동 중... [현재 상태: IDLE]")
    print(f" -> 통신 대상 서버: {API_URL}")
    push_console("[INFO] polling sensors — stream open")

    try:
        while True:
            current_time = time.time()
            lcd.update_scroll()
            led.update() 
            
            # [상태 1] STATE_IDLE
            if current_state == STATE_IDLE:
                # 4단계: 5초 주기로 대기 중에 온습도 및 위생예측 알고리즘 분석 후 SQLite3에 자동 INSERT
                if current_time - last_dht_time >= 5.0:
                    temp, hum, status = read_dht11_detailed()
                    if status == "SUCCESS":
                        # 불쾌지수 공식 대조 및 SQLite3 env_logs 테이블 자동 적재
                        di, hygiene_status = calculate_hygiene_and_log(temp, hum)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 내부 온도: {temp}°C | 습도: {hum}% | 불쾌지수: {di} | 위생상태: {hygiene_status}")
                        push_console(f"[INFO] env temp={temp}C hum={hum}% DI={di} status={hygiene_status}")
                        # 대기 화면 온습도 갱신
                        last_temp, last_hum = temp, hum
                        show_idle_screen(lcd, last_temp, last_hum)
                    last_dht_time = current_time

                dist = get_ultrasonic_distance()
                if 0.0 < dist <= 7.0:
                    print(f"\n[트리거 작동] {dist}cm에 사용자 감지! 카메라 캡처를 실행합니다.")
                    push_console(f"[INFO] user detected at {dist}cm — frame capturing...")
                    current_state = STATE_SCANNING
                    led.set_state(STATE_SCANNING)

            # [상태 2] STATE_SCANNING: 촬영 및 API 서버 전송
            elif current_state == STATE_SCANNING:
                lcd.clear()
                lcd.set_message(" USER DETECTED! ", lcd.LCD_LINE_1)
                lcd.set_message("  CAPTURING...  ", lcd.LCD_LINE_2)
                
                ret = capture_single_frame(TEMP_IMAGE_PATH)
                led.turn_off_rgb()
                
                if not ret:
                    print(" [SYSTEM_ALERT] 카메라 이미지 프레임을 가져오지 못했습니다.")
                    push_console("ERROR: camera capture failed")
                    current_state = STATE_RESULT_ERROR
                    led.set_state(STATE_RESULT_ERROR)
                    error_reason = "SYSTEM_FAULT"
                    state_entry_time = time.time()
                    result_displayed = False
                    continue
                    
                lcd.clear()
                lcd.set_message("  SENDING DATA  ", lcd.LCD_LINE_1)
                lcd.set_message("  ANALYZING...  ", lcd.LCD_LINE_2)
                
                print(f" -> 컨테이너 서버({API_URL})로 이미지 전송 중...")
                try:
                    with open(TEMP_IMAGE_PATH, "rb") as image_file:
                        files = {"file": ("captured_waste.jpg", image_file, "image/jpeg")}
                        response = requests.post(API_URL, files=files, timeout=10.0)
                        response.raise_for_status() 
                        
                    api_result = response.json()
                    predictions = api_result.get("predictions", [])
                    server_time = api_result.get("processing_time_sec", 0)
                    print(f" -> 서버 처리 완료 (응답시간: {server_time}초)")
                    push_console(f"[INFO] inference done in {server_time}s")

                    if predictions:
                        best_pred = predictions[0]
                        detected_item = best_pred["class_name"]
                        max_conf = best_pred["confidence"]

                        if max_conf >= CONFIDENCE_THRESHOLD:
                            print(f" -> 탐지 성공: '{detected_item}' (신뢰도: {max_conf * 100:.1f}%)")
                            push_console(f"✅ classified: {detected_item} conf={max_conf:.2f}")
                            current_state = STATE_RESULT_SUCCESS
                            led.set_state(STATE_RESULT_SUCCESS)
                            success_item_name = detected_item
                            success_confidence = max_conf
                        else:
                            print(f" -> 탐지 실패: 물체를 감지했으나 신뢰도({max_conf * 100:.1f}%)가 기준 미달입니다.")
                            push_console(f"[WARN] low confidence {max_conf * 100:.1f}% — rejected")
                            current_state = STATE_RESULT_ERROR
                            led.set_state(STATE_RESULT_ERROR)
                            error_reason = "LOW_CONFIDENCE"
                    else:
                        print(" -> 탐지 실패: 서버에서 예측 데이터가 오지 않았습니다.")
                        push_console("[WARN] no object detected in frame")
                        current_state = STATE_RESULT_ERROR
                        led.set_state(STATE_RESULT_ERROR)
                        error_reason = "NO_OBJECT"

                except requests.exceptions.RequestException as e:
                    print(f" [API_ERROR] 서버 통신 실패: {e}")
                    push_console(f"ERROR: server request failed — {e}")
                    current_state = STATE_RESULT_ERROR
                    led.set_state(STATE_RESULT_ERROR)
                    error_reason = "SYSTEM_FAULT"

                state_entry_time = time.time()
                result_displayed = False

            # [상태 3] STATE_RESULT_SUCCESS: 모터 작동 및 DB 적재
            elif current_state == STATE_RESULT_SUCCESS:
                if not result_displayed:
                    lcd.clear()
                    target_angle = 90  # 기본값
                    
                    # ----------------------------------------------------
                    # 서보모터 각도 맵핑 라우팅 (총 4개 방향)
                    # ----------------------------------------------------
                    if success_item_name == "plastico":
                        target_angle = 25
                        lcd.set_message("PLASTIC BOTTLE", lcd.LCD_LINE_1)
                        lcd.set_message("[3] REMOVE CAP&LABEL", lcd.LCD_LINE_2)
                        print("가이드: [플라스틱] -> 25도 각도로 배출합니다.")
                    elif success_item_name == "metal":
                        target_angle = 75
                        lcd.set_message("CAN & METAL WST", lcd.LCD_LINE_1)
                        lcd.set_message("[4] EMPTY & FLATTEN", lcd.LCD_LINE_2)
                        print("가이드: [캔/메탈] -> 75도 각도로 배출합니다.")
                    elif success_item_name == "papel_y_carton":
                        target_angle = 105
                        lcd.set_message("PAPER / BOX WST", lcd.LCD_LINE_1)
                        lcd.set_message("[2] REMOVE TAPE&FOLD", lcd.LCD_LINE_2)
                        print("가이드: [종이/박스류] -> 105도 각도로 배출합니다.")
                    elif success_item_name in ["vidrio", "organico"]:
                        target_angle = 155
                        if success_item_name == "vidrio":
                            lcd.set_message("GLASS BOTTLE", lcd.LCD_LINE_1)
                            lcd.set_message("[1] RINSE WITH WATER", lcd.LCD_LINE_2)
                        else:
                            lcd.set_message("ORGANIC WASTE", lcd.LCD_LINE_1)
                            lcd.set_message("[1] DRAIN WATER OUT", lcd.LCD_LINE_2)
                        print(f"가이드: [{success_item_name}] -> 155도 각도로 배출합니다.")
                    else:
                        target_angle = 155
                        lcd.set_message("GENERAL TRASH", lcd.LCD_LINE_1)
                        lcd.set_message("[1] STANDARD DISPOSE", lcd.LCD_LINE_2)
                        print("가이드: [일반쓰레기] -> 155도 각도로 배출합니다.")

                    debug_print(
                        "SERVO",
                        f"분류 결과 반영: success_item_name={success_item_name}, target_angle={target_angle}, state={current_state}"
                    )

                    # 4단계: 쓰레기 분류 결과 및 모터 서보 구동 각도를 SQLite3 데이터베이스에 로깅 (신뢰도 포함)
                    log_waste_event(success_item_name, target_angle, success_confidence)

                    # 오차를 줄이기 위해 분류 판을 0도로 먼저 보낸 뒤 목표 각도로 이동
                    debug_print("SERVO", "목표 각도 이동 전 0도로 기준 복귀")
                    servo.set_angle(0, duration=1.0)
                    push_console(f"[INFO] servo angle set to {target_angle} deg")
                    servo.set_angle(target_angle, duration=1.0)

                    # 판 각도 조정이 끝난 뒤 입구 개방
                    debug_print("ENTRY_SERVO", "분류 성공 감지로 입구 개방(180도)")
                    entry_servo.set_angle(180, duration=0.8)

                    result_displayed = True

                # 상태 진입 후 5초 경과 시 다시 대기 상태로 복귀 (비차단)
                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    show_idle_screen(lcd, last_temp, last_hum)
                    print(" -> 배출 완료. 분류 판을 0도로 복귀 후 입구를 닫습니다.\n")

                    # 분류 판을 먼저 0도로 복귀
                    debug_print("SERVO", "대기 상태 복귀 전 분류 서보 0도로 되돌림")
                    servo.set_angle(0, duration=1.0)

                    # 그다음 입구 닫기
                    debug_print("ENTRY_SERVO", "대기 모드 복귀로 입구 닫기(0도)")
                    entry_servo.set_angle(0, duration=0.8)

                    current_state = STATE_IDLE
                    led.set_state(STATE_IDLE)

            # [상태 4] STATE_RESULT_ERROR
            elif current_state == STATE_RESULT_ERROR:
                if not result_displayed:
                    lcd.clear()
                    if error_reason == "NO_OBJECT":
                        lcd.set_message("DETECTION ERROR", lcd.LCD_LINE_1)
                        lcd.set_message("TRY AGAIN (EMPTY)", lcd.LCD_LINE_2)
                    elif error_reason == "LOW_CONFIDENCE":
                        lcd.set_message("DETECTION ERROR", lcd.LCD_LINE_1)
                        lcd.set_message("UNRECOGNIZED WT", lcd.LCD_LINE_2)
                    elif error_reason == "SYSTEM_FAULT":
                        lcd.set_message("  SYSTEM ERROR  ", lcd.LCD_LINE_1)
                        lcd.set_message("CHECK SERVER/CAM", lcd.LCD_LINE_2)

                    result_displayed = True

                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    show_idle_screen(lcd, last_temp, last_hum)
                    current_state = STATE_IDLE
                    led.set_state(STATE_IDLE)

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n사용자에 의해 시스템이 안전 종료됩니다.")
    finally:
        lcd.clear()
        led.cleanup()
        servo.cleanup()
        entry_servo.cleanup()

if __name__ == "__main__":
    main()