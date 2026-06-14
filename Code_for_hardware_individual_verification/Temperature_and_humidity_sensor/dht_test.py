import time
import gpiod
from datetime import datetime

# 라즈베리파이 5 기본 GPIO 설정
CHIP = 'gpiochip4'
DHT_PIN = 17  # GPIO 17 (Pin 11)

def read_dht11_detailed():
    """gpiod를 이용하여 DHT11 센서 데이터를 읽고, 실패 시 세부 원인을 문자열로 반환합니다."""
    raw_values = []
    
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
        
        # 3. 데이터 펄스 고속 수집
        timeout = time.time() + 0.1
        while time.time() < timeout:
            raw_values.append(line.get_value())
            if len(raw_values) > 1500:
                break
                
    # 4. 수집된 원시 전압 데이터의 상태 변화(Edge) 검출
    transitions = []
    for i in range(1, len(raw_values)):
        if raw_values[i] != raw_values[i-1]:
            transitions.append(i)
            
    # [디버그 1] 시작 응답 신호 및 물리적 연결 상태 확인
    if len(transitions) < 80:
        if len(transitions) == 0:
            return None, None, f"ERR_NO_RESPONSE: 센서 신호가 전혀 감지되지 않음 (배선 상태/접촉 상태 확인 필요)"
        else:
            return None, None, f"ERR_WEAK_SIGNAL: 신호 상태 변화 부족 (감지된 변화: {len(transitions)}개, 최소 80개 필요. 전압 저하 및 노이즈 의심)"
        
    # 5. 각 데이터 비트의 길이 계산 및 0/1 판별
    bit_lengths = []
    start_index = 4 if len(transitions) >= 84 else 2
    
    for i in range(start_index, len(transitions) - 1, 2):
        if i + 1 < len(transitions):
            high_len = transitions[i+1] - transitions[i]
            bit_lengths.append(high_len)
            
    # [디버그 포인트 2] 데이터 유실 여부 확인
    if len(bit_lengths) < 40:
        return None, None, f"ERR_INCOMPLETE_DATA: 40비트 전체 데이터를 채우지 못했음 (추출된 비트: {len(bit_lengths)}개. 타이밍 불일치 혹은 센서 성능 저하)"
        
    # 데이터 비트가 '0'인지 '1'인지 판단할 중간 임계값 계산
    avg_len = sum(bit_lengths[:40]) / 40
    
    # 40비트 데이터 복원 (습도 정수, 습도 소수, 온도 정수, 온도 소수, 체크섬)
    data_bytes = [0, 0, 0, 0, 0]
    for i in range(40):
        byte_idx = i // 8
        data_bytes[byte_idx] <<= 1
        if bit_lengths[i] > avg_len:
            data_bytes[byte_idx] |= 1
            
    # [디버그 포인트 3] 체크섬(데이터 무결성) 검증
    checksum = (data_bytes[0] + data_bytes[1] + data_bytes[2] + data_bytes[3]) & 0xFF
    if data_bytes[4] == checksum:
        humidity = data_bytes[0] + (data_bytes[1] * 0.1)
        temperature = data_bytes[2] + (data_bytes[3] * 0.1)
        return round(temperature, 1), round(humidity, 1), "SUCCESS"
    else:
        # 오류 상세 분석을 위해 디코딩된 바이트값과 계산한 체크섬을 로그에 추가
        detail = f"수집 데이터 [습도={data_bytes[0]}.{data_bytes[1]}%, 온도={data_bytes[2]}.{data_bytes[3]}%] "
        detail += f"수신된 체크섬={data_bytes[4]}, 계산된 체크섬={checksum}"
        return None, None, f"ERR_CHECKSUM: 데이터 전송 오류 발생 ({detail})"

if __name__ == '__main__':
    print("=" * 60)
    print("라즈베리파이 5 DHT11 온습도 모듈 정밀 진단 툴")
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
                
            time.sleep(2.5)  # DHT11 센서 안정화를 위해 2.5초 간격으로 측정
    except KeyboardInterrupt:
        print("\n진단 측정을 종료합니다.")