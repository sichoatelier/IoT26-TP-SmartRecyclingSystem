import time
import gpiod

# 라즈베리파이 5의 기본 GPIO 칩 선택
CHIP = 'gpiochip4' 
TRIGGER_PIN = 23 # GPIO 23
ECHO_PIN = 24    # GPIO 24

def get_distance():
    # GPIO 칩 열기
    with gpiod.Chip(CHIP) as chip:
        # 핀 라인 가져오기
        trig_line = chip.get_line(TRIGGER_PIN)
        echo_line = chip.get_line(ECHO_PIN)
        
        # 방향 설정 (Trig: 출력, Echo: 입력)
        trig_line.request(consumer="Ultrasonic", type=gpiod.LINE_REQ_DIR_OUT)
        echo_line.request(consumer="Ultrasonic", type=gpiod.LINE_REQ_DIR_IN)
        
        # 1. Trigger 핀을 Low로 안정화
        trig_line.set_value(0)
        time.sleep(0.1)
        
        # 2. Trigger 핀에 10us의 High 펄스 인가
        trig_line.set_value(1)
        time.sleep(0.00001)
        trig_line.set_value(0)
        
        # 3. Echo 핀이 High가 되는 시간 측정
        start_time = time.time()
        timeout = start_time + 1.0 # 1초 타임아웃 예외처리
        
        while echo_line.get_value() == 0:
            start_time = time.time()
            if start_time > timeout:
                return -1.0
                
        # 4. Echo 핀이 Low가 되는 시간 측정
        stop_time = time.time()
        timeout = stop_time + 1.0
        while echo_line.get_value() == 1:
            stop_time = time.time()
            if stop_time > timeout:
                return -1.0
                
        # 5. 왕복 시간 계산 및 거리 환산 (단위: cm)
        # 음속: 343m/s -> 34300cm/s -> 왕복이므로 편도는 나누기 2 -> 시간 * 17150
        duration = stop_time - start_time
        distance = duration * 17150
        
        return round(distance, 1)

if __name__ == '__main__':
    print("초음파 센서 거리 측정 시작 (Ctrl+C로 종료)...")
    try:
        while True:
            dist = get_distance()
            if dist >= 0:
                print(f"측정된 거리: {dist} cm")
            else:
                print("센서 신호 타임아웃 발생 (배선 확인)")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n측정 종료")