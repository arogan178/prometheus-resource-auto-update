import math
from typing import Optional, Tuple
from goldilocks.config import (
    CPU_REQUEST_TARGET_FACTOR,
    MEMORY_REQUEST_TARGET_FACTOR,
    CPU_LIMIT_BUFFER_FACTOR,
    MEMORY_LIMIT_BUFFER_FACTOR,
    AUTO_MERGE_CPU_THRESHOLD,
    AUTO_MERGE_MEMORY_THRESHOLD,
    MIN_CPU_TARGET_MILLIS,
    MIN_MEMORY_TARGET_MI,
    BULK_REQUEST_MIN_RETAIN_FACTOR,
    BULK_LIMIT_MIN_RETAIN_FACTOR,
    IDLE_CPU_P95_THRESHOLD_MILLIS,
    IDLE_MEMORY_P95_THRESHOLD_MI,
    IDLE_REQUEST_CPU_MILLIS,
    IDLE_REQUEST_MEMORY_MI,
    IDLE_LIMIT_CPU_MILLIS,
    IDLE_LIMIT_MEMORY_MI,
)


def round_up_to_nearest_five(value: int) -> int:
    if value <= 0:
        return 0
    return ((value + 4) // 5) * 5


def get_request_tightening_policy() -> Tuple[float, float]:
    try:
        cpu_factor = float(CPU_REQUEST_TARGET_FACTOR)
        mem_factor = float(MEMORY_REQUEST_TARGET_FACTOR)
    except ValueError as exc:
        raise RuntimeError(
            "Invalid tightening factor configuration. "
            "Set CPU_REQUEST_TARGET_FACTOR and MEMORY_REQUEST_TARGET_FACTOR to decimal values, "
            "for example 0.95 and 0.85."
        ) from exc

    if cpu_factor <= 0:
        raise RuntimeError("CPU_REQUEST_TARGET_FACTOR must be greater than 0.")

    if mem_factor <= 0:
        raise RuntimeError("MEMORY_REQUEST_TARGET_FACTOR must be greater than 0.")

    return cpu_factor, mem_factor


def get_limit_buffer_policy() -> Tuple[float, float]:
    try:
        cpu_factor = float(CPU_LIMIT_BUFFER_FACTOR)
        mem_factor = float(MEMORY_LIMIT_BUFFER_FACTOR)
    except ValueError as exc:
        raise RuntimeError(
            "Invalid limit buffer factor configuration. "
            "Set CPU_LIMIT_BUFFER_FACTOR and MEMORY_LIMIT_BUFFER_FACTOR to decimal values, "
            "for example 1.20."
        ) from exc

    return cpu_factor, mem_factor


def _read_retain_factor(raw_value: str, name: str) -> float:
    try:
        factor = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid retain factor for {name}. Set it to a decimal between 0 and 1."
        ) from exc

    if not (0 < factor <= 1):
        raise RuntimeError(
            f"{name} must be greater than 0 and less than or equal to 1."
        )

    return factor


def get_bulk_retain_policy() -> Tuple[float, float]:
    return (
        _read_retain_factor(
            BULK_REQUEST_MIN_RETAIN_FACTOR, "BULK_REQUEST_MIN_RETAIN_FACTOR"
        ),
        _read_retain_factor(
            BULK_LIMIT_MIN_RETAIN_FACTOR, "BULK_LIMIT_MIN_RETAIN_FACTOR"
        ),
    )


def get_idle_workload_policy() -> Tuple[int, int, int, int, int, int]:
    return (
        IDLE_CPU_P95_THRESHOLD_MILLIS,
        IDLE_MEMORY_P95_THRESHOLD_MI,
        IDLE_REQUEST_CPU_MILLIS,
        IDLE_REQUEST_MEMORY_MI,
        IDLE_LIMIT_CPU_MILLIS,
        IDLE_LIMIT_MEMORY_MI,
    )


def cpu_to_millis(v: str) -> Optional[int]:
    if not v:
        return None
    if v.endswith("m"):
        return int(v[:-1])
    try:
        return int(float(v) * 1000 + 0.5)
    except ValueError:
        return None


