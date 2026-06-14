import os
import sys
import time
from datetime import datetime
import gpiod
from gpiod.line import Direction, Value  # gpiod v2.x의 표준 방향 및 값 정의 임포트
from smbus2 import SMBus
from picamera2 import Picamera2
from libcamera import Transform  # 하드웨어 GPU 180도 회전 필수 모듈

# ==========================================
# 1. 시스템 하드웨어 핀 및 설정 정의
# ==========================================
CHIP_PATH = '/dev/gpiochip4'  # 라즈베리파이 5 기본 GPIO 디바이스 절대 경로

# 초음파 센서 (HC-SR04) 핀
TRIG_PIN = 23  # GPIO 23 (Pin 16)
ECHO_PIN = 24  # GPIO 24 (Pin 18)

# 온습도 센서 (DHT11) 핀
DHT_PIN = 17   # GPIO 17 (Pin 11)

# I2C LCD 주소 및 버스 번호
LCD_ADDRESS = 0x27  # i2cdetect 결과에 따라 0x3F 등으로 변경 가능
I2C_BUS = 1

# 파일 저장 경로 설정
SAVE_DIR = "/home/iot-team5/Desktop"
if not os.path.exists(SAVE_DIR):
    SAVE_DIR = os.getcwd()  # 경로가 없으면 현재 폴더에 저장

# ==========================================
# 2. I2C 16x2 LCD 드라이버 클래스
# ==========================================
class I2CLCD:
    def __init__(self, address=0x27, bus_num=1):
        self.address = address
        try:
            self.bus = SMBus(bus_num)
        except Exception as e:
            print(f"[경고] I2C 버스를 열 수 없음. LCD가 작동하지 않음 ({e})")
            self.bus = None
            return
            
        self.LCD_CHR = 1  # 데이터 모드
        self.LCD_CMD = 0  # 명령 모드
        self.LCD_LINE_1 = 0x80  # 첫 번째 줄 시작
        self.LCD_LINE_2 = 0xC0  # 두 번째 줄 시작
        self.LCD_BACKLIGHT = 0x08  # 백라이트 켜기
        self.ENABLE = 0b00000100  # Enable 신호 펄스
        
        # LCD 초기화 시퀀스
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

    def display_text(self, text, line):
        if not self.bus:
            return
        self.lcd_write(line, self.LCD_CMD)
        text = text.ljust(16, " ")
        for char in text[:16]:
            self.lcd_write(ord(char), self.LCD_CHR)

    def clear(self):
        self.lcd_write(0x01, self.LCD_CMD)
        time.sleep(0.005)

