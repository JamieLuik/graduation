import serial
from serial.tools import list_ports
import time
import re
import random
from pathlib import Path
import os
import sys
import subprocess

# ------- POUCH CONFIG -------

# VOORZORG
ENCODER_LENGTH = 80
BARCODE_LOCATION = 10
BARCODE_LENGTH = 11
MAX_POUCHES = 64

# SPITS APELDOORN - 1693
# ENCODER_LENGTH = 76
# BARCODE_LOCATION = 60
# BARCODE_LENGTH = 5
# MAX_POUCHES = 11

# ---------- CONFIG ----------
BAUD_RATE = 9600

FIRST_BARCODE = ENCODER_LENGTH - BARCODE_LOCATION
POUCH_LENGTH = ENCODER_LENGTH

READ_STEPS_BEFORE = 2
READ_ATTEMPT_LENGTH = 10
RETRY_AMOUNT = 3

SCAN_WINDOW = 0.4
MOTOR_WAIT = 3.0
# ----------------------------

def select_com_port():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)

    if not ports:
        print("FAILED TO OPEN SERIAL PORT: No COM ports detected.")
        sys.exit(1)

    if len(ports) == 1:
        selected_port = ports[0].device
        print(f"Using serial port: {selected_port}")
        return selected_port

    print("Multiple COM ports detected. Select one:")
    for idx, port in enumerate(ports, start=1):
        details = f"{port.device} - {port.description}"
        if port.hwid:
            details += f" ({port.hwid})"
        print(f"{idx}. {details}")

    while True:
        selection = input("Enter the port number: ").strip()
        if not selection.isdigit():
            print("Invalid selection. Enter a number from the list.")
            continue

        selection_idx = int(selection)
        if 1 <= selection_idx <= len(ports):
            selected_port = ports[selection_idx - 1].device
            print(f"Using serial port: {selected_port}")
            return selected_port

        print("Invalid selection. Enter a valid number from the list.")

desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
timestamp = time.strftime("%Y%m%d_%H%M%S")

raw_log_path = desktop / f"improved_bartest_raw_{timestamp}.txt"
step_log_path = desktop / f"improved_bartest_log_{timestamp}.txt"

try:
    COM_PORT = select_com_port()
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.05, write_timeout=1)
except Exception as e:
    print("FAILED TO OPEN SERIAL PORT:", e)
    sys.exit(1)

def send(cmd):
    ser.write((cmd + "\r").encode())
    ser.flush()

def drain_serial(raw_log, capture_barcode=False):
    barcode = None
    while ser.in_waiting:
        line = ser.readline().decode(errors="ignore").strip()
        raw_log.write(line + "\n")
        if capture_barcode and "Read:" in line:
            m = re.search(r"Read:\s*(\S+)", line)
            if m:
                barcode = m.group(1)
    return barcode

def wait_with_serial_logging(duration, raw_log):
    end_time = time.perf_counter() + duration
    while time.perf_counter() < end_time:
        drain_serial(raw_log)
        time.sleep(0.01)

def is_shutdown_barcode(barcode):
    return (
        bool(barcode)
        and barcode.isdigit()
        and len(barcode) == (BARCODE_LENGTH + 1)
        and barcode.startswith("9")
    )

def scan_cutoff(raw_log):
    barcode = None

    send("BARTEST")
    start = time.perf_counter()

    # Scan window (max 1.2s)
    while time.perf_counter() - start < SCAN_WINDOW:
        latest_barcode = drain_serial(raw_log, capture_barcode=True)
        if latest_barcode:
            barcode = latest_barcode

    # Interrupt scan (motor step happens here)
    send("FWE=1")

    # Short drain (100ms)
    drain_start = time.perf_counter()
    while time.perf_counter() - drain_start < 0.1:
        latest_barcode = drain_serial(raw_log, capture_barcode=True)
        if latest_barcode:
            barcode = latest_barcode

    return barcode

step_number = 0
last_found_middle_position = None
shutdown_requested = False
next_pouch_count = 0

