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

# 상태 표시 LED 핀 설정 (상태등)
LED_PIN_R = 19
LED_PIN_Y = 13
LED_PIN_G = 6

# 3색 RGB LED 핀 설정 (조명용)
RGB_PIN_R = 16
RGB_PIN_G = 20
RGB_PIN_B = 21

# 서보모터 핀 설정 (분리배출 판 기울임용)
SERVO_PIN = 25

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


def debug_print(tag, message):
    if SERVO_DEBUG:
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}][{tag}] {message}", flush=True)

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
# 6. 온습도 센서 (DHT11) 함수
# ==========================================
def read_dht11_detailed():
    # ... (기존 코드와 동일) ...
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
# 7. 초음파 센서 (HC-SR04) 함수
# ==========================================
def get_ultrasonic_distance():
    # ... (기존 코드와 동일) ...
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
    # ... (기존 코드와 동일) ...
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
# 9. 메인 통합 관제 및 상태 기계 구동 루프
# ==========================================
def main():
    print("=" * 60)
    print("AIoT 스마트 분리수거 시스템 (Camera + Servo + LED)")
    print("=" * 60)
    
    lcd = I2CLCD(address=LCD_ADDRESS, bus_num=I2C_BUS)
    led = LEDController(CHIP_PATH, LED_PIN_R, LED_PIN_Y, LED_PIN_G, RGB_PIN_R, RGB_PIN_G, RGB_PIN_B)
    servo = ServoController(CHIP_PATH, SERVO_PIN)
    
    lcd.set_message("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
    lcd.set_message("   INITIAL...   ", lcd.LCD_LINE_2)
    time.sleep(1.0)

    current_state = STATE_IDLE
    led.set_state(STATE_IDLE) 
    
    # 시스템 시작 시 서보모터를 기본 수평 각도(90도)로 맞춤
    debug_print("SERVO", "시스템 시작 시 기본 위치(90도)로 이동")
    servo.set_angle(90, duration=1.0)
    
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
                if current_time - last_dht_time >= 3.0:
                    temp, hum, status = read_dht11_detailed()
                    if status == "SUCCESS":
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 내부 온도: {temp}°C | 습도: {hum}%")
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

            # [상태 3] STATE_RESULT_SUCCESS
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
                    
                    # 판 기울이기 (동작 시간 약 1초 소요)
                    servo.set_angle(target_angle, duration=1.0)
                    result_displayed = True

                # 상태 진입 후 5초 경과 시 다시 대기 상태로 복귀
                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    lcd.set_message("PLACE WASTE FIRST", lcd.LCD_LINE_1)
                    lcd.set_message("APPROACH TO TRIG", lcd.LCD_LINE_2)
                    print(" -> 배출 완료. 판을 기본 각도(90도)로 복귀합니다.\n")
                    
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

if __name__ == "__main__":
    main()