# ==========================================
# 3. 온습도 센서 (DHT11) 나노초 정밀 수집 함수
# ==========================================
def read_dht11_detailed():
    """도커 컨테이너 지연을 극복하기 위해 나노초 단위 절대 시간 변동 측정을 수행"""
    timestamps = []
    values = []
    
    try:
        with gpiod.request_lines(
            CHIP_PATH,
            consumer="DHT11",
            config={
                DHT_PIN: gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT, 
                    output_value=gpiod.line.Value.ACTIVE
                )
            }
        ) as lines:
            
            # 시작 신호 인가
            lines.set_value(DHT_PIN, gpiod.line.Value.INACTIVE)
            time.sleep(0.018)
            lines.set_value(DHT_PIN, gpiod.line.Value.ACTIVE)
            time.sleep(0.00004)
            
            # 고속 동적 입력 모드 전환
            lines.reconfigure_lines(
                config={
                    DHT_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)
                }
            )
            
            # 절대 나노초 변동 수집 루프
            get_val = lines.get_value
            pin = DHT_PIN
            
            last_val = get_val(pin).value
            timestamps.append(time.perf_counter_ns())
            values.append(last_val)
            
            timeout_ns = time.perf_counter_ns() + 100000000  # 100ms 타임아웃
            while time.perf_counter_ns() < timeout_ns:
                curr_val = get_val(pin).value
                if curr_val != last_val:
                    timestamps.append(time.perf_counter_ns())
                    values.append(curr_val)
                    last_val = curr_val
                    if len(values) > 100:
                        break
    except Exception as e:
        return None, None, f"GPIO 접근 실패 ({str(e)})"
                
    if len(values) < 10:
        return None, None, "센서 무반응 (연결 확인 요망)"
        
    # HIGH 상태 유지 시간(us) 계산
    high_durations = []
    for i in range(len(values) - 1):
        if values[i] == 1:
            duration_us = (timestamps[i+1] - timestamps[i]) / 1000.0
            high_durations.append(duration_us)
            
    if len(high_durations) < 40:
        return None, None, f"데이터 패킷 부족 (HIGH 구간: {len(high_durations)}개)"
        
    bit_signals = high_durations[-40:]
    avg_len = sum(bit_signals) / 40.0
    
    # 40비트 복원
    data_bytes = [0, 0, 0, 0, 0]
    for i in range(40):
        byte_idx = i // 8
        data_bytes[byte_idx] <<= 1
        if bit_signals[i] > avg_len:
            data_bytes[byte_idx] |= 1
            
    # 체크섬 검증
    checksum = (data_bytes[0] + data_bytes[1] + data_bytes[2] + data_bytes[3]) & 0xFF
    if data_bytes[4] == checksum:
        humidity = data_bytes[0] + (data_bytes[1] * 0.1)
        temperature = data_bytes[2] + (data_bytes[3] * 0.1)
        if humidity > 100.0 or temperature > 80.0:
            return None, None, "센서 오독 (이상 임계값 감지)"
        return round(temperature, 1), round(humidity, 1), "SUCCESS"
    else:
        return None, None, "체크섬 불일치 (데이터 깨짐)"

# ==========================================
# 4. 초음파 센서 (HC-SR04) 거리 측정 함수
# ==========================================
def get_ultrasonic_distance():
    """
    gpiod v2.x request_lines 방식을 기반으로 초음파 센서로부터 거리를 계산하여 cm 단위로 반환합니다.
    기존의 개별 chip 접근 방식 대신 한 번의 세션 내에서 입력/출력 라인을 할당하여 오작동을 차단합니다.
    """
    try:
        with gpiod.request_lines(
            CHIP_PATH,
            consumer="Ultrasonic",
            config={
                TRIG_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                ECHO_PIN: gpiod.LineSettings(direction=Direction.INPUT)
            }
        ) as lines:
            
            # 1. Trigger 핀을 Low(0)로 잠시 안정화
            lines.set_value(TRIG_PIN, Value.INACTIVE)
            time.sleep(0.05)  # 50ms 대기 (통합 대기 지연 최적화)
            
            # 2. Trigger 핀에 10us의 High(1) 펄스 인가
            lines.set_value(TRIG_PIN, Value.ACTIVE)
            time.sleep(0.00001)
            lines.set_value(TRIG_PIN, Value.INACTIVE)
            
            # 3. Echo 핀이 High가 되는 시간 측정
            start_time = time.time()
            timeout = start_time + 1.0  # 1초 타임아웃
            
            while lines.get_value(ECHO_PIN) == Value.INACTIVE:
                start_time = time.time()
                if start_time > timeout:
                    return -1.0
                    
            # 4. Echo 핀이 Low가 되는 시간 측정
            stop_time = time.time()
            timeout = stop_time + 1.0
            while lines.get_value(ECHO_PIN) == Value.ACTIVE:
                stop_time = time.time()
                if stop_time > timeout:
                    return -1.0
                    
            # 5. 왕복 시간 계산 및 거리 환산 (단위: cm)
            duration = stop_time - start_time
            distance = duration * 17150
            
            return round(distance, 1)
            
    except Exception as e:
        print(f"[경고] 초음파 센서 제어 에러: {e}")
        return -1.0

