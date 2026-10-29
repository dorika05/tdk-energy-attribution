# Compute instantaneous power from raw RAPL counter step-response files.
import csv
import pathlib

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent

def process_file(inpath, outpath, label):
    with open(inpath) as f:
        lines = [l.strip().split() for l in f if l.strip()]

    times = [float(l[0]) for l in lines]
    counters = [int(l[1]) for l in lines]
    t0 = times[0]

    rel_times = []
    powers = []
    for i in range(len(times) - 1):
        dt = times[i + 1] - times[i]
        dE = counters[i + 1] - counters[i]
        power = dE / dt / 1e6  # microjoules -> watts
        t_mid = ((times[i] + times[i + 1]) / 2) - t0
        rel_times.append(t_mid)
        powers.append(power)

    with open(outpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["relative_seconds", "power_watts"])
        for t, p in zip(rel_times, powers):
            w.writerow([round(t, 3), round(p, 3)])

    threshold = 2.0
    step_up_idx = None
    step_down_idx = None
    for i in range(1, len(powers)):
        if powers[i] - powers[i - 1] > threshold and step_up_idx is None:
            step_up_idx = i
        elif (
            step_up_idx is not None
            and powers[i - 1] - powers[i] > threshold
            and step_down_idx is None
        ):
            step_down_idx = i

    if step_up_idx is None:
        print(f"{label}: No step-up found")
        return

    idle_start = max(0, step_up_idx - 20)
    idle_power = sum(powers[idle_start:step_up_idx]) / (step_up_idx - idle_start)

    if step_down_idx is not None:
        load_start = step_up_idx + 5
        load_end = step_down_idx - 5
        if load_end > load_start:
            loaded_power = sum(powers[load_start:load_end]) / (load_end - load_start)
        else:
            loaded_power = sum(powers[step_up_idx:step_down_idx]) / (
                step_down_idx - step_up_idx
            )
        post_start = step_down_idx + 5
        post_end = min(len(powers), step_down_idx + 30)
        if post_end > post_start:
            post_idle = sum(powers[post_start:post_end]) / (post_end - post_start)
        else:
            post_idle = idle_power
    else:
        load_start = step_up_idx + 5
        load_end = len(powers) - 5
        loaded_power = sum(powers[load_start:load_end]) / (load_end - load_start)
        post_idle = None

    step_size = loaded_power - idle_power

    print(f"=== {label} ===")
    print(f"Step-up at relative time: {rel_times[step_up_idx]:.1f} s (index {step_up_idx})")
    if step_down_idx:
        print(
            f"Step-down at relative time: {rel_times[step_down_idx]:.1f} s (index {step_down_idx})"
        )
    print(f"Idle power: {idle_power:.2f} W")
    print(f"Loaded power: {loaded_power:.2f} W")
    print(f"Step size: {step_size:.2f} W")
    if post_idle:
        print(f"Post-step idle power: {post_idle:.2f} W")

    target_95 = idle_power + 0.95 * step_size
    rise_time = None
    for i in range(step_up_idx, min(step_up_idx + 30, len(powers))):
        if powers[i] >= target_95:
            rise_time = rel_times[i] - rel_times[step_up_idx - 1]
            break
    print(f"95% rise target: {target_95:.2f} W")
    if rise_time:
        print(f"Time to 95% of settled: {rise_time:.1f} s")
    else:
        print("Did not reach 95% within 30 samples")

    if step_down_idx:
        target_5pct = idle_power + 0.05 * step_size
        decay_time = None
        for i in range(step_down_idx, min(step_down_idx + 30, len(powers))):
            if powers[i] <= target_5pct:
                decay_time = rel_times[i] - rel_times[step_down_idx - 1]
                break
        print(f"5% decay target: {target_5pct:.2f} W")
        if decay_time:
            print(f"Time to within 5% of idle: {decay_time:.1f} s")
        else:
            print("Did not reach 5% within 30 samples")

    print()
    print(f"Samples around step-up (idx {step_up_idx}):")
    for i in range(max(0, step_up_idx - 3), min(len(powers), step_up_idx + 5)):
        marker = " <-- step" if i == step_up_idx else ""
        print(f"  t={rel_times[i]:.1f}s  P={powers[i]:.2f} W{marker}")

    if step_down_idx:
        print()
        print(f"Samples around step-down (idx {step_down_idx}):")
        for i in range(max(0, step_down_idx - 3), min(len(powers), step_down_idx + 5)):
            marker = " <-- step" if i == step_down_idx else ""
            print(f"  t={rel_times[i]:.1f}s  P={powers[i]:.2f} W{marker}")


process_file(
    ROOT / "step-response-raw.txt", HERE / "step-response-nginx-power.csv", "nginx"
)
print()
process_file(
    ROOT / "step-response-stressng-raw.txt",
    HERE / "step-response-stressng-power.csv",
    "stress-ng",
)
