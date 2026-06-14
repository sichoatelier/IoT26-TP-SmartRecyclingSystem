import time
import gpiod
from datetime import datetime

# 라즈베리파이 5 기본 GPIO 설정 (절대 경로로 지정하여 컨테이너 인식 오류 방지)
CHIP = '/dev/gpiochip4'  # 시스템 환경에 따라 '/dev/gpiochip0' 등으로 변경 가능
DHT_PIN = 17  # GPIO 17 (Pin 11)

def read_dht11_detailed():
    """
    gpiod v2.x 규격을 사용하며, 
    도커 컨테이너 환경의 가상화 지연을 극복하기 위해 나노초(ns) 단위 절대 시간 변동 측정을 수행합니다.
    """
    timestamps = []
    values = []
    
    # 1. 칩을 열고 처음에는 출력(OUTPUT) 모드로 시작 신호 준비
    try:
        with gpiod.request_lines(
            CHIP,
            consumer="DHT11",
            config={
                DHT_PIN: gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT, 
                    output_value=gpiod.line.Value.ACTIVE
                )
            }
        ) as lines:
            
            # 시작 신호 보내기: 최소 18ms 동안 Low 유지 후 High로 복귀
            lines.set_value(DHT_PIN, gpiod.line.Value.INACTIVE)
            time.sleep(0.018)
            lines.set_value(DHT_PIN, gpiod.line.Value.ACTIVE)
            time.sleep(0.00004) # 40us 대기
            
            # 2. 신호 받기 준비 (gpiod v2.x 동적 입력 모드 INPUT 전환으로 타이밍 극대화)
            lines.reconfigure_lines(
                config={
                    DHT_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)
                }
            )
            
            # 3. 절대 나노초 변동 기록 루프 (가상화 환경 극복 핵심 비책)
            # 상태가 변할 때의 정확한 나노초 타임스탬프를 다이렉트로 매핑합니다.
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
                    if len(values) > 100:  # 응답+데이터 포함 최대 약 84개의 transition 감지 시 조기 탈출
                        break
    except Exception as e:
        return None, None, f"ERR_GPIO_ACCESS: GPIO 라이브러리 접근 실패 ({str(e)})"
                
    # [디버그 1] 물리적 응답 신호 존재 확인
    if len(values) < 10:
        return None, None, "ERR_NO_RESPONSE: 센서 전원 무반응 (VCC/GND 단선 또는 접촉 불량을 점검해 주세요)"
        
    # 4. 수집된 타임스탬프에서 HIGH 레벨 유지 시간(microsecond) 정밀 계산
    high_durations = []
    for i in range(len(values) - 1):
        if values[i] == 1:  # HIGH 상태인 구간만 골라냄
            # (HIGH 종료 시점 타임스탬프 - HIGH 시작 시점 타임스탬프) / 1000 = 마이크로초(us)
            duration_us = (timestamps[i+1] - timestamps[i]) / 1000.0
            high_durations.append(duration_us)
            
    # [디버그 2] 수집된 데이터 패킷 수 검증
    # 정상 신호인 경우 최소 1개(프리앰블) + 40개(데이터 비트) = 41개 내외의 HIGH 구간이 검출됩니다.
    if len(high_durations) < 40:
        return None, None, f"ERR_INCOMPLETE_DATA: 유효 데이터 패킷 부족 (검출된 HIGH 구간: {len(high_durations)}개 / 최소 40개 필요)"
        
    # 패킷 오차를 방지하기 위해 가장 뒤쪽에서 생성된 최종 40개의 비트 신호만 정확하게 슬라이싱
    bit_signals = high_durations[-40:]
    
    # 데이터 비트가 '0'인지 '1'인지 판단할 중간 임계값 계산 (보통 28us와 70us의 중간인 48us 내외 형성됨)
    avg_len = sum(bit_signals) / 40.0
    
    # 5. 40비트 데이터 복원 (습도 정수, 습도 소수, 온도 정수, 온도 소수, 체크섬)
    data_bytes = [0, 0, 0, 0, 0]
    for i in range(40):
        byte_idx = i // 8
        data_bytes[byte_idx] <<= 1
        if bit_signals[i] > avg_len:
            data_bytes[byte_idx] |= 1
            
    # 6. 체크섬(데이터 무결성) 최종 검증
    checksum = (data_bytes[0] + data_bytes[1] + data_bytes[2] + data_bytes[3]) & 0xFF
    if data_bytes[4] == checksum:
        humidity = data_bytes[0] + (data_bytes[1] * 0.1)
        temperature = data_bytes[2] + (data_bytes[3] * 0.1)
        
        # 비현실적인 온습도 데이터 이상치 예외 필터링 추가
        if humidity > 100.0 or temperature > 80.0:
            return None, None, f"ERR_INVALID_RANGE: 물리 한계 초과 값 검출 (디코딩 습도: {humidity}%, 온도: {temperature}°C)"
            
        return round(temperature, 1), round(humidity, 1), "SUCCESS"
    else:
        detail = f"수집 데이터 [습도={data_bytes[0]}.{data_bytes[1]}%, 온도={data_bytes[2]}.{data_bytes[3]}%] "
        detail += f"수신된 체크섬={data_bytes[4]}, 계산된 체크섬={checksum}"
        return None, None, f"ERR_CHECKSUM: 데이터 전송 오류 (나노초 펄스 타이밍 깨짐 / {detail})"

if __name__ == '__main__':
    print("=" * 60)
    print("라즈베리파이 5 DHT11 도커 안정형 정밀 진단 툴 (v2.x 통합)")
    print("=" * 60)
    
    try:
        while True:
            current_time = datetime.now().strftime('%H:%M:%S')
            temp, hum, status = read_dht11_detailed()
            
            if status == "SUCCESS":
                print(f"[{current_time}] 온도: {temp}°C | 습도: {hum}% | 상태: 정상")
            else:
                print(f"[{current_time}] 측정 실패 리포트")
                print(f"  └─ 원인 분석: {status}")
                print("-" * 60)
                
            time.sleep(3.0)  # DHT11 센서 안정화를 위해 3.0초 간격으로 측정
    except KeyboardInterrupt:
        print("\n진단 측정을 종료합니다.")