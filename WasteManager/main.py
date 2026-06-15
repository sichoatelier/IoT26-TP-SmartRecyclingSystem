import os
import sys
import time
import subprocess
from datetime import datetime
import requests  
import gpiod
from gpiod.line import Direction, Value
from smbus2 import SMBus

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

# I2C LCD 설정
LCD_ADDRESS = 0x27  
I2C_BUS = 1

# 파일 경로 및 서버 통신 설정
SAVE_DIR = "/opt/Desktop"
if not os.path.exists(SAVE_DIR):
    SAVE_DIR = os.getcwd()  

TEMP_IMAGE_PATH = os.path.join(SAVE_DIR, "captured_waste.jpg")

# 컨테이너에 띄워둔 FastAPI 서버 주소
API_URL = "http://localhost:8000/predict"
CONFIDENCE_THRESHOLD = 0.5  

# ==========================================
# 3. I2C 16x2 LCD 드라이버 클래스
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
# 4. 온습도 센서 (DHT11) 함수
# ==========================================
def read_dht11_detailed():
    timestamps = []
    values = []
    try:
        with gpiod.request_lines(
            CHIP_PATH,
            consumer="DHT11",
            config={DHT_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT, output_value=gpiod.line.Value.ACTIVE)}
        ) as lines:
            lines.set_value(DHT_PIN, gpiod.line.Value.INACTIVE)
            time.sleep(0.018)
            lines.set_value(DHT_PIN, gpiod.line.Value.ACTIVE)
            time.sleep(0.00004)
            lines.reconfigure_lines(config={DHT_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)})
            
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
# 5. 초음파 센서 (HC-SR04) 함수
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
# 6. 카메라 제어 (CLI 유틸리티 전용 - OpenCV 완전 제거)
# ==========================================
def capture_single_frame(output_path):
    """
    라즈베리파이 5의 네이티브 카메라 툴을 사용하여 OpenCV 없이 가볍게 캡처합니다.
    --width, --height 옵션으로 크기를 줄이고, --rotation 옵션으로 180도 회전을 바로 적용합니다.
    """
    print("[CAM_DEBUG] 시스템 기본 카메라 툴을 사용하여 캡처를 시도합니다...")
    
    commands = [
        # rpicam-still (Raspberry Pi 5 기본)
        ["rpicam-still", "-t", "500", "--immediate", "--width", "640", "--height", "480", "--rotation", "180", "-o", output_path],
        # libcamera-still (호환성 목적)
        ["libcamera-still", "-t", "500", "--immediate", "--width", "640", "--height", "480", "--rotation", "180", "-o", output_path]
    ]
    
    for cmd in commands:
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0)
            # 캡처 성공 시 파일이 정상적으로 생성되었는지 확인
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                print(f"[CAM_DEBUG] [SUCCESS] 이미지 캡처 완료: {output_path}")
                return True
        except Exception as e:
            print(f"[CAM_DEBUG] 명령어({cmd[0]}) 실행 중 오류: {e}")
            pass

    print("[CAM_DEBUG] [FATAL] 카메라 캡처에 실패했습니다.")
    return False


