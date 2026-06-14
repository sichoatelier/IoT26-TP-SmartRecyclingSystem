import os
import sys
import time
from datetime import datetime
import gpiod
from gpiod.line import Direction, Value  # gpiod v2.x의 표준 방향 및 값 정의 임포트
from smbus2 import SMBus
import cv2  # 컨테이너 환경에서 안정적이고 가벼운 OpenCV 임포트
from ultralytics import YOLO  # YOLOv8 추론 및 NCNN 가속화를 위한 라이브러리

# ==========================================
# 1. 시스템 상태 기계 (State Machine) 상태 정의
# ==========================================
STATE_IDLE = "IDLE"              # 대기 및 온습도 모니터링 상태 (사용자 배출 대기)
STATE_SCANNING = "SCANNING"      # 초음파 트리거 감지 후 카메라 촬영 및 YOLO 추론 상태
STATE_RESULT_SUCCESS = "SUCCESS"  # 탐지 성공 및 분류 가이드 제공 상태
STATE_RESULT_ERROR = "ERROR"      # 탐지 실패 (객체 없음, 신뢰도 미달, HW 오류) 상태

# ==========================================
# 2. 시스템 하드웨어 핀 및 설정 정의
# ==========================================
CHIP_PATH = '/dev/gpiochip4'  # 라즈베리파이 5 기본 GPIO 디바이스 절대 경로

# 초음파 센서 (HC-SR04) 핀 - 사용자 트리거용
TRIG_PIN = 23  # GPIO 23 (Pin 16)
ECHO_PIN = 24  # GPIO 24 (Pin 18)

# 온습도 센서 (DHT11) 핀 - 내부 위생 상태 주기 감시용
DHT_PIN = 17   # GPIO 17 (Pin 11)

# I2C LCD 주소 및 버스 번호
LCD_ADDRESS = 0x27  # i2cdetect 결과에 따라 0x3F 등으로 변경 가능
I2C_BUS = 1

# 파일 및 AI 모델 저장 경로 설정
SAVE_DIR = "/home/iot-team5/Desktop"
if not os.path.exists(SAVE_DIR):
    SAVE_DIR = os.getcwd()  # 경로가 없으면 현재 작업 폴더에 저장

PT_MODEL_PATH = "yolo26n.pt"            # 베이스 PyTorch 가중치 모델
NCNN_MODEL_DIR = "yolo26n_ncnn_model"  # 초고속 NCNN 가속화 컴파일 모델 디렉토리
TEMP_IMAGE_PATH = os.path.join(SAVE_DIR, "captured_waste.jpg")

CONFIDENCE_THRESHOLD = 0.5  # 인공지능 분석 신뢰도 커트라인 (50%)

