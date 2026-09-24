def judge(cyan_mm: float, magenta_mm: float) -> tuple[str, str]:
    if abs(cyan_mm) <= 0.15 and abs(magenta_mm) <= 0.15:
        return "套准", "青品两色偏差都在允差内"
    return "套不准", "至少一色偏差超出允差"
