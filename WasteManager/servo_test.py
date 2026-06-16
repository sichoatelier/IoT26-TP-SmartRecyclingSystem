#!/usr/bin/env python3
# ==========================================
# 서보모터 단독 테스트 도구 (Verbose)
# ------------------------------------------
# main_final.py 와 동일한 핀/PWM 스펙으로 각 서보모터를
# 개별 테스트한다. 모든 동작은 상세 로그로 출력된다.
#
# 사용법:
#   python3 servo_test.py                 # 대화형 메뉴
#   python3 servo_test.py 18 90           # GPIO18 서보를 90도로 (단발)
#   python3 servo_test.py 19 0 0.8        # GPIO19 서보를 0도로, duration 0.8s
# ==========================================
import sys
import time
from datetime import datetime

import gpiod
from gpiod.line import Direction, Value

# ==========================================
# 하드웨어 설정 (main_final.py 와 동일)
# ==========================================
CHIP_PATH = '/dev/gpiochip4'

SERVO_PIN = 18        # 분류 판 서보 (분리배출 판 기울임)
ENTRY_SERVO_PIN = 19  # 입구 개폐 서보

# 분류 판 각도 프리셋 (main_final.py 의 분류 라우팅과 동일)
PLATE_PRESETS = {
    "plastico (플라스틱)":      25,
    "metal (캔/메탈)":          75,
    "papel_y_carton (종이)":    105,
    "vidrio/organico/general": 155,
    "기본 복귀 (0도)":           0,
}

# 입구 서보 각도 프리셋
ENTRY_PRESETS = {
    "닫힘": 0,
    "개방": 180,
}


def debug_print(tag, message):
    """타임스탬프 포함 상세 로그 (밀리초 단위)"""
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}][{tag}] {message}", flush=True)


# ==========================================
# 서보 모터 컨트롤러 (Verbose 버전)
# ==========================================
class ServoController:
    def __init__(self, chip_path, pin):
        self.pin = pin
        self.chip_path = chip_path
        debug_print("SERVO", f"초기화 시작: chip_path={chip_path}, pin={pin}")
        try:
            self.req = gpiod.request_lines(
                chip_path,
                consumer=f"ServoTest_{pin}",
                config={self.pin: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)}
            )
            debug_print("SERVO", f"GPIO 라인 요청 성공 (pin={pin})")
        except Exception as e:
            debug_print("SERVO", f"GPIO 라인 요청 실패 (pin={pin}): {e}")
            raise

    def set_angle(self, angle, duration=1.0):
        t0 = time.time()
        debug_print("SERVO", f"set_angle 호출: pin={self.pin}, angle={angle}, duration={duration}")

        if angle < 0:
            debug_print("SERVO", f"angle 값이 낮아 0도로 보정: {angle} -> 0")
            angle = 0
        elif angle > 180:
            debug_print("SERVO", f"angle 값이 높아 180도로 보정: {angle} -> 180")
            angle = 180

        # SG90 및 일반 서보모터 스펙 (0도: 0.5ms, 180도: 2.5ms) 적용
        pulse_width = 0.0005 + (angle / 180.0) * 0.002
        period = 0.020  # 50Hz
        low_time = period - pulse_width

        debug_print(
            "SERVO",
            f"PWM 계산: angle={angle}, pulse_width={pulse_width * 1000:.3f}ms, "
            f"low_time={low_time * 1000:.3f}ms, period={period * 1000:.3f}ms, freq=50Hz"
        )

        if low_time <= 0:
            debug_print("SERVO", f"비정상 PWM 계산: low_time={low_time:.6f}s, angle={angle} -> 중단")
            return

        # duration 동안 소프트웨어 PWM 루프 실행
        end_time = time.time() + duration
        loop_count = 0
        while time.time() < end_time:
            loop_count += 1
            self.req.set_value(self.pin, Value.ACTIVE)
            time.sleep(pulse_width)
            self.req.set_value(self.pin, Value.INACTIVE)
            time.sleep(low_time)

        elapsed = time.time() - t0
        debug_print(
            "SERVO",
            f"set_angle 종료: pin={self.pin}, angle={angle}, duration={duration}, "
            f"loop_count={loop_count}, 실측 소요={elapsed:.3f}s"
        )

    def sweep(self, start=0, end=180, step=15, duration=0.4):
        """start~end 각도를 step 간격으로 순차 이동하며 동작 범위를 점검"""
        debug_print("SWEEP", f"스윕 시작: pin={self.pin}, {start}도 -> {end}도, step={step}도, 각 {duration}s")
        direction = step if end >= start else -step
        angle = start
        while (direction > 0 and angle <= end) or (direction < 0 and angle >= end):
            self.set_angle(angle, duration=duration)
            angle += direction
        debug_print("SWEEP", f"스윕 종료: pin={self.pin}")

    def cleanup(self):
        debug_print("SERVO", f"cleanup 시작: pin={self.pin}")
        try:
            self.req.set_value(self.pin, Value.INACTIVE)
            self.req.release()
            debug_print("SERVO", f"cleanup 완료: pin={self.pin}")
        except Exception as e:
            debug_print("SERVO", f"cleanup 에러 (pin={self.pin}): {e}")