# ==========================================
# 3. I2C 16x2 LCD 드라이버 클래스
# ==========================================
class I2CLCD:
    def __init__(self, address=0x27, bus_num=1):
        self.address = address
        try:
            self.bus = SMBus(bus_num)
        except Exception as e:
            print(f"[경고] I2C 버스를 열 수 없음. LCD가 작동하지 않음. ({e})")
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
# 4. 온습도 센서 (DHT11) 나노초 정밀 수집 함수
# ==========================================
def read_dht11_detailed():
    """도커 컨테이너 지연을 극복하기 위한 나노초 단위 절대 시간 변동 측정."""
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
# 5. 검증된 초음파 센서 (HC-SR04) 거리 측정 함수
# ==========================================
def get_ultrasonic_distance():
    """
    gpiod v2.x request_lines 방식을 기반으로 초음파 센서로부터 거리를 계산하여 cm 단위로 반환.
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
            
            # 2. Trigger 핀에 10us of High(1) 펄스 인가
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
# 6. AI 모델 라이프사이클 초기화 함수
# ==========================================
def initialize_ai_model():
    """
    NCNN 기반 가속 모델을 우선적으로 로드하고, 없을 경우 PT 모델을 컴파일하여 내보냅니다.
    """
    print("\n[AI 모델 초기화] 가중치 로드 시도 중...")
    if os.path.exists(NCNN_MODEL_DIR):
        print(f" -> NCNN 가속 모델 발견: '{NCNN_MODEL_DIR}' 디렉토리 로드 완료.")
        return YOLO(NCNN_MODEL_DIR)
        
    print(f" -> NCNN 모델을 찾을 수 없어 베이스 모델 '{PT_MODEL_PATH}'로 컴파일을 준비합니다.")
    if not os.path.exists(PT_MODEL_PATH):
        print(f" -> 로컬에 '{PT_MODEL_PATH}'가 존재하지 않습니다. 라이브 다운로드를 구성합니다.")
        
    try:
        model = YOLO(PT_MODEL_PATH)
        print(" -> 라즈베리파이 5 엣지 맞춤형 NCNN 가속 포맷으로 변환 중... (수 분 소요)")
        model.export(format="ncnn")
        print(" -> NCNN 가속화 변환 성공!")
        return YOLO(NCNN_MODEL_DIR)
    except Exception as e:
        print(f"AI 모델 로드 실패: {e}")
        return None

# ==========================================
# 7. 메인 통합 관제 및 상태 기계 구동 루프
# ==========================================
def main():
    print("=" * 60)
    print("AIoT 스마트 분리수거 시스템 - OpenCV V4L2 가속 포팅 버전")
    print("=" * 60)
    
    # 7-1. LCD 하드웨어 초기화
    lcd = I2CLCD(address=LCD_ADDRESS, bus_num=I2C_BUS)
    lcd.display_text("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
    lcd.display_text("   INITIAL...   ", lcd.LCD_LINE_2)
    time.sleep(1.0)

    # 7-2. AI 가중치 모델 로드
    yolo_model = initialize_ai_model()
    if not yolo_model:
        lcd.display_text("  AI MODEL ERR  ", lcd.LCD_LINE_2)
        sys.exit(1)

    # 7-3. OpenCV 기반 V4L2 비디오 스트림 초기화 (도커 친화 백엔드)
    print("카메라 장치(OpenCV V4L2) 초기화 중...")
    try:
        # V4L2 드라이버를 통해 /dev/video0에 직접 바인딩
        cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise IOError("카메라 노드를 열 수 없습니다. 연결 상태를 확인하세요.")
            
        # YOLO 이미지 분석 해상도로 최적화 설정
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        print("OpenCV 카메라 구동 완료.")
    except Exception as e:
        print(f"카메라 제어 실패: {e}")
        lcd.display_text(" CAMERA ERROR!  ", lcd.LCD_LINE_2)
        sys.exit(1)

    # 기본 상태 설정
    current_state = STATE_IDLE
    last_dht_time = 0      # 온습도 출력 제한용 타이머 (3초 주기)
    
    # 비차단(Non-blocking) 타이밍 제어용 전역 변수들
    state_entry_time = 0
    result_displayed = False
    success_item_name = ""
    error_reason = ""
    
    # 쓰레기를 먼저 배치한 후 센서에 접근하는 사용자 행동 유도 가이드 명시
    lcd.display_text("PLACE WASTE FIRST", lcd.LCD_LINE_1)
    lcd.display_text("APPROACH TO TRIG", lcd.LCD_LINE_2)
    print("\n상태 기계 구동 엔진 가동 중... [현재 상태: IDLE]")
    print(" -> 시나리오 가이드: 쓰레기를 놓고 센서에 접근해 주세요.")

    try:
        while True:
            current_time = time.time()
            
            # ==========================================
            # [상태 1] STATE_IDLE: 대기 상태 제어 흐름
            # ==========================================
            if current_state == STATE_IDLE:
                # 3초 주기로 온습도를 정밀 감시하여 위생 환경을 감지 및 콘솔에 로깅
                if current_time - last_dht_time >= 3.0:
                    temp, hum, status = read_dht11_detailed()
                    now_str = datetime.now().strftime('%H:%M:%S')
                    if status == "SUCCESS":
                        print(f"[{now_str}] 내부 온도: {temp}°C | 내부 습도: {hum}%")
                    else:
                        print(f"[{now_str}] 온습도 감지 대기 중 (원인: {status})")
                    last_dht_time = current_time
                
                # 초음파를 통해 쓰레기가 아닌 '사용자의 접근(손/몸)' 감지
                dist = get_ultrasonic_distance()
                if 0.0 < dist <= 7.0:
                    print(f"\n[사용자 접근 감지] {dist}cm에 사용자 감지! 즉시 카메라 캡처를 실행합니다.")
                    current_state = STATE_SCANNING

            # ==========================================
            # [상태 2] STATE_SCANNING: 사용자 트리거로 즉각 촬영 및 AI 분석 (OpenCV 버전)
            # ==========================================
            elif current_state == STATE_SCANNING:
                lcd.clear()
                lcd.display_text(" USER DETECTED! ", lcd.LCD_LINE_1)
                lcd.display_text("  CAPTURING...  ", lcd.LCD_LINE_2)
                
                print(" -> 사용자 접근 조건 만족: 카메라 촬영 즉시 트리거 실행...")
                try:
                    # OpenCV 드라이버의 이전 버퍼 프레임들을 밀어내어 가장 최신 프레임을 수집합니다.
                    for _ in range(5):
                        cap.read()
                        
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        raise IOError("프레임 수집 실패")
                        
                    # [특수 처리 이식] 기존 Transform(180)의 역할을 OpenCV 하드웨어 가속 회전 연산으로 완벽하게 대체
                    frame = cv2.rotate(frame, cv2.ROTATE_180)
                    
                    # 캡처 파일을 YOLO 분석용 지정 경로에 동적 저장
                    cv2.imwrite(TEMP_IMAGE_PATH, frame)
                    print(f" -> [캡처 성공] 임시 이미지 보관 완료: {TEMP_IMAGE_PATH}")
                    
                    # 캡처 직후 분석 안내 메시지 전환
                    lcd.clear()
                    lcd.display_text("  CAPTURED OK!  ", lcd.LCD_LINE_1)
                    lcd.display_text("  ANALYZING...  ", lcd.LCD_LINE_2)
                    
                except Exception as e:
                    print(f"카메라 캡처 중 하드웨어 장애 발생: {e}")
                    current_state = STATE_RESULT_ERROR
                    error_reason = "SYSTEM_FAULT"
                    state_entry_time = time.time()
                    result_displayed = False
                    continue
                
                print(" -> YOLO NCNN 엣지 인공지능 패킷 분석 작동...")
                try:
                    results = yolo_model(TEMP_IMAGE_PATH, verbose=False)
                    detected_item = None
                    max_conf = 0.0
                    
                    if len(results) > 0:
                        result = results[0]
                        
                        # [핵심 변경] YOLO 이미지 분류(Classification) 모델인 경우 (result.probs 속성 존재)
                        if hasattr(result, 'probs') and result.probs is not None:
                            class_id = int(result.probs.top1)
                            max_conf = float(result.probs.top1conf)
                            detected_item = result.names[class_id].lower()
                            
                        # [하이브리드 호환 예비용] YOLO 객체 탐지(Detection) 모델인 경우 (result.boxes 속성 존재)
                        elif hasattr(result, 'boxes') and result.boxes is not None and len(result.boxes) > 0:
                            for box in result.boxes:
                                conf = float(box.conf[0])
                                if conf > max_conf:
                                    max_conf = conf
                                    class_id = int(box.cls[0])
                                    detected_item = result.names[class_id].lower()
                    
                    if detected_item and max_conf >= CONFIDENCE_THRESHOLD:
                        print(f" -> 탐지 성공: '{detected_item}' (신뢰도: {max_conf * 100:.1f}%)")
                        current_state = STATE_RESULT_SUCCESS
                        success_item_name = detected_item
                        state_entry_time = time.time()
                        result_displayed = False
                    else:
                        # 신뢰도 미달 혹은 객체 없음 분류
                        if len(results) > 0 and (
                            (hasattr(results[0], 'probs') and results[0].probs is not None) or 
                            (hasattr(results[0], 'boxes') and results[0].boxes is not None and len(results[0].boxes) > 0)
                        ):
                            print(f" -> 탐지 실패: 물체를 감지했으나 신뢰도({max_conf * 100:.1f}%)가 기준선(50%) 미만입니다.")
                            current_state = STATE_RESULT_ERROR
                            error_reason = "LOW_CONFIDENCE"
                        else:
                            print(" -> 탐지 실패: 배출 영역 내에 쓰레기 데이터가 감지되지 않았습니다.")
                            current_state = STATE_RESULT_ERROR
                            error_reason = "NO_OBJECT"
                        state_entry_time = time.time()
                        result_displayed = False
                            
                except Exception as e:
                    print(f"YOLO 인퍼런스 엔진 가동 중 심각한 예외 발생: {e}")
                    current_state = STATE_RESULT_ERROR
                    error_reason = "SYSTEM_FAULT"
                    state_entry_time = time.time()
                    result_displayed = False

            # ==========================================
            # [상태 3] STATE_RESULT_SUCCESS: 탐지 성공 및 종류별 '올바른 전처리 가이드' 제공 (비차단형 구현)
            # ==========================================
            elif current_state == STATE_RESULT_SUCCESS:
                if not result_displayed:
                    lcd.clear()
                    if "bottle" in success_item_name or "plastic" in success_item_name:
                        lcd.display_text("PLASTIC BOTTLE", lcd.LCD_LINE_1)
                        lcd.display_text("REMOVE CAP&LABEL", lcd.LCD_LINE_2)
                        print("가이드: [플라스틱] 비닐 라벨과 플라스틱 뚜껑을 완전히 떼어내고 압착해주세요.")
                    elif "can" in success_item_name or "metal" in success_item_name:
                        lcd.display_text("CAN & METAL WST", lcd.LCD_LINE_1)
                        lcd.display_text("EMPTY & FLATTEN", lcd.LCD_LINE_2)
                        print("가이드: [캔/메탈] 내부 잔여물을 깨끗이 비우고 찌그러뜨려주세요.")
                    elif "paper" in success_item_name or "cardboard" in success_item_name:
                        lcd.display_text("PAPER / BOX WST", lcd.LCD_LINE_1)
                        lcd.display_text("REMOVE TAPE&FOLD", lcd.LCD_LINE_2)
                        print("가이드: [종이류] 박스의 비닐 테이프와 이물질을 완전히 뜯고 평평하게 접어주세요.")
                    elif "glass" in success_item_name:
                        lcd.display_text("GLASS BOTTLE", lcd.LCD_LINE_1)
                        lcd.display_text("RINSE WITH WATER", lcd.LCD_LINE_2)
                        print("가이드: [유리병] 내용물을 가볍게 헹군 뒤 깨지지 않도록 배출해 주세요.")
                    else:
                        lcd.display_text("GENERAL TRASH", lcd.LCD_LINE_1)
                        lcd.display_text("PUT IN THE BIN", lcd.LCD_LINE_2)
                        print("가이드: [일반/기타] 별도 재활용이 곤란한 재질이므로 수거함에 그대로 배출해 주세요.")
                    
                    print(" -> 시스템 배출 가이드 제공 시작 (비차단식 5초 버퍼 가동)")
                    result_displayed = True

                # 전시 중에도 온습도 센서 지속 로깅
                if current_time - last_dht_time >= 3.0:
                    temp, hum, status = read_dht11_detailed()
                    now_str = datetime.now().strftime('%H:%M:%S')
                    if status == "SUCCESS":
                        print(f"[{now_str}] 내부 온도: {temp}°C | 내부 습도: {hum}%")
                    else:
                        print(f"[{now_str}] 온습도 감지 대기 중 (원인: {status})")
                    last_dht_time = current_time

                # 비차단 타임 계산: 5초가 경과하면 안전하게 IDLE로 복구
                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    lcd.display_text("PLACE WASTE FIRST", lcd.LCD_LINE_1)
                    lcd.display_text("APPROACH TO TRIG", lcd.LCD_LINE_2)
                    print(" -> 시스템 상태 복원 완료. [현재 상태: IDLE]\n")
                    current_state = STATE_IDLE

            # ==========================================
            # [상태 4] STATE_RESULT_ERROR: 탐지 실패 사유별 세부 제어 흐름 (비차단형 구현)
            # ==========================================
            elif current_state == STATE_RESULT_ERROR:
                if not result_displayed:
                    lcd.clear()
                    # 에러 원인에 따른 차별화 메시지 출력 구현
                    if error_reason == "NO_OBJECT":
                        lcd.display_text("DETECTION ERROR ", lcd.LCD_LINE_1)
                        lcd.display_text("TRY AGAIN (EMPTY)", lcd.LCD_LINE_2)
                        print("시스템 피드백: 촬영본에 물체가 정상적으로 식별되지 않습니다. 쓰레기가 올바르게 배치되었는지 점검하세요.")
                    elif error_reason == "LOW_CONFIDENCE":
                        lcd.display_text("DETECTION ERROR ", lcd.LCD_LINE_1)
                        lcd.display_text("UNRECOGNIZED WT ", lcd.LCD_LINE_2)
                        print("시스템 피드백: 쓰레기 분리 배출 종류 식별의 불확실성이 큽니다. 다시 시도하세요.")
                    elif error_reason == "SYSTEM_FAULT":
                        lcd.display_text("  SYSTEM ERROR  ", lcd.LCD_LINE_1)
                        lcd.display_text("CHECK CAMERA/HW ", lcd.LCD_LINE_2)
                        print("시스템 경고: 하드웨어 모듈 및 I/O 핀 결선 장애 의심. 연결을 진단하세요.")
                    
                    print(" -> 에러 리포트 가이드 제공 시작 (비차단식 5초 버퍼 가동)")
                    result_displayed = True

                # 전시 중에도 온습도 정밀 지속 로깅
                if current_time - last_dht_time >= 3.0:
                    temp, hum, status = read_dht11_detailed()
                    now_str = datetime.now().strftime('%H:%M:%S')
                    if status == "SUCCESS":
                        print(f"[{now_str}] 내부 온도: {temp}°C | 내부 습도: {hum}%")
                    else:
                        print(f"[{now_str}] 온습도 감지 대기 중 (원인: {status})")
                    last_dht_time = current_time

                # 5초 경과 시 IDLE로 복구
                if current_time - state_entry_time >= 5.0:
                    lcd.clear()
                    lcd.display_text("PLACE WASTE FIRST", lcd.LCD_LINE_1)
                    lcd.display_text("APPROACH TO TRIG", lcd.LCD_LINE_2)
                    print(" -> 예외 복구 및 센서 대기 모드 진입. [현재 상태: IDLE]\n")
                    current_state = STATE_IDLE

            # 루프 사이클 CPU 점유율 과다 차단용 마이크로 지연
            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n사용자에 의해 시스템이 안전 종료됩니다. 모든 자원을 정상 해제합니다.")
    finally:
        try:
            cap.release()  # OpenCV 비디오 자원 반환
        except:
            pass
        lcd.clear()

if __name__ == "__main__":
    main()