with open(step_log_path, "w", buffering=1) as step_log, \
    open(raw_log_path, "w", buffering=1) as raw_log:

    send("AUTOLOAD")
    wait_with_serial_logging(7, raw_log)

    # ---- Initial positioning ----
    initial_move = FIRST_BARCODE - READ_STEPS_BEFORE
    send(f"FWE={initial_move}")
    step_number += initial_move
    wait_with_serial_logging(MOTOR_WAIT, raw_log)

    while True:

        retry_count = 0
        cluster_positions = []

        # ---- Retry window ----
        while retry_count < RETRY_AMOUNT:

            cluster_positions = []
            consecutive_misses = 0

            for _ in range(READ_ATTEMPT_LENGTH):

                barcode = scan_cutoff(raw_log)

                # Log BEFORE incrementing step counter
                if barcode:
                    if barcode.isdigit() and len(barcode) == BARCODE_LENGTH:
                        result = barcode
                    else:
                        result = f"{barcode} (Reading error)"
                else:
                    result = "----"

                step_log.write(f"Step {step_number:07d}: {result}\n")
                print(f"Step {step_number:07d}: {result}")

                if is_shutdown_barcode(barcode):
                    shutdown_requested = True
                    break

                # Cluster logic
                if barcode and barcode.isdigit() and len(barcode) == BARCODE_LENGTH:
                    cluster_positions.append(step_number)
                    consecutive_misses = 0
                else:
                    consecutive_misses += 1

                # Step increment happens AFTER logging
                step_number += 1

                # End cluster if started and 2 misses occur
                if cluster_positions and consecutive_misses >= 2:
                    break

            if shutdown_requested:
                break

            if cluster_positions:
                break

            # Roll back attempt window
            send(f"RWE={READ_ATTEMPT_LENGTH}")
            step_number -= READ_ATTEMPT_LENGTH
            wait_with_serial_logging(MOTOR_WAIT, raw_log)
            retry_count += 1

        if shutdown_requested:
            break

        if not cluster_positions:
            print("=====NEXT POUCH - NO BARCODE FOUND=====")
            step_log.write("=====NEXT POUCH - NO BARCODE FOUND=====\n")

            reference_middle_position = last_found_middle_position
            if reference_middle_position is None:
                reference_middle_position = FIRST_BARCODE

            # Keep pouch alignment based on the last confirmed barcode center:
            # target = last_found_center + (n * POUCH_LENGTH) - READ_STEPS_BEFORE
            # Pick n so the target is the first pouch position ahead of the current head.
            base_target = reference_middle_position - READ_STEPS_BEFORE
            pouch_multiple = ((step_number - base_target) // POUCH_LENGTH) + 1
            if pouch_multiple < 1:
                pouch_multiple = 1
            target_position = base_target + (pouch_multiple * POUCH_LENGTH)

            move_amount = target_position - step_number
            while move_amount <= 0:
                target_position += POUCH_LENGTH
                move_amount = target_position - step_number

            send(f"FWE={move_amount}")
            step_number += move_amount
            wait_with_serial_logging(MOTOR_WAIT, raw_log)
            next_pouch_count += 1
            if next_pouch_count >= MAX_POUCHES:
                print(f"Reached MAX_POUCHES ({MAX_POUCHES}). Terminating test.")
                step_log.write(f"Reached MAX_POUCHES ({MAX_POUCHES}). Terminating test.\n")
                break

            continue

        # ---- Continue cluster until 2 misses ----
        consecutive_misses = 0

        while consecutive_misses < 2:

            barcode = scan_cutoff(raw_log)

            if barcode:
                if barcode.isdigit() and len(barcode) == BARCODE_LENGTH:
                    result = barcode
                else:
                    result = f"{barcode} (Reading error)"
            else:
                result = "----"

            step_log.write(f"Step {step_number:07d}: {result}\n")
            print(f"Step {step_number:07d}: {result}")

            if is_shutdown_barcode(barcode):
                shutdown_requested = True
                break

            if barcode and barcode.isdigit() and len(barcode) == BARCODE_LENGTH:
                cluster_positions.append(step_number)
                consecutive_misses = 0
            else:
                consecutive_misses += 1

            step_number += 1

        if shutdown_requested:
            break

        # ---- Compute midpoint of this cluster only ----
        lower_position = min(cluster_positions)
        upper_position = max(cluster_positions)
        midpoint_sum = lower_position + upper_position

        if midpoint_sum % 2 == 0:
            middle_position = midpoint_sum // 2
        else:
            middle_position = random.choice(
                [midpoint_sum // 2, (midpoint_sum // 2) + 1]
            )

        print(f"=====NEXT POUCH - BARCODE FOUND AT {middle_position}=====")
        step_log.write(f"=====NEXT POUCH - BARCODE FOUND AT {middle_position}=====\n")
        last_found_middle_position = middle_position

        expected_next_center = middle_position + POUCH_LENGTH
        target_position = expected_next_center - READ_STEPS_BEFORE

        move_amount = target_position - step_number

        if move_amount > 0:
            send(f"FWE={move_amount}")
            step_number += move_amount
            wait_with_serial_logging(MOTOR_WAIT, raw_log)
        elif move_amount < 0:
            send(f"RWE={abs(move_amount)}")
            step_number += move_amount
            wait_with_serial_logging(MOTOR_WAIT, raw_log)

        next_pouch_count += 1
        if next_pouch_count >= MAX_POUCHES:
            print(f"Reached MAX_POUCHES ({MAX_POUCHES}). Terminating test.")
            step_log.write(f"Reached MAX_POUCHES ({MAX_POUCHES}). Terminating test.\n")
            break

    if shutdown_requested:
        final_move = 2 * POUCH_LENGTH
        send(f"FWE={final_move}")
        step_number += final_move
        wait_with_serial_logging(MOTOR_WAIT, raw_log)
        print(f"Shutdown barcode detected. Final move executed: FWE={final_move}.")
        step_log.write(f"Shutdown barcode detected. Final move executed: FWE={final_move}.\n")

        analyzer_path = Path(__file__).with_name("bartest_analyser.py")
        if analyzer_path.exists():
            try:
                subprocess.run([sys.executable, str(analyzer_path)], check=True)
                print("bartest_analyser.py completed.")
                step_log.write("bartest_analyser.py completed.\n")
            except subprocess.CalledProcessError as exc:
                print(f"bartest_analyser.py failed: {exc}")
                step_log.write(f"bartest_analyser.py failed: {exc}\n")
        else:
            print("bartest_analyser.py not found.")
            step_log.write("bartest_analyser.py not found.\n")

print("Test complete.")
ser.close()
