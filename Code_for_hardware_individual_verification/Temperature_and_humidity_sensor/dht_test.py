import time
import gpiod
from datetime import datetime

# 라즈베리파이 5 기본 GPIO 설정 (절대 경로로 지정하여 컨테이너 인식 오류 방지)
CHIP = '/dev/gpiochip4'  # 시스템 환경에 따라 '/dev/gpiochip0' 등으로 변경 가능
DHT_PIN = 17  # GPIO 17 (Pin 11)

def read_dht11_detailed():
    """gpiod v2.x 규격을 사용하며, 실패 시 정밀 디버깅 메세지를 반환합니다."""
    raw_values = []
    
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
            
            # 3. 데이터 펄스 고속 수집
            timeout = time.time() + 0.1
            while time.time() < timeout:
                raw_values.append(lines.get_value(DHT_PIN).value)
                if len(raw_values) > 1500:
                    break
    except Exception as e:
        return None, None, f"ERR_GPIO_ACCESS: GPIO 라이브러리 접근 실패 ({str(e)})"
                
    # 4. 수집된 원시 전압 데이터의 상태 변화(Edge) 검출
    transitions = []
    for i in range(1, len(raw_values)):
        if raw_values[i] != raw_values[i-1]:
            transitions.append(i)
            
    # [디버그 포인트 1] 시작 응답 신호 및 물리적 연결 상태 확인
    if len(transitions) < 80:
        if len(transitions) == 0:
            return None, None, f"ERR_NO_RESPONSE: 센서 신호가 전혀 감지되지 않았습니다. (VCC/GND 전원 공급 및 접촉 상태 확인 필요)"
        else:
            return None, None, f"ERR_WEAK_SIGNAL: 신호 상태 변화가 부족합니다. (감지된 변화: {len(transitions)}개, 최소 80개 필요. 전압 저하 및 신호선 접촉 불량 의심)"
        
    # 5. 각 데이터 비트의 길이 계산 및 0/1 판별
    bit_lengths = []
    start_index = 4 if len(transitions) >= 84 else 2
    
    for i in range(start_index, len(transitions) - 1, 2):
        if i + 1 < len(transitions):
            high_len = transitions[i+1] - transitions[i]
            bit_lengths.append(high_len)
            
    # [디버그 포인트 2] 데이터 유실 여부 확인
    if len(bit_lengths) < 40:
        return None, None, f"ERR_INCOMPLETE_DATA: 40비트 전체 데이터를 수집하지 못했습니다. (추출된 비트: {len(bit_lengths)}개. 타이밍 노이즈 의심)"
        
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
        detail = f"수집 데이터 [습도={data_bytes[0]}.{data_bytes[1]}%, 온도={data_bytes[2]}.{data_bytes[3]}%] "
        detail += f"수신된 체크섬={data_bytes[4]}, 계산된 체크섬={checksum}"
        return None, None, f"ERR_CHECKSUM: 데이터 전송 중 데이터가 깨졌습니다. ({detail})"

if __name__ == '__main__':
    print("=" * 60)
    print("라즈베리파이 5 DHT11 정밀 진단 툴 (gpiod v2.x 규격 통합본)")
    print("=" * 60)
    
    try:
        while True:
            current_time = datetime.now().strftime('%H:%M:%S')
            temp, hum, status = read_dht11_detailed()
            
            if status == "SUCCESS":
                print(f"[{current_time}] 🌡️ 온도: {temp}°C | 💧 습도: {hum}% | 상태: 정상")
            else:
                print(f"[{current_time}] ❌ 측정 실패 리포트")
                print(f"  └─ 원인 분석: {status}")
                print("-" * 60)
                
            time.sleep(3.0)  # DHT11 센서 안정화를 위해 3.0초 간격으로 측정
    except KeyboardInterrupt:
        print("\n진단 측정을 종료합니다.")