# ==========================================
# 5. 메인 통합 제어 루프
# ==========================================
def main():
    print("=" * 60)
    print("AIoT 스마트 분리수거 시스템 - MVP 단계 1차 통합")
    print("=" * 60)
    
    # LCD 초기화
    lcd = I2CLCD(address=LCD_ADDRESS, bus_num=I2C_BUS)
    lcd.display_text("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
    lcd.display_text("   INITIAL...   ", lcd.LCD_LINE_2)
    time.sleep(1.5)

    # Picamera2 초기화 및 180도 회전 하드웨어 가속 적용
    print("카메라 장치 초기화 중...")
    try:
        pic = Picamera2()
        config = pic.create_preview_configuration(main={"size": (1280, 720)})
        config["transform"] = Transform(180)  # 뒤집혀 장착된 물리 카메라 180도 회전
        pic.configure(config)
        pic.start()
        print("카메라 프리뷰 구동 완료.")
    except Exception as e:
        print(f"카메라 제어 실패: {e}")
        lcd.display_text(" CAMERA ERROR!  ", lcd.LCD_LINE_2)
        sys.exit(1)

    lcd.display_text("   [ READY ]    ", lcd.LCD_LINE_2)
    print("통합 관제 감지 루프 작동 시작...")
    
    capture_count = 0
    last_dht_time = 0  # 온습도 출력 제한용 타이머 (3초 주기)

    try:
        while True:
            current_time = time.time()
            
            # 5-1. 온습도는 비동기식으로 약 3초에 한 번씩 터미널에 로깅
            if current_time - last_dht_time >= 3.0:
                temp, hum, status = read_dht11_detailed()
                now_str = datetime.now().strftime('%H:%M:%S')
                if status == "SUCCESS":
                    print(f"[{now_str}] 내부 온도: {temp}°C | 내부 습도: {hum}% [정상 수집]")
                else:
                    print(f"[{now_str}] 온습도 수집 불가 원인: {status}")
                last_dht_time = current_time
            
            # 5-2. 실시간 초음파 거리 센싱 (검증된 v2.x 기반 함수 호출)
            dist = get_ultrasonic_distance()
            
            # 7cm 이하 감지 시 캡처 흐름 돌입
            if 0.0 < dist <= 7.0:
                print(f"\n[인체 감지] 측정 거리: {dist}cm (임계값 7.0cm 이하)")
                
                # LCD 캡처 가이드 전송
                lcd.clear()
                lcd.display_text("    CAPTURING   ", lcd.LCD_LINE_1)
                lcd.display_text("  Please Wait.. ", lcd.LCD_LINE_2)
                
                capture_count += 1
                save_path = os.path.join(SAVE_DIR, f'captured_waste_{capture_count}.jpg')
                
                print(" -> 이미지 촬영 실행 중...")
                try:
                    # 180도 보정 완료된 스틸컷 동적 저장
                    pic.capture_file(save_path)
                    print(f"촬영 및 저장 성공: {save_path}")
                except Exception as e:
                    print(f"촬영 실패: {e}")
                    
                # 촬영 완료 안내 후 시스템 안정화 및 중복 인식 방지 대기
                lcd.clear()
                lcd.display_text(" CAPTURE COMPLETE", lcd.LCD_LINE_1)
                lcd.display_text("================ ", lcd.LCD_LINE_2)
                
                print(" -> 시스템 쿨다운 버퍼링 진입 (5초 대기)...")
                time.sleep(5.0)
                
                # 대기 화면 원상복구
                lcd.clear()
                lcd.display_text("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
                lcd.display_text("   [ READY ]    ", lcd.LCD_LINE_2)
                print(" -> 시스템 재준비 완료. 대기 중...\n")

            # 루프 사이클 CPU 점유 오버헤드 방지용 마이크로 슬립
            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n사용자에 의해 시스템이 안전 종료됩니다. 자원을 반환합니다.")
    finally:
        try:
            pic.stop()
        except:
            pass
        lcd.clear()

if __name__ == "__main__":
    main()