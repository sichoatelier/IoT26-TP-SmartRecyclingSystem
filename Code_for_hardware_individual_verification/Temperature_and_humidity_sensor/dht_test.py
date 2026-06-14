import time
import gpiod

# 라즈베리파이 5 기본 GPIO 설정
CHIP = 'gpiochip4'
DHT_PIN = 17  # GPIO 17 (Pin 11)

def read_dht11():
    """gpiod를 이용하여 DHT11 센서로부터 온도와 습도를 읽어옵니다."""
    pulses = []
    
    with gpiod.Chip(CHIP) as chip:
        line = chip.get_line(DHT_PIN)
        
        # 1. 시작 신호 보내기 (출력 모드)
        line.request(consumer="DHT11", type=gpiod.LINE_REQ_DIR_OUT)
        line.set_value(0)
        time.sleep(0.018)  # 최소 18ms 동안 Low 유지
        line.set_value(1)
        time.sleep(0.00004) # 40us 동안 High 유지
        
        # 2. 신호 받기 준비 (입력 모드 전환)
        line.release()
        line.request(consumer="DHT11", type=gpiod.LINE_REQ_DIR_IN)
        
        # 3. 데이터 펄스 감지 (최대 1000번 루프 돌며 신호 변화 기록)
        # 통신 속도가 매우 빠르므로 루프를 빠르게 돌아 전압 변화 상태를 원시 데이터로 수집함
        raw_values = []
        timeout = time.time() + 0.1
        while time.time() < timeout:
            raw_values.append(line.get_value())
            if len(raw_values) > 1500:
                break
                
    # 4. 수집된 원시 전압 데이터 분석 (Low->High 변화 지점 검출)
    transitions = []
    for i in range(1, len(raw_values)):
        if raw_values[i] != raw_values[i-1]:
            transitions.append(i)
            
    # 통신 패킷 길이가 너무 짧으면 오류 처리 (정상 패킷은 대략 80개 이상의 변화점이 있음)
    if len(transitions) < 80:
        return None, None
        
    # 5. 각 데이터 비트의 길이 계산 및 0/1 판별
    # 전압 변동 폭의 지속 시간을 비교하여 이진 데이터를 추출
    bit_lengths = []
    # 데이터는 80마이크로초 대기 이후 3번째 변화점부터 실데이터 시작
    start_index = 4 if len(transitions) >= 84 else 2
    
    for i in range(start_index, len(transitions) - 1, 2):
        if i + 1 < len(transitions):
            high_len = transitions[i+1] - transitions[i]
            bit_lengths.append(high_len)
            
    if len(bit_lengths) < 40:
        return None, None
        
    # 데이터 비트가 '0'인지 '1'인지 판단할 중간 임계값 계산
    avg_len = sum(bit_lengths[:40]) / 40
    
    # 40비트 데이터 복원 (습도 정수, 습도 소수, 온도 정수, 온도 소수, 체크섬)
    data_bytes = [0, 0, 0, 0, 0]
    for i in range(40):
        byte_idx = i // 8
        data_bytes[byte_idx] <<= 1
        if bit_lengths[i] > avg_len:
            data_bytes[byte_idx] |= 1
            
    # 6. 체크섬(데이터 무결성) 검증
    checksum = (data_bytes[0] + data_bytes[1] + data_bytes[2] + data_bytes[3]) & 0xFF
    if data_bytes[4] == checksum:
        humidity = data_bytes[0] + (data_bytes[1] * 0.1)
        temperature = data_bytes[2] + (data_bytes[3] * 0.1)
        return round(temperature, 1), round(humidity, 1)
    else:
        return None, None  # 체크섬 오류 시 에러 처리

if __name__ == '__main__':
    print("라즈베리파이 5 온습도 측정 테스트 시작 (Ctrl+C로 종료)...")
    try:
        while True:
            temp, hum = read_dht11()
            if temp is not None and hum is not None:
                print(f"현재 온도: {temp}°C | 현재 습도: {hum}%")
            else:
                # DHT11 센서는 하드웨어 특성상 간헐적으로 통신 노이즈가 발생하므로 재시도 처리
                print("센서 데이터 읽기 실패 (재시도 중...)")
            time.sleep(2.0)  # DHT11은 센서 내부 갱신 주기를 위해 최소 2초의 대기 시간 필요
    except KeyboardInterrupt:
        print("\n측정 종료")