def convert_cpu(v: str) -> str:
    millis = cpu_to_millis(v)
    if millis is None:
        return v
    if millis % 1000 == 0:
        return str(millis // 1000)
    return str(millis / 1000)


def format_cpu_millis(millis: int) -> str:
    return convert_cpu(f"{millis}m")


def ceil_cpu_cores_to_millis(value: float, minimum_millis: Optional[int] = None) -> int:
    floor = MIN_CPU_TARGET_MILLIS if minimum_millis is None else minimum_millis
    return max(floor, int(math.ceil(value * 1000)))


def buffered_cpu_limit(cpu: str, buffer_factor: float = 1.20) -> str:
    millis = cpu_to_millis(cpu)
    if millis is None:
        return ""
    buffered = int((millis * buffer_factor) + 0.999999)
    buffered = round_up_to_nearest_five(buffered)
    if buffered % 1000 == 0:
        return str(buffered // 1000)
    return str(buffered / 1000)


def memory_to_mi(v: str) -> Optional[int]:
    if not v:
        return None
    v = str(v)
    multipliers = {
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "Ti": 1024 * 1024,
        "K": 1000 / 1048576,
        "M": 1000000 / 1048576,
        "G": 1000000000 / 1048576,
        "T": 1000000000000 / 1048576,
        "k": 1 / 1024,
    }
    for suffix, mult in multipliers.items():
        if v.endswith(suffix):
            return int((float(v[: -len(suffix)]) * mult) + 0.999999)
    if v.isdigit():
        return int((float(v) / 1048576) + 0.999999)
    return None


def format_memory_mi(mi: int) -> str:
    if mi is None:
        return ""
    if mi <= 0:
        return "0Mi"
    if mi >= 1024 and mi % 1024 == 0:
        return f"{mi // 1024}Gi"
    return f"{mi}Mi"


def convert_memory(v: str) -> str:
    mi = memory_to_mi(v)
    return format_memory_mi(mi) if mi is not None else v


def ceil_memory_to_mi(value: float, minimum_mi: Optional[int] = None) -> int:
    floor = MIN_MEMORY_TARGET_MI if minimum_mi is None else minimum_mi
    return max(floor, int(math.ceil(value)))


def retained_cpu_millis(current_millis: int, retain_factor: float) -> int:
    if current_millis <= 0:
        return 0
    retained = max(1, int(math.ceil(current_millis * retain_factor)))
    retained = round_up_to_nearest_five(retained) if retained >= 5 else retained
    return min(current_millis, retained)


def retained_memory_mi(current_mi: int, retain_factor: float) -> int:
    if current_mi <= 0:
        return 0
    retained = max(1, int(math.ceil(current_mi * retain_factor)))
    retained = round_up_to_nearest_five(retained)
    return min(current_mi, retained)


def buffered_memory_limit(mem: str, buffer_factor: float = 1.20) -> str:
    mi = memory_to_mi(mem)
    if mi is None:
        return ""
    buffered = int((mi * buffer_factor) + 0.999999)
    buffered = round_up_to_nearest_five(buffered)
    return format_memory_mi(buffered)


def tighten_cpu_request(
    target_cpu: str,
    cpu_factor: float,
) -> str:
    target_millis = cpu_to_millis(target_cpu)
    if target_millis is None:
        return target_cpu

    tightened = max(1, int((target_millis * cpu_factor) + 0.999999))
    tightened = round_up_to_nearest_five(tightened) if tightened >= 5 else tightened

    return convert_cpu(f"{tightened}m")


def tighten_memory_request(
    target_memory: str,
    memory_factor: float,
) -> str:
    target_mi = memory_to_mi(target_memory)
    if target_mi is None:
        return target_memory

    tightened = max(1, int((target_mi * memory_factor) + 0.999999))
    tightened = round_up_to_nearest_five(tightened)

    return format_memory_mi(tightened)


def get_auto_merge_thresholds() -> Tuple[int, int]:
    cpu_threshold = cpu_to_millis(AUTO_MERGE_CPU_THRESHOLD)
    mem_threshold = memory_to_mi(AUTO_MERGE_MEMORY_THRESHOLD)

    if cpu_threshold is None or mem_threshold is None:
        raise RuntimeError(
            "Invalid auto-merge threshold configuration. "
            "Set AUTO_MERGE_CPU_THRESHOLD and AUTO_MERGE_MEMORY_THRESHOLD to valid Kubernetes resource values."
        )

    return cpu_threshold, mem_threshold


def format_val(val: int, unit: str) -> str:
    if val == 0:
        return "0" if unit == "m" else f"0{unit}"
    if unit == "Mi" and abs(val) >= 1024 and val % 1024 == 0:
        return f"{val // 1024}Gi"
    if unit == "m":
        cores = val / 1000
        if cores == int(cores):
            return f"{int(cores)}"
        return f"{cores:g}"
    return f"{val}{unit}"


def format_diff_detailed(cur: int, tgt: int, unit: str) -> str:
    diff = tgt - cur
    sign = "+" if diff > 0 else ""
    lbl = "Increase" if diff > 0 else "Savings" if diff < 0 else "No Change"

    if diff == 0:
        return f"{format_val(cur, unit)} (No Change)"

    return f"{format_val(cur, unit)} -> {format_val(tgt, unit)} ({sign}{format_val(diff, unit)} {lbl})"
