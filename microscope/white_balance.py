# white_balance.py — 전체 교체

import time
import cv2
import numpy as np


def run_white_balance(picam2, current_colour_gain=1.0,
                      n_frames=5, settle_time=0.3):
    """
    Gray-world 소프트웨어 화이트 밸런스.
    
    현재 카메라 프레임을 n_frames장 캡처해서 R/G/B 채널 평균을 구한 뒤,
    세 채널 평균이 같아지도록 ColourGains 보정값을 계산합니다.
    하드웨어 AWB를 사용하지 않으므로 LED 조명에서도 정확히 동작합니다.
    """
    try:
        # 노출이 안정될 때까지 잠깐 대기
        time.sleep(settle_time)

        r_acc, g_acc, b_acc = [], [], []

        for _ in range(n_frames):
            frame_yuv = picam2.capture_array("main")
            if frame_yuv is None:
                continue
            frame_bgr = cv2.cvtColor(frame_yuv, cv2.COLOR_YUV2BGR_I420)
            b, g, r = cv2.split(frame_bgr)
            r_acc.append(float(np.mean(r)))
            g_acc.append(float(np.mean(g)))
            b_acc.append(float(np.mean(b)))

        if not r_acc:
            print("[white_balance] 프레임 캡처 실패")
            return None

        mean_r = float(np.mean(r_acc))
        mean_g = float(np.mean(g_acc))
        mean_b = float(np.mean(b_acc))

        print(f"[white_balance] 채널 평균: R={mean_r:.1f} G={mean_g:.1f} B={mean_b:.1f}")

        if mean_r < 2.0 or mean_b < 2.0:
            print("[white_balance] 채널 평균이 너무 낮습니다 — 조명이 켜져 있는지 확인하세요")
            return None

        # Gray World: 현재 게인이 이미 frame에 적용된 상태이므로
        # 보정 배율 = mean_G / mean_R (R을 얼마나 더 올려야 G와 같아지는지)
        # 새 게인 = 현재 게인 × 보정 배율
        from camera import cam_controls
        cur_r = cam_controls["red_gain"]
        cur_b = cam_controls["blue_gain"]

        new_r = cur_r * (mean_g / mean_r)
        new_b = cur_b * (mean_g / mean_b)

        # 범위 제한
        new_r = max(0.1, min(8.0, new_r))
        new_b = max(0.1, min(8.0, new_b))

        print(f"[white_balance] 게인 변경: R {cur_r:.2f}→{new_r:.2f}  B {cur_b:.2f}→{new_b:.2f}")

        # colour_gain 보정 (apply_camera_controls에서 colour_gain 곱이 없으므로 실질적으로 /1.0)
        if current_colour_gain == 0.0:
            current_colour_gain = 1.0

        return round(new_r / current_colour_gain, 3), round(new_b / current_colour_gain, 3)

    except Exception as e:
        print(f"[white_balance] 오류: {e}")
        return None
