import time
from smbus2 import SMBus

# I2C 16x2 LCD 제어용 초경량 드라이버 클래스
class I2CLCD:
    # 일반적인 I2C LCD 주소는 0x27 또는 0x3F
    # 위 터미널에서 i2cdetect로 나온 주소값을 작성
    def __init__(self, address=0x27, bus_num=1):
        self.address = address
        self.bus = SMBus(bus_num)
        
        # LCD 명령어 정의
        self.LCD_CHR = 1 # 데이터 전송 모드
        self.LCD_CMD = 0 # 명령 전송 모드
        
        # 라인 주소 정의 (16x2 화면)
        self.LCD_LINE_1 = 0x80 # 첫 번째 줄 시작 위치
        self.LCD_LINE_2 = 0xC0 # 두 번째 줄 시작 위치
        
        self.LCD_BACKLIGHT = 0x08  # 백라이트 켜기 (0x00은 끄기)
        self.ENABLE = 0b00000100   # En (Enable) 핀 트리거
        
        # LCD 초기화 시퀀스 실행
        self.lcd_write(0x33, self.LCD_CMD) # 4비트 모드 설정용 시드
        self.lcd_write(0x32, self.LCD_CMD) # 4비트 모드 진입
        self.lcd_write(0x06, self.LCD_CMD) # 커서 이동 방향 오른쪽으로 설정
        self.lcd_write(0x0C, self.LCD_CMD) # 디스플레이 ON, 커서 OFF, 커서 깜빡임 OFF
        self.lcd_write(0x28, self.LCD_CMD) # 2줄 출력, 5x8 폰트 설정
        self.lcd_write(0x01, self.LCD_CMD) # 화면 클리어
        time.sleep(0.005)

    def write_word(self, data):
        """I2C 버스로 실제 데이터를 물리 전송하는 저수준 함수"""
        temp = data | self.LCD_BACKLIGHT
        self.bus.write_byte(self.address, temp)

    def send_pulse(self, data):
        """LCD가 신호를 안전하게 수신하도록 동기화 펄스(Enable) 전송"""
        self.write_word(data | self.ENABLE)
        time.sleep(0.0005)
        self.write_word(data & ~self.ENABLE)
        time.sleep(0.0001)

    def lcd_write(self, val, mode):
        """명령어 혹은 텍스트 바이트를 상위 4비트, 하위 4비트로 나눠 전송 (4비트 모드)"""
        # 상위 4비트 추출
        high = mode | (val & 0xF0)
        self.write_word(high)
        self.send_pulse(high)
        
        # 하위 4비트 추출
        low = mode | ((val << 4) & 0xF0)
        self.write_word(low)
        self.send_pulse(low)

    def display_text(self, text, line):
        """원하는 줄(LCD_LINE_1 또는 LCD_LINE_2)에 문자열 출력"""
        # 줄 시작 주소 설정
        self.lcd_write(line, self.LCD_CMD)
        
        # 16칸에 맞춤 및 패딩 처리
        text = text.ljust(16, " ")
        
        # 문자 하나씩 아스키코드로 변환하여 화면에 그리기
        for char in text[:16]:
            self.lcd_write(ord(char), self.LCD_CHR)

    def clear(self):
        """화면에 채워진 모든 텍스트를 지웁니다."""
        self.lcd_write(0x01, self.LCD_CMD)
        time.sleep(0.005)

# 실제 데모에서 사용할법한 텍스트 동작 시나리오 테스트
if __name__ == '__main__':
    print("라즈베리파이 5 - I2C LCD 제어 테스트 시작")
    
    try:
        # LCD 초기화 (주소가 다르면 0x3F 등으로 변경)
        lcd = I2CLCD(address=0x27)
        
        # 시나리오 1: 대기 상태 (시스템 시작 알림)
        print("시나리오 1: 대기 상태 출력")
        lcd.display_text("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
        lcd.display_text("   [ READY ]    ", lcd.LCD_LINE_2)
        time.sleep(3.0)
        
        # 시나리오 2: 스캔 시작 (초음파 감지 후 카메라 촬영)
        print("시나리오 2: 쓰레기 감지 상태 출력")
        lcd.clear()
        lcd.display_text("USER DETECTED!", lcd.LCD_LINE_1)
        lcd.display_text("Scanning waste..", lcd.LCD_LINE_2)
        time.sleep(2.5)
        
        # 시나리오 3: 판별 완료 및 제출 안내 (YOLO 추론 성공)
        print("시나리오 3: 판별 및 분리배출 요령 안내")
        lcd.clear()
        # 영문과 대중적인 기호들만 출력이 지원됩니다.
        lcd.display_text("PLASTIC BOTTLE", lcd.LCD_LINE_1)
        lcd.display_text("-> Open Blue Bin", lcd.LCD_LINE_2)
        time.sleep(4.0)

        # 시나리오 4: 경고 상태 (가득 참 혹은 위생 불량)
        print("시나리오 4: 위생 위험 상태 경고")
        lcd.clear()
        lcd.display_text("   WARNING!   ", lcd.LCD_LINE_1)
        lcd.display_text("BIN IS FULL NOW ", lcd.LCD_LINE_2)
        time.sleep(3.0)

        # 다시 원상복구 대기 상태로 마무리
        lcd.clear()
        lcd.display_text("  AIoT SYSTEM  ", lcd.LCD_LINE_1)
        lcd.display_text("   [ READY ]    ", lcd.LCD_LINE_2)
        print("테스트 완료!")

    except KeyboardInterrupt:
        print("\n테스트 조기 종료")