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
from flask import Flask, jsonify, render_template_string  # Flask 백엔드 모듈 임포트

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
            servo_angle INTEGER
        )
    ''')
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
            if time.time() - self.last_toggle_time >= 0.5:
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


def log_waste_event(waste_type, servo_angle):
    """AIoT 배출 성공 시 쓰레기 이벤트 내역을 waste_logs 테이블에 적재합니다."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO waste_logs (waste_type, servo_angle)
            VALUES (?, ?)
        ''', (waste_type, servo_angle))
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
        
        # 2. 최근 5개 배출 이력 조회
        cursor.execute("SELECT timestamp, waste_type, servo_angle FROM waste_logs ORDER BY id DESC LIMIT 5")
        waste_rows = cursor.fetchall()
        
        # 3. 종류별 누적 배출 통계 카운트 계산
        cursor.execute("SELECT waste_type, COUNT(*) FROM waste_logs GROUP BY waste_type")
        stat_rows = cursor.fetchall()
        
        # 4. 전체 분리수거 처리 횟수 조회
        cursor.execute("SELECT COUNT(*) FROM waste_logs")
        total_recycling_count = cursor.fetchone()[0]
        
        conn.close()
        
        # JSON 포맷 바인딩
        current_env = {
            "time": env_row[0].split()[-1] if env_row else "N/A",
            "temperature": env_row[1] if env_row else 0.0,
            "humidity": env_row[2] if env_row else 0.0,
            "discomfort_index": env_row[3] if env_row else 0.0,
            "hygiene_status": env_row[4] if env_row else "Unknown"
        }
        
        recent_wastes = []
        for r in waste_rows:
            # 타임스탬프에서 시:분:초만 분리
            t_str = r[0].split()[-1] if ' ' in r[0] else r[0]
            recent_wastes.append({
                "time": t_str,
                "type": r[1],
                "angle": r[2]
            })
            
        # 통계 초기화
        stats = {"plastico": 0, "metal": 0, "papel_y_carton": 0, "vidrio": 0, "organico": 0, "general": 0}
        for s in stat_rows:
            key = s[0]
            if key in stats:
                stats[key] = s[1]
            else:
                stats["general"] += s[1]
                
        return jsonify({
            "env": current_env,
            "recent": recent_wastes,
            "stats": stats,
            "total_count": total_recycling_count
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 단일 웹 대시보드 렌더링용 Tailwind HTML 소스 템플릿
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gachon AIoT Smart Recycling 관제 센터</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700&display=swap');
        body { font-family: 'Pretendard', sans-serif; }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">

    <!-- Gachon University Header Banner -->
    <header class="bg-gradient-to-r from-blue-950 via-slate-900 to-indigo-950 border-b border-indigo-950 px-6 py-4 shadow-xl">
        <div class="max-w-7xl mx-auto flex flex-col sm:flex-row justify-between items-center gap-3">
            <div class="flex items-center gap-3">
                <span class="text-3xl">♻️</span>
                <div>
                    <h1 class="text-xl font-bold tracking-tight text-white">AIoT 스마트 분리수거 로컬 관제 센터</h1>
                    <p class="text-xs text-indigo-300 font-semibold">Gachon University • IoT26 Spring 2026</p>
                </div>
            </div>
            <div class="flex items-center gap-2 text-xs bg-indigo-950/70 border border-indigo-500/30 px-3.5 py-1.5 rounded-full text-indigo-300">
                <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
                <span class="font-medium">스레드 2: Flask 로컬 웹 서버 가동 중 [Port 5000]</span>
            </div>
        </div>
    </header>

    <main class="max-w-7xl mx-auto px-4 py-8">
        
        <!-- 실시간 위생 지수 및 상태 대시보드 카드 -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            
            <!-- 카드 1: 위생 위험 지수 -->
            <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 shadow-md transition hover:border-indigo-500">
                <div class="flex justify-between items-center mb-3">
                    <span class="text-xs text-slate-400 font-bold uppercase tracking-wider">위생 위험 불쾌지수</span>
                    <span class="p-2 bg-indigo-500/10 text-indigo-400 rounded-lg text-sm"><i class="fa-solid fa-chart-line"></i></span>
                </div>
                <div class="flex items-baseline gap-1.5">
                    <span id="discomfort-val" class="text-4xl font-extrabold text-white">0.0</span>
                    <span class="text-sm text-slate-400">DI</span>
                </div>
                <div class="w-full bg-slate-800 h-2 rounded-full mt-4 overflow-hidden">
                    <div id="discomfort-bar" class="bg-indigo-500 h-2 rounded-full transition-all duration-1000" style="width: 0%"></div>
                </div>
                <p id="di-advice" class="text-xxs text-slate-400 mt-2">상태를 수집 중입니다...</p>
            </div>

            <!-- 카드 2: 위생 상태 등급 -->
            <div id="status-card" class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 shadow-md transition">
                <div class="flex justify-between items-center mb-3">
                    <span class="text-xs text-slate-400 font-bold uppercase tracking-wider">위생 관리 상태</span>
                    <span class="p-2 bg-emerald-500/10 text-emerald-400 rounded-lg text-sm"><i class="fa-solid fa-shield-virus"></i></span>
                </div>
                <div class="flex items-center gap-2">
                    <span id="status-val" class="text-3xl font-extrabold text-white">None</span>
                </div>
                <div class="w-full bg-slate-800 h-2 rounded-full mt-4 overflow-hidden">
                    <div id="status-bar" class="bg-emerald-500 h-2 rounded-full transition-all duration-1000" style="width: 0%"></div>
                </div>
                <p id="status-advice" class="text-xxs text-slate-400 mt-2">안정적인 상태가 유지 중입니다.</p>
            </div>

            <!-- 카드 3: 위생 센서 온도 -->
            <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 shadow-md transition hover:border-rose-500">
                <div class="flex justify-between items-center mb-3">
                    <span class="text-xs text-slate-400 font-bold uppercase tracking-wider">쓰레기통 온도</span>
                    <span class="p-2 bg-rose-500/10 text-rose-400 rounded-lg text-sm"><i class="fa-solid fa-thermometer-half"></i></span>
                </div>
                <div class="flex items-baseline gap-1">
                    <span id="temp-val" class="text-4xl font-extrabold text-white">0.0</span>
                    <span class="text-sm text-slate-400">°C</span>
                </div>
                <div class="w-full bg-slate-800 h-2 rounded-full mt-4 overflow-hidden">
                    <div id="temp-bar" class="bg-rose-500 h-2 rounded-full transition-all duration-1000" style="width: 0%"></div>
                </div>
                <p class="text-xxs text-slate-400 mt-2">DHT11 센서 실시간 수집 결과</p>
            </div>

            <!-- 카드 4: 위생 센서 습도 -->
            <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 shadow-md transition hover:border-sky-500">
                <div class="flex justify-between items-center mb-3">
                    <span class="text-xs text-slate-400 font-bold uppercase tracking-wider">쓰레기통 습도</span>
                    <span class="p-2 bg-sky-500/10 text-sky-400 rounded-lg text-sm"><i class="fa-solid fa-droplet"></i></span>
                </div>
                <div class="flex items-baseline gap-1">
                    <span id="hum-val" class="text-4xl font-extrabold text-white">0.0</span>
                    <span class="text-sm text-slate-400">%</span>
                </div>
                <div class="w-full bg-slate-800 h-2 rounded-full mt-4 overflow-hidden">
                    <div id="hum-bar" class="bg-sky-500 h-2 rounded-full transition-all duration-1000" style="width: 0%"></div>
                </div>
                <p class="text-xxs text-slate-400 mt-2">악취 및 곰팡이 유발 지표 모니터링</p>
            </div>
            
        </div>

        <!-- 메인 데이터 그리드 (좌측: 누적 분리수거 통계, 우측: 최근 실시간 배출 로그) -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
            
            <!-- 좌측 2/3 영역: 누적 처리수 및 통계 -->
            <div class="lg:col-span-2 space-y-8">
                
                <!-- 누적 쓰레기 수집 통계 -->
                <div class="bg-slate-900/40 border border-slate-900 rounded-2xl p-6">
                    <div class="flex justify-between items-center mb-6">
                        <h2 class="text-lg font-bold text-white flex items-center gap-2">
                            <span class="w-1.5 h-4 bg-indigo-500 rounded-full"></span>
                            카테고리별 누적 분리배출 현황
                        </h2>
                        <div class="bg-slate-800/50 border border-slate-700/50 px-3.5 py-1.5 rounded-xl text-right">
                            <span class="text-xxs text-slate-400 block font-semibold">총 분리배출 처리 건수</span>
                            <span id="total-cnt-val" class="text-lg font-extrabold text-indigo-400">0건</span>
                        </div>
                    </div>
                    
                    <div class="grid grid-cols-2 sm:grid-cols-3 gap-4">
                        <!-- 플라스틱 -->
                        <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                            <div class="flex justify-between text-xs text-slate-400 mb-2 font-semibold">
                                <span>🥤 플라스틱</span>
                                <span id="stat-plastic-cnt" class="text-indigo-400 font-bold">0건</span>
                            </div>
                            <div class="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                                <div id="stat-plastic-bar" class="bg-indigo-500 h-1.5 transition-all duration-1000" style="width: 0%"></div>
                            </div>
                        </div>
                        <!-- 캔/메탈 -->
                        <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                            <div class="flex justify-between text-xs text-slate-400 mb-2 font-semibold">
                                <span>🥫 캔 / 메탈</span>
                                <span id="stat-metal-cnt" class="text-rose-400 font-bold">0건</span>
                            </div>
                            <div class="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                                <div id="stat-metal-bar" class="bg-rose-500 h-1.5 transition-all duration-1000" style="width: 0%"></div>
                            </div>
                        </div>
                        <!-- 종이 -->
                        <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                            <div class="flex justify-between text-xs text-slate-400 mb-2 font-semibold">
                                <span>📦 종이류</span>
                                <span id="stat-paper-cnt" class="text-amber-400 font-bold">0건</span>
                            </div>
                            <div class="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                                <div id="stat-paper-bar" class="bg-amber-500 h-1.5 transition-all duration-1000" style="width: 0%"></div>
                            </div>
                        </div>
                        <!-- 유리 -->
                        <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                            <div class="flex justify-between text-xs text-slate-400 mb-2 font-semibold">
                                <span>🍾 유리병</span>
                                <span id="stat-glass-cnt" class="text-emerald-400 font-bold">0건</span>
                            </div>
                            <div class="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                                <div id="stat-glass-bar" class="bg-emerald-500 h-1.5 transition-all duration-1000" style="width: 0%"></div>
                            </div>
                        </div>
                        <!-- 유기물 -->
                        <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                            <div class="flex justify-between text-xs text-slate-400 mb-2 font-semibold">
                                <span>🍎 음식물</span>
                                <span id="stat-organic-cnt" class="text-teal-400 font-bold">0건</span>
                            </div>
                            <div class="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                                <div id="stat-organic-bar" class="bg-teal-500 h-1.5 transition-all duration-1000" style="width: 0%"></div>
                            </div>
                        </div>
                        <!-- 일반 -->
                        <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                            <div class="flex justify-between text-xs text-slate-400 mb-2 font-semibold">
                                <span>🗑️ 일반쓰레기</span>
                                <span id="stat-general-cnt" class="text-slate-400 font-bold">0건</span>
                            </div>
                            <div class="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
                                <div id="stat-general-bar" class="bg-slate-500 h-1.5 transition-all duration-1000" style="width: 0%"></div>
                            </div>
                        </div>
                    </div>
                </div>

            </div>

            <!-- 우측 1/3 영역: 최근 실시간 배출 로그 이력 -->
            <div class="bg-slate-900/40 border border-slate-900 rounded-2xl p-6 flex flex-col h-full">
                <h2 class="text-lg font-bold text-white mb-6 flex items-center gap-2">
                    <span class="w-1.5 h-4 bg-sky-500 rounded-full"></span>
                    최근 쓰레기 배출 로그 (SQLite3)
                </h2>
                
                <div class="flex-1 space-y-4 max-h-[350px] overflow-y-auto pr-1" id="recent-logs-container">
                    <div class="text-center py-12 text-slate-500 text-sm">연결을 확인하는 중입니다...</div>
                </div>
            </div>

        </div>
    </main>

    <footer class="mt-20 border-t border-slate-900 py-6 text-center text-xs text-slate-600">
        <p>© 2026 Gachon University Prof. Jaehyuk Choi - Spring 2026 AIoT Team Project</p>
    </footer>

    <!-- 실시간 상태 데이터 Fetching & 화면 업데이트 로직 -->
    <script>
        function updateDashboard() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    const env = data.env;
                    
                    // 1. 온습도 갱신
                    document.getElementById('temp-val').textContent = env.temperature.toFixed(1);
                    document.getElementById('temp-bar').style.width = Math.min((env.temperature / 45) * 100, 100) + '%';
                    
                    document.getElementById('hum-val').textContent = env.humidity.toFixed(1);
                    document.getElementById('hum-bar').style.width = env.humidity + '%';

                    // 2. 불쾌지수(DI) 갱신
                    document.getElementById('discomfort-val').textContent = env.discomfort_index.toFixed(1);
                    document.getElementById('discomfort-bar').style.width = Math.min((env.discomfort_index / 100) * 100, 100) + '%';
                    
                    const diAdvice = document.getElementById('di-advice');
                    if(env.discomfort_index >= 75) {
                        diAdvice.innerHTML = "위생 위험 지수가 높습니다. 환기 권장.";
                        diAdvice.className = "text-xxs text-rose-400 mt-2 font-semibold animate-pulse";
                    } else if(env.discomfort_index >= 68) {
                        diAdvice.innerHTML = "악취 우려가 시작되는 구간입니다.";
                        diAdvice.className = "text-xxs text-amber-400 mt-2";
                    } else {
                        diAdvice.innerHTML = "안정적이고 쾌적한 내부 수거 상태입니다.";
                        diAdvice.className = "text-xxs text-slate-400 mt-2";
                    }

                    // 3. 위생 상태 등급 갱신
                    const statusCard = document.getElementById('status-card');
                    const statusVal = document.getElementById('status-val');
                    const statusBar = document.getElementById('status-bar');
                    const statusAdvice = document.getElementById('status-advice');
                    
                    statusVal.textContent = env.hygiene_status;
                    
                    if(env.hygiene_status === "Warning") {
                        statusCard.className = "bg-rose-950/20 border border-rose-800 rounded-2xl p-6 shadow-md transition";
                        statusBar.className = "bg-rose-500 h-2 rounded-full transition-all duration-1000";
                        statusBar.style.width = "100%";
                        statusAdvice.textContent = "🚨 세균 및 바이러스 발생 번식 지수가 매우 높아 소독이 필요합니다.";
                        statusAdvice.className = "text-xxs text-rose-400 mt-2 font-semibold";
                    } else if(env.hygiene_status === "Caution") {
                        statusCard.className = "bg-amber-950/20 border border-amber-800 rounded-2xl p-6 shadow-md transition";
                        statusBar.className = "bg-amber-500 h-2 rounded-full transition-all duration-1000";
                        statusBar.style.width = "65%";
                        statusAdvice.textContent = "⚠️ 습기로 인한 악취 유발 가능성이 있습니다.";
                        statusAdvice.className = "text-xxs text-amber-400 mt-2";
                    } else {
                        statusCard.className = "bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 shadow-md transition hover:border-emerald-500";
                        statusBar.className = "bg-emerald-500 h-2 rounded-full transition-all duration-1000";
                        statusBar.style.width = "30%";
                        statusAdvice.textContent = "✅ 내부 세균 우려가 없는 청결한 상태입니다.";
                        statusAdvice.className = "text-xxs text-emerald-400 mt-2";
                    }

                    // 4. 총 배출 건수 갱신
                    document.getElementById('total-cnt-val').textContent = data.total_count + '건';

                    // 5. 종류별 통계 프로그레스바 갱신 (비율 연산)
                    updateStatBlock('plastic', data.stats.plastico, data.total_count);
                    updateStatBlock('metal', data.stats.metal, data.total_count);
                    updateStatBlock('paper', data.stats.papel_y_carton, data.total_count);
                    updateStatBlock('glass', data.stats.vidrio, data.total_count);
                    updateStatBlock('organic', data.stats.organico, data.total_count);
                    updateStatBlock('general', data.stats.general, data.total_count);

                    // 6. 실시간 배출 로그 피드백 갱신
                    const logsContainer = document.getElementById('recent-logs-container');
                    logsContainer.innerHTML = '';
                    
                    if (data.recent.length === 0) {
                        logsContainer.innerHTML = '<div class="text-center text-slate-600 py-12 text-xs">배출 기록이 존재하지 않습니다.</div>';
                    } else {
                        data.recent.forEach(log => {
                            const emoji = getEmoji(log.type);
                            const badgeColor = getBadgeColor(log.type);
                            
                            const itemHTML = `
                                <div class="bg-slate-900 border border-slate-800/60 p-3 rounded-xl flex justify-between items-center transition hover:border-slate-700">
                                    <div class="flex items-center gap-3">
                                        <span class="text-xl">${emoji}</span>
                                        <div>
                                            <div class="text-xs font-bold text-white uppercase">${log.type.replace('_', ' ')}</div>
                                            <div class="text-[10px] text-slate-500">${log.time}</div>
                                        </div>
                                    </div>
                                    <div class="text-right">
                                        <span class="inline-block px-2 py-1 text-[10px] font-bold rounded ${badgeColor}">${log.angle}도 지향</span>
                                    </div>
                                </div>
                            `;
                            logsContainer.innerHTML += itemHTML;
                        });
                    }
                })
                .catch(err => console.error("통신 장애 발생:", err));
        }

        function updateStatBlock(idPrefix, count, total) {
            document.getElementById(`stat-${idPrefix}-cnt`).textContent = count + '건';
            const percent = total > 0 ? (count / total) * 100 : 0;
            document.getElementById(`stat-${idPrefix}-bar`).style.width = percent + '%';
        }

        function getEmoji(type) {
            if (type.includes("plastic")) return '🥤';
            if (type.includes("metal")) return '🥫';
            if (type.includes("papel")) return '📦';
            if (type.includes("vidrio")) return '🍾';
            if (type.includes("organic")) return '🍎';
            return '🗑️';
        }

        function getBadgeColor(type) {
            if (type.includes("plastic")) return 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/30';
            if (type.includes("metal")) return 'bg-rose-500/10 text-rose-400 border border-rose-500/30';
            if (type.includes("papel")) return 'bg-amber-500/10 text-amber-400 border border-amber-500/30';
            if (type.includes("vidrio")) return 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
            if (type.includes("organic")) return 'bg-teal-500/10 text-teal-400 border border-teal-500/30';
            return 'bg-slate-500/10 text-slate-400 border border-slate-500/30';
        }

        // 3초 단위 갱신 루프 활성화
        setInterval(updateDashboard, 3000);
        window.onload = updateDashboard;
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
# 9. 메인 통합 관제 및 상태 기계 구동 루프
# ==========================================
def main():
    print("=" * 60)
    print("AIoT 스마트 분리수거 시스템 (Camera + Servo + LED + SQLite3 + Flask)")
    print("=" * 60)
    
    # 5단계: Flask 웹 관제서버 백그라운드 스레드 시동
    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()
    
    lcd = I2CLCD(address=LCD_ADDRESS, bus_num=I2C_BUS)
    led = LEDController(CHIP_PATH, LED_PIN_R, LED_PIN_Y, LED_PIN_G, RGB_PIN_R, RGB_PIN_G, RGB_PIN_B)
    servo = ServoController(CHIP_PATH, SERVO_PIN)
    entry_servo = ServoController(CHIP_PATH, ENTRY_SERVO_PIN)

    lcd.set_message("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
    lcd.set_message("   INITIAL...   ", lcd.LCD_LINE_2)
    time.sleep(1.0)

    current_state = STATE_IDLE
    led.set_state(STATE_IDLE) 
    
    # 시스템 시작 시 서보모터를 기본 수평 각도(90도)로 맞춤
    debug_print("SERVO", "시스템 시작 시 기본 위치(90도)로 이동")
    servo.set_angle(90, duration=1.0)
    debug_print("ENTRY_SERVO", "시스템 시작 시 입구를 닫는 0도로 설정")
    entry_servo.set_angle(0, duration=1.0)
    
    last_dht_time = 0      
    state_entry_time = 0
    result_displayed = False
    success_item_name = ""
    error_reason = ""
    
    lcd.set_message("PLACE WASTE FIRST", lcd.LCD_LINE_1)
    lcd.set_message("APPROACH TO TRIG", lcd.LCD_LINE_2)
    print("\n상태 기계 구동 엔진 가동 중... [현재 상태: IDLE]")
    print(f" -> 통신 대상 서버: {API_URL}")

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
                    last_dht_time = current_time
                
                dist = get_ultrasonic_distance()
                if 0.0 < dist <= 7.0:
                    print(f"\n[트리거 작동] {dist}cm에 사용자 감지! 카메라 캡처를 실행합니다.")
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

                    if predictions:
                        best_pred = predictions[0]
                        detected_item = best_pred["class_name"]
                        max_conf = best_pred["confidence"]
                        
                        if max_conf >= CONFIDENCE_THRESHOLD:
                            print(f" -> 탐지 성공: '{detected_item}' (신뢰도: {max_conf * 100:.1f}%)")
                            current_state = STATE_RESULT_SUCCESS
                            led.set_state(STATE_RESULT_SUCCESS)
                            success_item_name = detected_item
                        else:
                            print(f" -> 탐지 실패: 물체를 감지했으나 신뢰도({max_conf * 100:.1f}%)가 기준 미달입니다.")
                            current_state = STATE_RESULT_ERROR
                            led.set_state(STATE_RESULT_ERROR)
                            error_reason = "LOW_CONFIDENCE"
                    else:
                        print(" -> 탐지 실패: 서버에서 예측 데이터가 오지 않았습니다.")
                        current_state = STATE_RESULT_ERROR
                        led.set_state(STATE_RESULT_ERROR)
                        error_reason = "NO_OBJECT"

                except requests.exceptions.RequestException as e:
                    print(f" [API_ERROR] 서버 통신 실패: {e}")
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
                        target_angle = 35
                        lcd.set_message("PLASTIC BOTTLE", lcd.LCD_LINE_1)
                        lcd.set_message("REMOVE CAP&LABEL", lcd.LCD_LINE_2)
                        print("가이드: [플라스틱] -> 35도 각도로 배출합니다.")
                    elif success_item_name == "metal":
                        target_angle = 75
                        lcd.set_message("CAN & METAL WST", lcd.LCD_LINE_1)
                        lcd.set_message("EMPTY & FLATTEN", lcd.LCD_LINE_2)
                        print("가이드: [캔/메탈] -> 75도 각도로 배출합니다.")
                    elif success_item_name == "papel_y_carton":
                        target_angle = 105
                        lcd.set_message("PAPER / BOX WST", lcd.LCD_LINE_1)
                        lcd.set_message("REMOVE TAPE&FOLD", lcd.LCD_LINE_2)
                        print("가이드: [종이/박스류] -> 105도 각도로 배출합니다.")
                    elif success_item_name in ["vidrio", "organico"]:
                        target_angle = 145
                        if success_item_name == "vidrio":
                            lcd.set_message("GLASS BOTTLE", lcd.LCD_LINE_1)
                            lcd.set_message("RINSE WITH WATER", lcd.LCD_LINE_2)
                        else:
                            lcd.set_message("ORGANIC WASTE", lcd.LCD_LINE_1)
                            lcd.set_message("DRAIN WATER OUT", lcd.LCD_LINE_2)
                        print(f"가이드: [{success_item_name}] -> 145도 각도로 배출합니다.")
                    else:
                        target_angle = 145
                        lcd.set_message("GENERAL TRASH", lcd.LCD_LINE_1)
                        lcd.set_message("STANDARD DISPOSE", lcd.LCD_LINE_2)
                        print("가이드: [일반쓰레기] -> 145도 각도로 배출합니다.")

                    debug_print(
                        "SERVO",
                        f"분류 결과 반영: success_item_name={success_item_name}, target_angle={target_angle}, state={current_state}"
                    )

                    debug_print("ENTRY_SERVO", "분류 성공 감지로 입구 개방(180도)")
                    entry_servo.set_angle(180, duration=0.8)

                    # 4단계: 쓰레기 분류 결과 및 모터 서보 구동 각도를 SQLite3 데이터베이스에 로깅
                    log_waste_event(success_item_name, target_angle)
                    
                    # 판 기울이기 (동작 시간 약 1초 소요)
                    servo.set_angle(target_angle, duration=1.0)
                    result_displayed = True

                # 상태 진입 후 5초 경과 시 다시 대기 상태로 복귀 (비차단)
                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    lcd.set_message("PLACE WASTE FIRST", lcd.LCD_LINE_1)
                    lcd.set_message("APPROACH TO TRIG", lcd.LCD_LINE_2)
                    print(" -> 배출 완료. 판을 기본 각도(90도)로 복귀합니다.\n")
                    
                    debug_print("ENTRY_SERVO", "대기 모드 복귀로 입구 닫기(0도)")
                    entry_servo.set_angle(0, duration=0.8)

                    # 다음 쓰레기 측정을 위해 판을 다시 수평으로 복귀
                    debug_print("SERVO", "대기 상태 복귀 전 기본 위치(90도)로 되돌림")
                    servo.set_angle(90, duration=1.0)
                    
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
                    lcd.set_message("PLACE WASTE FIRST", lcd.LCD_LINE_1)
                    lcd.set_message("APPROACH TO TRIG", lcd.LCD_LINE_2)
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