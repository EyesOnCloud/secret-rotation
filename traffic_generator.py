#!/usr/bin/env python3

import requests
import time
import sys
import signal
from datetime import datetime

TARGET_URL = "http://localhost:8080/db-check"
INTERVAL_SECONDS = 1.0

total_requests = 0
success_count = 0
failure_count = 0
retry_count = 0
start_time = time.time()
running = True

def signal_handler(sig, frame):
    global running
    running = False
    print_summary()
    sys.exit(0)

def print_summary():
    elapsed = round(time.time() - start_time, 1)
    print("\n")
    print("═" * 60)
    print(" TRAFFIC GENERATOR SUMMARY")
    print("═" * 60)
    print(f" Duration              : {elapsed}s")
    print(f" Total requests        : {total_requests}")
    print(f" Successful (200)      : {success_count}")
    print(f" Failed (non-200)      : {failure_count}")
    print(f" App-level retries     : {retry_count}")
    print(f" Success rate          : {round((success_count / total_requests) * 100, 2) if total_requests > 0 else 0}%")
    print("═" * 60)
    if failure_count == 0:
        print(" RESULT: ZERO DOWNTIME ACHIEVED — All requests succeeded")
    else:
        print(f" RESULT: {failure_count} request(s) failed during rotation")
    print("═" * 60)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

print("═" * 60)
print(" Traffic Generator — LAB 2.3 Zero-Downtime Rotation")
print("═" * 60)
print(f" Target   : {TARGET_URL}")
print(f" Interval : {INTERVAL_SECONDS}s per request")
print(f" Press Ctrl+C to stop and see summary")
print("═" * 60)
print(f"{'TIME':12} {'STATUS':8} {'RESP_MS':10} {'VAULT_VER':12} {'RETRIES':8} {'RESULT'}")
print("-" * 70)

while running:
    request_time = datetime.now().strftime("%H:%M:%S")
    request_start = time.time()

    try:
        response = requests.get(TARGET_URL, timeout=10)
        response_ms = round((time.time() - request_start) * 1000, 1)
        total_requests += 1

        if response.status_code == 200:
            success_count += 1
            data = response.json()
            vault_version = data.get('vault_secret_version', '?')
            retries = data.get('retries_used', 0)

            if retries > 0:
                retry_count += 1
                result_str = f"OK (RETRY — rotation detected)"
                print(f"{request_time:12} {response.status_code:<8} {response_ms:<10} {str(vault_version):<12} {retries:<8} {result_str}")
            else:
                result_str = "OK"
                print(f"{request_time:12} {response.status_code:<8} {response_ms:<10} {str(vault_version):<12} {retries:<8} {result_str}")

        else:
            failure_count += 1
            result_str = f"FAILED — HTTP {response.status_code}"
            print(f"{request_time:12} {response.status_code:<8} {response_ms:<10} {'?':<12} {'?':<8} {result_str}")

    except requests.exceptions.ConnectionError:
        response_ms = round((time.time() - request_start) * 1000, 1)
        total_requests += 1
        failure_count += 1
        print(f"{request_time:12} {'ERR':<8} {response_ms:<10} {'?':<12} {'?':<8} CONNECTION ERROR — app down")

    except requests.exceptions.Timeout:
        response_ms = round((time.time() - request_start) * 1000, 1)
        total_requests += 1
        failure_count += 1
        print(f"{request_time:12} {'TIMEOUT':<8} {response_ms:<10} {'?':<12} {'?':<8} REQUEST TIMEOUT")

    except Exception as e:
        response_ms = round((time.time() - request_start) * 1000, 1)
        total_requests += 1
        failure_count += 1
        print(f"{request_time:12} {'ERR':<8} {response_ms:<10} {'?':<12} {'?':<8} {str(e)[:30]}")

    time.sleep(max(0, INTERVAL_SECONDS - (time.time() - request_start)))