# ==========================================
# 7. 메인 통합 관제 및 상태 기계 구동 루프
# ==========================================
def main():
    print("=" * 60)
    print("AIoT 스마트 분리수거 시스템 (Lightweight API Client)")
    print("=" * 60)
    
    lcd = I2CLCD(address=LCD_ADDRESS, bus_num=I2C_BUS)
    lcd.set_message("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
    lcd.set_message("   INITIAL...   ", lcd.LCD_LINE_2)
    time.sleep(1.0)

    current_state = STATE_IDLE
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
            
            # [상태 1] STATE_IDLE
            if current_state == STATE_IDLE:
                if current_time - last_dht_time >= 3.0:
                    temp, hum, status = read_dht11_detailed()
                    if status == "SUCCESS":
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 내부 온도: {temp}°C | 습도: {hum}%")
                    last_dht_time = current_time
                
                dist = get_ultrasonic_distance()
                if 0.0 < dist <= 7.0:
                    print(f"\n[트리거 작동] {dist}cm에 사용자 감지! 카메라 캡처를 실행합니다.")
                    current_state = STATE_SCANNING

            # [상태 2] STATE_SCANNING: 촬영 및 API 서버 전송
            elif current_state == STATE_SCANNING:
                lcd.clear()
                lcd.set_message(" USER DETECTED! ", lcd.LCD_LINE_1)
                lcd.set_message("  CAPTURING...  ", lcd.LCD_LINE_2)
                
                # 가벼워진 캡처 함수 호출
                ret = capture_single_frame(TEMP_IMAGE_PATH)
                if not ret:
                    print(" [SYSTEM_ALERT] 카메라 이미지 프레임을 가져오지 못했습니다.")
                    current_state = STATE_RESULT_ERROR
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
                            success_item_name = detected_item
                        else:
                            print(f" -> 탐지 실패: 물체를 감지했으나 신뢰도({max_conf * 100:.1f}%)가 기준 미달입니다.")
                            current_state = STATE_RESULT_ERROR
                            error_reason = "LOW_CONFIDENCE"
                    else:
                        print(" -> 탐지 실패: 서버에서 예측 데이터가 오지 않았습니다.")
                        current_state = STATE_RESULT_ERROR
                        error_reason = "NO_OBJECT"

                except requests.exceptions.RequestException as e:
                    print(f" [API_ERROR] 서버 통신 실패: {e}")
                    current_state = STATE_RESULT_ERROR
                    error_reason = "SYSTEM_FAULT"

                state_entry_time = time.time()
                result_displayed = False

            # [상태 3] STATE_RESULT_SUCCESS: 커스텀 클래스 가이드 제공
            elif current_state == STATE_RESULT_SUCCESS:
                if not result_displayed:
                    lcd.clear()
                    if success_item_name == "plastico":
                        lcd.set_message("PLASTIC BOTTLE", lcd.LCD_LINE_1)
                        lcd.set_message("REMOVE CAP&LABEL", lcd.LCD_LINE_2)
                        print("가이드: [플라스틱] 비닐 라벨과 플라스틱 뚜껑을 완전히 떼어내고 압착하세요.")
                    elif success_item_name == "metal":
                        lcd.set_message("CAN & METAL WST", lcd.LCD_LINE_1)
                        lcd.set_message("EMPTY & FLATTEN", lcd.LCD_LINE_2)
                        print("가이드: [캔/메탈] 내부 잔여물을 깨끗이 비우고 찌그러뜨리세요.")
                    elif success_item_name == "papel_y_carton":
                        lcd.set_message("PAPER / BOX WST", lcd.LCD_LINE_1)
                        lcd.set_message("REMOVE TAPE&FOLD", lcd.LCD_LINE_2)
                        print("가이드: [종이/박스류] 비닐 테이프와 이물질을 뜯고 평평하게 접으세요.")
                    elif success_item_name == "vidrio":
                        lcd.set_message("GLASS BOTTLE", lcd.LCD_LINE_1)
                        lcd.set_message("RINSE WITH WATER", lcd.LCD_LINE_2)
                        print("가이드: [유리병] 내용물을 헹군 뒤 깨지지 않게 주의하여 배출하세요.")
                    elif success_item_name == "organico":
                        lcd.set_message("ORGANIC WASTE", lcd.LCD_LINE_1)
                        lcd.set_message("DRAIN WATER OUT", lcd.LCD_LINE_2)
                        print("가이드: [음식물/유기물] 물기를 완전히 제거한 후 전용 수거함에 버리세요.")
                    else:
                        lcd.set_message("GENERAL TRASH", lcd.LCD_LINE_1)
                        lcd.set_message("STANDARD DISPOSE", lcd.LCD_LINE_2)
                        
                    result_displayed = True

                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    lcd.set_message("PLACE WASTE FIRST", lcd.LCD_LINE_1)
                    lcd.set_message("APPROACH TO TRIG", lcd.LCD_LINE_2)
                    print(" -> 시스템 상태 복원 완료. [현재 상태: IDLE]\n")
                    current_state = STATE_IDLE

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

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n사용자에 의해 시스템이 안전 종료됩니다.")
    finally:
        lcd.clear()

if __name__ == "__main__":
    main()