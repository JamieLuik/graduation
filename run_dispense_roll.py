import serial
from serial.tools import list_ports
import time
import re
from pathlib import Path
import os
import sys

# ---------- CONFIG ----------
BAUD_RATE = 9600
MAX_POUCHES = 64

BARCODE_LENGTH = 11

AUTOLOAD_WAIT = 7.0
# ----------------------------

START_SCAN_TOKEN = "[SE655C  ]: Starting scan process"
READ_TOKEN = "[SE655C  ]: Read:"
DISPENSE_SUCCESS_TOKEN = "Dispense process succeeded"
FAILED_SACHET_TOKEN = "failed at sachet"

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

raw_log_path = desktop / f"dispense_roll_raw_{timestamp}.txt"
step_log_path = desktop / f"dispense_roll_log_{timestamp}.txt"

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
        while ser.in_waiting:
            line = ser.readline().decode(errors="ignore").strip()
            raw_log.write(line + "\n")
        time.sleep(0.01)

def is_shutdown_barcode(barcode):
    return (
        bool(barcode)
        and barcode.isdigit()
        and len(barcode) == (BARCODE_LENGTH + 1)
        and barcode.startswith("9")
    )

shutdown_requested = False
failed_at_sachet = False
pouch_head = 1
scan_start_count = 0
scan_read_count = 0

current_pouch_has_read = False
current_pouch_barcode = None

with open(step_log_path, "w", buffering=1) as step_log, \
    open(raw_log_path, "w", buffering=1) as raw_log:

    send("AUTOLOAD")
    wait_with_serial_logging(AUTOLOAD_WAIT, raw_log)

    send("DISPENSE_ROLL")

    while True:
        if ser.in_waiting:
            line = ser.readline().decode(errors="ignore").strip()
            raw_log.write(line + "\n")

            if START_SCAN_TOKEN in line:
                scan_start_count += 1
                continue

            if READ_TOKEN in line:
                scan_read_count += 1
                m = re.search(r"Read:\s*(\S+)", line)
                if m:
                    current_pouch_has_read = True
                    current_pouch_barcode = m.group(1)
                continue

            if FAILED_SACHET_TOKEN in line:
                failed_at_sachet = True
                shutdown_requested = True
                step_log.write("Detected 'failed at sachet'. Terminating test.\n")
                print("Detected 'failed at sachet'. Terminating test.")
                break

            if DISPENSE_SUCCESS_TOKEN in line:
                if current_pouch_has_read and current_pouch_barcode:
                    if current_pouch_barcode.isdigit() and len(current_pouch_barcode) == BARCODE_LENGTH:
                        result = current_pouch_barcode
                    else:
                        result = f"{current_pouch_barcode} (Reading error)"
                else:
                    result = "----"

                log_line = f"Pouch {pouch_head:04d}: {result}"
                print(log_line)
                step_log.write(log_line + "\n")

                if current_pouch_has_read and is_shutdown_barcode(current_pouch_barcode):
                    shutdown_requested = True

                current_pouch_has_read = False
                current_pouch_barcode = None

                if shutdown_requested:
                    break

                pouch_head += 1
                if pouch_head > MAX_POUCHES:
                    print(f"Reached MAX_POUCHES ({MAX_POUCHES}). Terminating test.")
                    step_log.write(f"Reached MAX_POUCHES ({MAX_POUCHES}). Terminating test.\n")
                    break
        else:
            time.sleep(0.01)

    pouch_count = pouch_head - 1
    barcode_count = scan_read_count
    read_percent = (barcode_count / pouch_count * 100.0) if pouch_count else 0.0

    print(f"\nPouch Count:   {pouch_count}")
    print(f"Barcode Count: {barcode_count}")
    print(f"Read %:        {read_percent:.2f}")

    step_log.write(f"\nPouch Count:   {pouch_count}\n")
    step_log.write(f"Barcode Count: {barcode_count}\n")
    step_log.write(f"Read %:        {read_percent:.2f}\n")
    if failed_at_sachet:
        step_log.write("Test terminated due to 'failed at sachet'.\n")

print("Test complete.")
ser.close()