# ==========================================
# 직접 실행 모드 (인자: pin angle [duration])
# ==========================================
def run_direct(args):
    pin = int(args[0])
    angle = int(args[1])
    duration = float(args[2]) if len(args) >= 3 else 1.0
    debug_print("MAIN", f"직접 실행 모드: pin={pin}, angle={angle}, duration={duration}")
    servo = ServoController(CHIP_PATH, pin)
    try:
        servo.set_angle(angle, duration=duration)
    finally:
        servo.cleanup()


# ==========================================
# 대화형 메뉴 모드
# ==========================================
def select_servo():
    print("\n" + "=" * 50)
    print("테스트할 서보모터를 선택하세요")
    print("=" * 50)
    print(f"  1) 분류 판 서보  (GPIO {SERVO_PIN})")
    print(f"  2) 입구 개폐 서보 (GPIO {ENTRY_SERVO_PIN})")
    print(f"  3) 직접 핀 번호 입력")
    print(f"  q) 종료")
    choice = input("선택 > ").strip().lower()
    if choice == "1":
        return SERVO_PIN, "분류 판"
    if choice == "2":
        return ENTRY_SERVO_PIN, "입구 개폐"
    if choice == "3":
        pin = int(input("GPIO 핀 번호 > ").strip())
        return pin, f"커스텀(GPIO {pin})"
    if choice == "q":
        return None, None
    print("[!] 잘못된 선택입니다.")
    return select_servo()


def servo_menu(servo, label):
    """선택한 서보에 대한 동작 메뉴"""
    presets = ENTRY_PRESETS if servo.pin == ENTRY_SERVO_PIN else PLATE_PRESETS
    while True:
        print("\n" + "-" * 50)
        print(f"[{label}] (GPIO {servo.pin}) 동작 선택")
        print("-" * 50)
        print("  a) 각도 직접 입력")
        print("  s) 스윕 (0 -> 180 -> 0)")
        print("  p) 프리셋 목록 실행")
        print("  b) 다른 서보 선택으로 돌아가기")
        print("  q) 종료")
        action = input("동작 > ").strip().lower()

        if action == "a":
            try:
                angle = int(input("각도 (0~180) > ").strip())
                dur_raw = input("duration 초 (기본 1.0) > ").strip()
                duration = float(dur_raw) if dur_raw else 1.0
            except ValueError:
                print("[!] 숫자를 입력하세요.")
                continue
            servo.set_angle(angle, duration=duration)

        elif action == "s":
            servo.sweep(0, 180, step=15, duration=0.4)
            servo.sweep(180, 0, step=15, duration=0.4)

        elif action == "p":
            items = list(presets.items())
            for i, (name, ang) in enumerate(items, 1):
                print(f"  {i}) {name} -> {ang}도")
            sel = input("프리셋 번호 (전체 순차=all) > ").strip().lower()
            if sel == "all":
                for name, ang in items:
                    debug_print("PRESET", f"{name} -> {ang}도")
                    servo.set_angle(ang, duration=1.0)
            else:
                try:
                    name, ang = items[int(sel) - 1]
                    debug_print("PRESET", f"{name} -> {ang}도")
                    servo.set_angle(ang, duration=1.0)
                except (ValueError, IndexError):
                    print("[!] 잘못된 번호입니다.")

        elif action == "b":
            return "back"
        elif action == "q":
            return "quit"
        else:
            print("[!] 잘못된 선택입니다.")


def run_interactive():
    print("=" * 50)
    print("서보모터 단독 테스트 도구 (Verbose)")
    print(f"CHIP_PATH={CHIP_PATH}")
    print("=" * 50)

    active = {}  # pin -> ServoController (한 번 잡은 라인 재사용)
    try:
        while True:
            pin, label = select_servo()
            if pin is None:
                break
            if pin not in active:
                active[pin] = ServoController(CHIP_PATH, pin)
            result = servo_menu(active[pin], label)
            if result == "quit":
                break
    except KeyboardInterrupt:
        print("\n[중단] 사용자에 의해 종료됩니다.")
    finally:
        for servo in active.values():
            servo.cleanup()
        debug_print("MAIN", "모든 서보 라인 해제 완료. 프로그램 종료.")


def main():
    if len(sys.argv) >= 3:
        run_direct(sys.argv[1:])
    else:
        run_interactive()


if __name__ == "__main__":
    main()
