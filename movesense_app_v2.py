import sys
import os
import csv
import re
import zlib
import struct
import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Callable
from bleak import BleakClient, BleakScanner
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QListWidget, QMessageBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QPlainTextEdit,
                             QRadioButton, QButtonGroup, QGroupBox)
from qasync import QEventLoop, asyncSlot
from sbem_tools.parse_sbem import read_chunks

# Suppress harmless Wayland focus warnings on Linux desktop
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland*=false")


def ensure_bluetooth_powered(cycle: bool = False):
    """Ensures host Bluetooth controller is powered on via bluetoothctl."""
    try:
        if cycle:
            subprocess.run(["bluetoothctl", "power", "off"], capture_output=True, timeout=1.5)
            subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=1.5)
        else:
            subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=1.5)
    except Exception:
        pass

# ==============================================================================
# Movesense BLE UUIDs & Protocol Constants
# ==============================================================================

# GSP Service (Generic SensorData Protocol - default Movesense DataLogger firmware)
GSP_SERVICE_UUID = "34802252-7185-4d5d-b431-630e7050e8f0"
GSP_WRITE_CHAR_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"
GSP_NOTIFY_CHAR_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"

# Whiteboard GATT Service (Movesense Protocol V9)
WB_SERVICE_UUID = "0000fdf3-0000-1000-8000-00805f9b34fb"
WB_WRITE_CHAR_UUID = "6b200001-ff4e-4979-8186-fb7ba486fcd7"
WB_NOTIFY_CHAR_UUID = "6b200002-ff4e-4979-8186-fb7ba486fcd7"

# Movesense 16-bit ADC to mV conversion scale
ECG_LSB_TO_MV = 0.000381469726563

# Whiteboard Framing & Message Constants
WB_MSG_HELLO = 0x12
WB_MSG_HELLO_ACK = 0x13
WB_MSG_CLIENT_ON_NOTIFY = 0x14
WB_MSG_RESOURCE_GET_REQUEST = 0x0A
WB_MSG_PROVIDER_GET_REQUEST = 0x0D
WB_MSG_PROVIDER_PUT_REQUEST = 0x0E
WB_MSG_PROVIDER_DELETE_REQUEST = 0x17

RES_ID_DATALOGGER_CONFIG = bytes([0xF0, 0x33, 0x00])
RES_ID_DATALOGGER_STATE  = bytes([0xF0, 0x33, 0x01])
RES_ID_LOGBOOK_ENTRIES   = bytes([0xF0, 0x34, 0x00])


def sanitize_filename_part(text: str) -> str:
    """Replaces characters unsafe in filenames with underscores."""
    return re.sub(r'[^A-Za-z0-9._-]', '_', text).strip('._') or "unnamed"


def sf_encode(payload: bytes) -> bytes:
    """Encodes payload with HDLC framing (0x7E) and CRC32 IEEE LE."""
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    crc_bytes = struct.pack("<I", crc)
    data = payload + crc_bytes
    framed = bytearray([0x7E])
    for b in data:
        if b in (0x7E, 0x7D):
            framed.append(0x7D)
            framed.append(b ^ 0x20)
        else:
            framed.append(b)
    framed.append(0x7E)
    return bytes(framed)


def sf_decode(frame: bytes) -> bytes:
    """Decodes HDLC framed packet and validates CRC32 IEEE."""
    if len(frame) < 6 or frame[0] != 0x7E or frame[-1] != 0x7E:
        return b""
    unescaped = bytearray()
    i = 1
    end = len(frame) - 1
    while i < end:
        b = frame[i]
        if b == 0x7D:
            i += 1
            if i < end:
                unescaped.append(frame[i] ^ 0x20)
        else:
            unescaped.append(b)
        i += 1
    if len(unescaped) < 4:
        return b""
    payload = bytes(unescaped[:-4])
    recvd_crc = struct.unpack("<I", unescaped[-4:])[0]
    calc_crc = zlib.crc32(payload) & 0xFFFFFFFF
    return payload if recvd_crc == calc_crc else b""


def pack_serial(serial_str: str) -> bytes:
    res = bytearray(12)
    for group in range(4):
        chars = [0, 0, 0, 0]
        for i in range(4):
            idx = group * 4 + i
            if idx < len(serial_str):
                chars[i] = (ord(serial_str[idx]) - 0x2F) & 0x3F
        res[group * 3]     = chars[0] | ((chars[1] & 0x03) << 6)
        res[group * 3 + 1] = (chars[1] >> 2) | ((chars[2] & 0x0F) << 4)
        res[group * 3 + 2] = (chars[2] >> 4) | (chars[3] << 2)
    return bytes(res)


def unpack_serial(b: bytes) -> str:
    chars = []
    for group in range(4):
        b0 = b[group * 3]
        b1 = b[group * 3 + 1]
        b2 = b[group * 3 + 2]
        c0 = (b0 & 0x3F) + 0x2F
        c1 = (((b0 >> 6) & 0x03) | ((b1 & 0x0F) << 2)) + 0x2F
        c2 = (((b1 >> 4) & 0x0F) | ((b2 & 0x03) << 4)) + 0x2F
        c3 = ((b2 >> 2) & 0x3F) + 0x2F
        chars.extend([chr(c0), chr(c1), chr(c2), chr(c3)])
    return "".join(chars)


# ==============================================================================
# Unified Movesense DataLogger & Whiteboard Client
# ==============================================================================

class MovesenseSensorClient:
    """Manages connection, Whiteboard DataLogger recording, and Logbook operations over BLE."""

    def __init__(self, mac: str):
        self.mac = mac
        self.client: Optional[BleakClient] = None
        self.serial_number = ""
        self.uses_gsp = True
        self.download_queue: asyncio.Queue = asyncio.Queue()
        self.response_queue: asyncio.Queue = asyncio.Queue()
        self.download_done = asyncio.Event()

    def _on_notification(self, sender: Any, data: bytearray):
        raw = bytes(data)
        if self.uses_gsp:
            if not raw:
                return
            resp_code = raw[0]
            if resp_code in (2, 3):  # GSP DATA or DATA_PART2
                self.download_queue.put_nowait(raw)
            else:
                self.response_queue.put_nowait(raw)
        else:
            # Whiteboard V9 frame
            payload = sf_decode(raw)
            if not payload or len(payload) < 6 or payload[0] != 0xA5:
                return
            msg_type = payload[1]
            if msg_type == WB_MSG_CLIENT_ON_NOTIFY:
                self.download_queue.put_nowait(payload[6:])
            else:
                self.response_queue.put_nowait(payload)

    async def connect(self, timeout: float = 12.0) -> bool:
        """Connects to sensor, auto-detects GSP vs Whiteboard V9, and performs handshake."""
        last_err = None
        for attempt in range(2):
            try:
                self.client = BleakClient(self.mac, timeout=timeout)
                await self.client.connect()
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    ensure_bluetooth_powered(cycle=True)
                    await asyncio.sleep(1.2)

        if last_err is not None:
            raise last_err

        # Check for GSP service
        service_uuids = [s.uuid.lower() for s in self.client.services]
        if GSP_SERVICE_UUID.lower() in service_uuids:
            self.uses_gsp = True
            await self.client.start_notify(GSP_NOTIFY_CHAR_UUID, self._on_notification)
            await asyncio.sleep(0.3)
            # Send GSP HELLO
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, bytearray([0, 100]), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                if len(resp) >= 4 and resp[0] == 1:
                    parts = resp[3:].decode('ascii', errors='ignore').split('\x00')
                    if parts:
                        self.serial_number = parts[0]
            except asyncio.TimeoutError:
                pass
        else:
            self.uses_gsp = False
            await self.client.start_notify(WB_NOTIFY_CHAR_UUID, self._on_notification)
            await asyncio.sleep(0.3)
            # Send Whiteboard HELLO
            hello_payload = bytearray(38)
            hello_payload[0] = 0x09; hello_payload[1] = 0x09
            hello_payload[3:15] = pack_serial("199999999999")
            hello_payload[15:19] = bytes([0x04, 0x01, 0x02, 0x00])
            packet = bytes([0xA5, WB_MSG_HELLO, 0x26, 0x00, 0x00, 0x00]) + bytes(hello_payload)
            await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(packet), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                body = resp[6:]
                if len(body) >= 15:
                    self.serial_number = unpack_serial(body[3:15])
            except asyncio.TimeoutError:
                pass

        return True

    async def disconnect(self):
        """Disconnects cleanly from the sensor."""
        if self.client and self.client.is_connected:
            try:
                char_uuid = GSP_NOTIFY_CHAR_UUID if self.uses_gsp else WB_NOTIFY_CHAR_UUID
                await self.client.stop_notify(char_uuid)
            except Exception:
                pass
            await self.client.disconnect()
        self.client = None

    async def configure_and_start_logging(self, ecg_enabled: bool = True) -> bool:
        """Sets UTC time, configures DataLogger (ECG optional + HR + Time), and starts logging."""
        utc_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)

        if self.uses_gsp:
            # 1. Set UTC Time
            time_bytes = struct.pack("<Q", utc_us)
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, bytearray([8, 101]) + time_bytes, response=True)
            await asyncio.sleep(0.2)

            # 2. Configure DataLogger: (ECG 125Hz optional) + /Meas/HR + /Time/Detailed
            config_data = bytearray([6, 102])
            if ecg_enabled:
                config_data.extend("/Meas/ECG/125/mV".encode() + b"\0")
            config_data.extend("/Meas/HR".encode() + b"\0")
            config_data.extend("/Time/Detailed".encode() + b"\0")
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, config_data, response=True)
            await asyncio.sleep(0.2)

            # 3. Start DataLogger (State 3 = DATALOGGER_LOGGING)
            while not self.response_queue.empty():
                self.response_queue.get_nowait()
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, bytearray([9, 103, 3]), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                if len(resp) >= 4:
                    status = struct.unpack("<H", resp[2:4])[0]
                    return status in (200, 202)
            except asyncio.TimeoutError:
                return False
        else:
            # Whiteboard V9 implementation
            payload = RES_ID_DATALOGGER_STATE + bytes([0x01, 0x00, 0x00, 0x01, 0x03, 0x00, 0x03])
            header = bytes([0xA5, WB_MSG_PROVIDER_PUT_REQUEST, len(payload), 0x00, 0x04, 0x00])
            await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                body = resp[6:]
                if len(body) >= 8:
                    status = struct.unpack("<H", body[6:8])[0]
                    return status in (200, 202)
            except asyncio.TimeoutError:
                return False

        return False

    async def stop_logging(self) -> bool:
        """Stops DataLogger (sets state 2 = DATALOGGER_READY)."""
        if self.uses_gsp:
            while not self.response_queue.empty():
                self.response_queue.get_nowait()
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, bytearray([9, 104, 2]), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                if len(resp) >= 4:
                    status = struct.unpack("<H", resp[2:4])[0]
                    return status in (200, 202)
            except asyncio.TimeoutError:
                return False
        else:
            payload = RES_ID_DATALOGGER_STATE + bytes([0x01, 0x00, 0x00, 0x01, 0x03, 0x00, 0x02])
            header = bytes([0xA5, WB_MSG_PROVIDER_PUT_REQUEST, len(payload), 0x00, 0x05, 0x00])
            await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                body = resp[6:]
                if len(body) >= 8:
                    status = struct.unpack("<H", body[6:8])[0]
                    return status in (200, 202)
            except asyncio.TimeoutError:
                return False

        return False

    async def get_latest_log_id(self) -> int:
        """Queries /Mem/Logbook/entries to determine the latest recorded log ID."""
        if self.uses_gsp:
            while not self.response_queue.empty():
                self.response_queue.get_nowait()
            cmd = bytearray([4, 105]) + b"/Mem/Logbook/entries\x00"
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, cmd, response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                if len(resp) >= 5 and resp[0] == 1:
                    status = struct.unpack("<H", resp[2:4])[0]
                    if status == 200:
                        count = resp[4]
                        entry_data = resp[5:]
                        log_ids = []
                        for i in range(0, len(entry_data), 16):
                            chunk = entry_data[i:i+16]
                            if len(chunk) == 16:
                                eid = struct.unpack("<I", chunk[0:4])[0]
                                log_ids.append(eid)
                        if log_ids:
                            latest = max(log_ids)
                            print(f"📋 Found {count} log entries on sensor: IDs {log_ids}, selecting latest ID {latest}")
                            return latest
            except asyncio.TimeoutError:
                print("⚠️ Timeout querying /Mem/Logbook/entries")
        return 1

    async def download_log_data(self, log_id: int, target_filepath: str, timeout: float = 20.0, 
                                progress_callback: Optional[Callable[[int], None]] = None) -> bool:
        """Downloads log file from sensor, verifying high-speed chunk stream until end marker."""
        while not self.download_queue.empty():
            self.download_queue.get_nowait()
        while not self.response_queue.empty():
            self.response_queue.get_nowait()
        self.download_done.clear()

        dirname = os.path.dirname(target_filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        downloaded = bytearray()

        if self.uses_gsp:
            cmd = bytearray([3, 106]) + struct.pack("<I", log_id)
            await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, cmd, response=True)
            # Check FETCH_LOG command acknowledgement
            try:
                init_resp = await asyncio.wait_for(self.response_queue.get(), timeout=5.0)
                if len(init_resp) >= 4 and init_resp[0] == 1:
                    status = struct.unpack("<H", init_resp[2:4])[0]
                    if status != 200:
                        print(f"❌ FETCH_LOG returned error status {status} for log ID {log_id}")
                        return False
            except asyncio.TimeoutError:
                print("⚠️ No ACK for FETCH_LOG, waiting for data stream...")

            chunks_received = 0
            stream_ended_cleanly = False
            while True:
                try:
                    packet = await asyncio.wait_for(self.download_queue.get(), timeout=timeout)
                    if len(packet) >= 6:
                        offset = struct.unpack("<I", packet[2:6])[0]
                        chunk = packet[6:]
                        if len(chunk) > 0:
                            chunks_received += 1
                            if len(downloaded) < offset + len(chunk):
                                downloaded.extend(b"\0" * (offset + len(chunk) - len(downloaded)))
                            downloaded[offset:offset + len(chunk)] = chunk
                            if progress_callback and chunks_received % 25 == 0:
                                progress_callback(len(downloaded))
                        else:
                            print(f"🏁 End-of-file marker received ({chunks_received} packets, {len(downloaded)} bytes).")
                            if progress_callback:
                                progress_callback(len(downloaded))
                            stream_ended_cleanly = True
                            break
                except asyncio.TimeoutError:
                    if chunks_received > 0:
                        print(f"⚠️ Stream pause/complete after {chunks_received} packets ({len(downloaded)} bytes).")
                    break

            # If stream ended by timeout rather than EOF marker, give the BLE GATT layer
            # a moment to settle before subsequent commands (avoids GATT Unlikely Error 0x0E)
            if not stream_ended_cleanly and chunks_received > 0:
                await asyncio.sleep(1.5)
                # Drain any late-arriving packets
                while not self.download_queue.empty():
                    self.download_queue.get_nowait()
        else:
            # Whiteboard V9 download
            res_id = bytes([0x00, 0x34, 0x05])
            payload = res_id + bytes([0x01, 0x00, 0x00, 0x00])
            header = bytes([0xA5, WB_MSG_PROVIDER_GET_REQUEST, len(payload), 0x00, 0x06, 0x00])
            await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
            while True:
                try:
                    chunk = await asyncio.wait_for(self.download_queue.get(), timeout=timeout)
                    if not chunk or len(chunk) < 4:
                        break
                    offset = struct.unpack("<I", chunk[0:4])[0]
                    data_bytes = chunk[4:]
                    if len(data_bytes) == 0:
                        break
                    if len(downloaded) < offset + len(data_bytes):
                        downloaded.extend(b"\0" * (offset + len(data_bytes) - len(downloaded)))
                    downloaded[offset:offset + len(data_bytes)] = data_bytes
                except asyncio.TimeoutError:
                    break

        if downloaded:
            with open(target_filepath, 'wb') as f:
                f.write(downloaded)

        return os.path.exists(target_filepath) and os.path.getsize(target_filepath) > 0

    async def clear_logbook(self) -> bool:
        """Erases all stored logs from the sensor internal memory (CLEAR_LOGBOOK).
        
        Retries once if a transient GATT error occurs after a data transfer.
        """
        if self.uses_gsp:
            for attempt in range(2):
                try:
                    while not self.response_queue.empty():
                        self.response_queue.get_nowait()
                    await self.client.write_gatt_char(GSP_WRITE_CHAR_UUID, bytearray([5, 107]), response=True)
                    resp = await asyncio.wait_for(self.response_queue.get(), timeout=8.0)
                    if len(resp) >= 4:
                        status = struct.unpack("<H", resp[2:4])[0]
                        return status in (200, 204)
                except asyncio.TimeoutError:
                    return False
                except Exception as e:
                    if attempt == 0:
                        print(f"⚠️ Clear attempt {attempt+1} failed ({type(e).__name__}), retrying after 2s...")
                        await asyncio.sleep(2.0)
                    else:
                        print(f"❌ Clear failed after retry: {type(e).__name__}: {e}")
                        return False
        else:
            payload = RES_ID_LOGBOOK_ENTRIES + bytes([0x01, 0x00, 0x00, 0x00])
            header = bytes([0xA5, WB_MSG_PROVIDER_DELETE_REQUEST, len(payload), 0x00, 0x07, 0x00])
            await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
            try:
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=6.0)
                body = resp[6:]
                if len(body) >= 8:
                    status = struct.unpack("<H", body[6:8])[0]
                    return status in (200, 204)
            except asyncio.TimeoutError:
                return False

        return False


# ==============================================================================
# SBEM Log Parser & CSV Exporter
# ==============================================================================

async def export_sbem_to_csv(sbem_filepath: str, user_id: str, ecg_csv_path: str, hr_csv_path: str,
                             ecg_enabled: bool = True) -> Dict[str, int]:
    """Parses raw Movesense SBEM file and exports calibrated ECG (optional) and HR/RR to CSV."""
    base_rel_ms = None
    base_utc_us = None
    ecg_samples = []
    hr_samples = []

    def data_cb(cid, b):
        nonlocal base_rel_ms, base_utc_us
        if cid == 107 and len(b) >= 12:  # TimeDetailed
            base_rel_ms = struct.unpack("<I", b[:4])[0]
            base_utc_us = struct.unpack("<Q", b[4:12])[0]
        elif cid == 68 and len(b) >= 4:   # ECG 125Hz chunk
            ts_ms = struct.unpack("<I", b[:4])[0]
            raw_vals = struct.unpack(f"<{(len(b) - 4) // 2}h", b[4:])
            for i, val in enumerate(raw_vals):
                t_sample = ts_ms + int(i * 1000 / 125)
                ecg_samples.append((t_sample, val * ECG_LSB_TO_MV))
        elif cid == 74 and len(b) >= 6:   # HR / RR interval chunk
            avg_hr = struct.unpack("<f", b[:4])[0]
            rrs = list(struct.unpack(f"<{(len(b) - 4) // 2}H", b[4:]))
            hr_samples.append((avg_hr, rrs))

    try:
        await read_chunks(sbem_filepath, data_callback=data_cb)
    except Exception as e:
        print(f"⚠️ Error reading SBEM chunks from {sbem_filepath}: {e}")

    start_dt = datetime.fromtimestamp(base_utc_us / 1_000_000, tz=timezone.utc) if base_utc_us else datetime.now(timezone.utc)
    # Use first HR timestamp as base if no ECG present
    base_ms = base_rel_ms if base_rel_ms is not None else (ecg_samples[0][0] if ecg_samples else 0)

    # 1. Write ECG CSV (only if ECG data was recorded)
    if ecg_enabled and ecg_samples:
        with open(ecg_csv_path, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "UserID", "SampleIndex", "ECG_mV"])
            for idx, (t_ms, mv) in enumerate(ecg_samples):
                dt = start_dt + timedelta(milliseconds=(t_ms - base_ms))
                writer.writerow([dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], user_id, idx, f"{mv:.4f}"])

    # 2. Write HR / RR CSV
    rr_count = 0
    t_rr_ms = base_ms
    with open(hr_csv_path, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "UserID", "HeartRate_bpm", "RR_Interval_ms"])
        for hr, rrs in hr_samples:
            for rr in rrs:
                t_rr_ms += rr
                dt = start_dt + timedelta(milliseconds=(t_rr_ms - base_ms))
                writer.writerow([dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], user_id, f"{hr:.1f}", rr])
                rr_count += 1

    return {'ecg_count': len(ecg_samples), 'rr_count': rr_count}


# ==============================================================================
# Main GUI Application
# ==============================================================================

class MovesenseApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Movesense Whiteboard DataLogger & Logbook Acquisition")
        self.resize(650, 750)

        # Application State
        self.sensors_to_connect = [] 
        self.is_recording = False
        self.session_id = ""
        self.event_counter = 1
        self.ecg_enabled = False  # Default: HR/RR only (faster, smaller files)

        self.init_ui()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Session ID ---
        session_layout = QHBoxLayout()
        session_layout.addWidget(QLabel("Session ID:"))
        self.session_input = QLineEdit()
        self.session_input.setPlaceholderText("e.g., Trial_001")
        session_layout.addWidget(self.session_input)
        main_layout.addLayout(session_layout)

        main_layout.addWidget(QLabel("--- Step 1: Discover Sensors ---"))

        # --- Scanner Controls ---
        self.scan_btn = QPushButton("🔍 Scan for Movesense Sensors")
        self.scan_btn.clicked.connect(self.scan_sensors)
        main_layout.addWidget(self.scan_btn)

        # --- Scanned Devices Table ---
        self.scanned_table = QTableWidget(0, 3)
        self.scanned_table.setHorizontalHeaderLabels(["Sensor Name", "MAC Address", "Assign User ID"])
        self.scanned_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.scanned_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.scanned_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        main_layout.addWidget(self.scanned_table)

        # --- Add All Button ---
        self.add_all_btn = QPushButton("⬇️ Add All Listed Sensors")
        self.add_all_btn.clicked.connect(self.add_all_scanned)
        main_layout.addWidget(self.add_all_btn)

        # --- Step 2: Ready to Connect ---
        main_layout.addWidget(QLabel("--- Step 2: Ready to Connect ---"))

        # --- Ready List ---
        self.sensor_list_ui = QListWidget()
        main_layout.addWidget(self.sensor_list_ui)

        # --- Step 2 Action Buttons ---
        ready_btn_layout = QHBoxLayout()
        self.remove_selected_btn = QPushButton("➖ Remove Selected")
        self.remove_selected_btn.clicked.connect(self.remove_selected_sensor)
        self.remove_all_btn = QPushButton("🗑️ Remove All Listed Sensors")
        self.remove_all_btn.clicked.connect(self.remove_all_sensors)
        ready_btn_layout.addWidget(self.remove_selected_btn)
        ready_btn_layout.addWidget(self.remove_all_btn)
        main_layout.addLayout(ready_btn_layout)

        # --- Recording Mode Selector ---
        mode_group = QGroupBox("Recording Mode")
        mode_layout = QHBoxLayout(mode_group)
        self.mode_btn_group = QButtonGroup(self)
        self.radio_hr_only = QRadioButton("💓 HR / RR only  (small file, fast download)")
        self.radio_hr_only.setChecked(True)
        self.radio_full = QRadioButton("📈 ECG + HR / RR  (full waveform, large file)")
        self.radio_hr_only.setToolTip(
            "Records only heart rate and RR intervals.\n"
            "~97% smaller file than full ECG mode.\n"
            "Recommended for HRV analysis."
        )
        self.radio_full.setToolTip(
            "Records the raw ECG waveform at 125 Hz plus HR/RR.\n"
            "Produces a ~170 KB SBEM file per 10 minutes.\n"
            "Use when you need the raw ECG signal."
        )
        self.mode_btn_group.addButton(self.radio_hr_only, 0)
        self.mode_btn_group.addButton(self.radio_full, 1)
        self.radio_hr_only.toggled.connect(self._on_mode_changed)
        mode_layout.addWidget(self.radio_hr_only)
        mode_layout.addWidget(self.radio_full)
        main_layout.addWidget(mode_group)

        # --- Recording Controls ---
        rec_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶ START WHITEBOARD RECORDING")
        self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        self.start_btn.clicked.connect(self.start_recording)

        self.stop_btn = QPushButton("⏹ STOP & DOWNLOAD LOGS")
        self.stop_btn.setStyleSheet("background-color: #F44336; color: white; font-weight: bold; padding: 10px;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_recording)

        rec_layout.addWidget(self.start_btn)
        rec_layout.addWidget(self.stop_btn)
        main_layout.addLayout(rec_layout)

        # --- Live Event Controls ---
        main_layout.addWidget(QLabel("--- Step 3: Live Event Marker ---"))
        self.add_event_btn = QPushButton("📍 Add Event (Event1)")
        self.add_event_btn.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 10px; font-size: 13px;")
        self.add_event_btn.setEnabled(False)
        self.add_event_btn.setShortcut("Ctrl+E")
        self.add_event_btn.setToolTip("Click or press Ctrl+E to log an incremental event marker")
        self.add_event_btn.clicked.connect(self.add_event)
        main_layout.addWidget(self.add_event_btn)

        self.event_status_label = QLabel("Ready. Scan sensors and assign User IDs to begin.")
        self.event_status_label.setStyleSheet("color: #666; font-style: italic;")
        main_layout.addWidget(self.event_status_label)

        # --- Activity Log Console ---
        main_layout.addWidget(QLabel("--- Activity Log ---"))
        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumHeight(150)
        self.log_console.setStyleSheet("background-color: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 11px;")
        main_layout.addWidget(self.log_console)

    def log(self, text: str):
        """Prints to console and appends to GUI Activity Log."""
        print(text)
        self.log_console.appendPlainText(text)
        sb = self.log_console.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _on_mode_changed(self):
        """Updates ecg_enabled state when recording mode radio changes."""
        self.ecg_enabled = self.radio_full.isChecked()

    def remove_selected_sensor(self):
        selected_row = self.sensor_list_ui.currentRow()
        if 0 <= selected_row < len(self.sensors_to_connect):
            del self.sensors_to_connect[selected_row]
            self.sensor_list_ui.takeItem(selected_row)

    def remove_all_sensors(self):
        self.sensors_to_connect.clear()
        self.sensor_list_ui.clear()

    def add_event(self):
        """Logs an event marker with current timestamp and incremented Event#."""
        if not self.is_recording:
            return

        event_name = f"Event{self.event_counter}"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        events_filename = f"{self.session_id}_events.csv"

        try:
            with open(events_filename, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([timestamp, event_name])

            print(f"[{timestamp}] 📍 Logged {event_name}")
            self.event_status_label.setText(f"✅ Last logged: {event_name} at {timestamp.split(' ')[1]}")
            self.event_counter += 1
            self.add_event_btn.setText(f"📍 Add Event (Event{self.event_counter})")
        except Exception as e:
            print(f"❌ Failed to log event: {e}")
            self.event_status_label.setText(f"❌ Error logging event: {e}")

    def get_next_default_user_id(self, existing_ids):
        counter = 1
        while f"User_{counter}".lower() in existing_ids:
            counter += 1
        return f"User_{counter}"

    @asyncSlot()
    async def scan_sensors(self):
        ensure_bluetooth_powered()
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning... Please wait 5 seconds...")
        self.scanned_table.setRowCount(0)

        try:
            devices = await BleakScanner.discover(timeout=5.0)
            existing_ids = {sensor['user'].lower() for sensor in self.sensors_to_connect}
            assigned_placeholders = set(existing_ids)
            row = 0
            for d in devices:
                if d.name and ("KANA" in d.name or "Movesense" in d.name):
                    self.scanned_table.insertRow(row)
                    self.scanned_table.setItem(row, 0, QTableWidgetItem(d.name))
                    self.scanned_table.setItem(row, 1, QTableWidgetItem(d.address))
                    
                    default_user = self.get_next_default_user_id(assigned_placeholders)
                    assigned_placeholders.add(default_user.lower())

                    user_input = QLineEdit()
                    user_input.setPlaceholderText(default_user)
                    self.scanned_table.setCellWidget(row, 2, user_input)
                    row += 1
                    
            if row == 0:
                QMessageBox.information(self, "Scan Complete", "No Movesense sensors found. Make sure they are awake!")
        except Exception as e:
            QMessageBox.warning(self, "Scan Error", f"An error occurred while scanning: {e}")
        finally:
            self.scan_btn.setEnabled(True)
            self.scan_btn.setText("🔍 Scan for Movesense Sensors")

    def add_all_scanned(self):
        existing_users_lower = {sensor['user'].lower(): sensor['user'] for sensor in self.sensors_to_connect}
        existing_macs = {sensor['mac'] for sensor in self.sensors_to_connect}

        new_entries = []
        new_users_seen = {}

        for row in range(self.scanned_table.rowCount()):
            mac = self.scanned_table.item(row, 1).text()
            name = self.scanned_table.item(row, 0).text()
            user_widget = self.scanned_table.cellWidget(row, 2)
            user_id = user_widget.text().strip() or user_widget.placeholderText()

            if mac in existing_macs:
                continue

            user_id_lower = user_id.lower()
            if user_id_lower in existing_users_lower:
                conflicting = existing_users_lower[user_id_lower]
                QMessageBox.warning(self, "Duplicate User ID", 
                    f"User ID '{user_id}' is already assigned to a sensor (as '{conflicting}').\n\nEach sensor must have a unique User ID.")
                return

            if user_id_lower in new_users_seen:
                QMessageBox.warning(self, "Duplicate User ID",
                    f"User ID '{user_id}' is entered multiple times in the table.\n\nEach sensor must have a unique User ID.")
                return

            new_users_seen[user_id_lower] = user_id
            new_entries.append({'mac': mac, 'user': user_id, 'name': name})

        if not new_entries and self.scanned_table.rowCount() > 0:
            QMessageBox.information(self, "Already Added", "All scanned sensors are already in the ready list.")
            return

        for entry in new_entries:
            self.sensors_to_connect.append(entry)
            self.sensor_list_ui.addItem(f"User: {entry['user']}  |  MAC: {entry['mac']}  ({entry.get('name', '')})")

        self.scanned_table.setRowCount(0)

    @asyncSlot()
    async def start_recording(self):
        """Starts DataLogger on all sensors and disconnects BLE to run in low-energy mode."""
        raw_session = self.session_input.text().strip()
        if not raw_session:
            QMessageBox.warning(self, "Error", "Please enter a Session ID.")
            return

        self.session_id = sanitize_filename_part(raw_session)
        if not self.sensors_to_connect:
            QMessageBox.warning(self, "Error", "Please add at least one sensor from the scan list.")
            return

        # Validate unique user IDs
        user_ids = [s['user'].lower() for s in self.sensors_to_connect]
        if len(user_ids) != len(set(user_ids)):
            QMessageBox.critical(self, "Duplicate User ID", "Duplicate User IDs detected. Each sensor must have a unique User ID.")
            return

        self.is_recording = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.session_input.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.add_all_btn.setEnabled(False)
        self.remove_all_btn.setEnabled(False)
        self.remove_selected_btn.setEnabled(False)
        # Lock mode selector for duration of session
        self.radio_hr_only.setEnabled(False)
        self.radio_full.setEnabled(False)

        # Initialize event marker CSV
        events_filename = f"{self.session_id}_events.csv"
        with open(events_filename, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Timestamp", "Event"])

        self.event_counter = 1
        self.add_event_btn.setText("📍 Add Event (Event1)")
        self.add_event_btn.setEnabled(True)
        mode_label = "ECG + HR/RR" if self.ecg_enabled else "HR/RR only"
        self.event_status_label.setText(f"Starting DataLogger on sensors [{mode_label}]...")

        started_sensors = []
        failed_sensors = []

        # Connect sequentially to avoid [org.bluez.Error.InProgress]
        for sensor in self.sensors_to_connect:
            user_id = sensor['user']
            mac = sensor['mac']
            self.log(f"\n🔄 Connecting to {user_id} ({mac}) via Whiteboard...")
            self.event_status_label.setText(f"Starting DataLogger on {user_id} ({mac})...")

            client = MovesenseSensorClient(mac)
            try:
                await client.connect()
                self.log(f"✅ Connected to {user_id} (Protocol: {'GSP' if client.uses_gsp else 'Whiteboard V9'})")

                streams = "ECG 125Hz + HR/RR + Time" if self.ecg_enabled else "HR/RR + Time"
                self.log(f"🚀 Configuring DataLogger ({streams}) and starting logging on {user_id}...")
                ok = await client.configure_and_start_logging(ecg_enabled=self.ecg_enabled)
                if ok:
                    self.log(f"✅ {user_id} DataLogger is now LOGGING internally [{mode_label}].")
                    started_sensors.append(user_id)
                else:
                    self.log(f"❌ {user_id} failed to transition to LOGGING.")
                    failed_sensors.append(user_id)
            except Exception as e:
                err_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                self.log(f"❌ Failed to start {user_id}: {err_desc}")
                failed_sensors.append(user_id)
            finally:
                # Disconnect BLE so the sensor logs in low-power mode
                await client.disconnect()
                self.log(f"🔌 BLE disconnected for {user_id} (Operating autonomously in Low-Power mode).")
                await asyncio.sleep(0.5)

        if not started_sensors:
            QMessageBox.critical(self, "Recording Failed", "Failed to start DataLogger on any sensor.")
            self.is_recording = False
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.session_input.setEnabled(True)
            self.scan_btn.setEnabled(True)
            self.add_all_btn.setEnabled(True)
            self.remove_all_btn.setEnabled(True)
            self.remove_selected_btn.setEnabled(True)
            self.add_event_btn.setEnabled(False)
            self.radio_hr_only.setEnabled(True)
            self.radio_full.setEnabled(True)
            self.event_status_label.setText("Ready.")
            return

        status_text = f"Sensors logging internally: {', '.join(started_sensors)} (Low-Power Mode Active)"
        if failed_sensors:
            status_text += f" | Failed: {', '.join(failed_sensors)}"
        self.event_status_label.setText(status_text)
        self.log(f"\n🎉 Session '{self.session_id}' is active! Sensors are recording internally in low-power mode.")

    @asyncSlot()
    async def stop_recording(self):
        """Reconnects, stops DataLogger, downloads SBEM logs, verifies integrity, exports CSVs, and safely clears sensor memory."""
        ensure_bluetooth_powered()
        self.is_recording = False
        self.stop_btn.setEnabled(False)
        self.add_event_btn.setEnabled(False)
        self.event_status_label.setText("Stopping sensors and retrieving logs via Whiteboard...")

        downloaded_files = []
        cleared_sensors = []

        for sensor in self.sensors_to_connect:
            user_id = sensor['user']
            mac = sensor['mac']
            self.log(f"\n🔄 Reconnecting to {user_id} ({mac}) to stop logging & retrieve data...")
            self.event_status_label.setText(f"Reconnecting to {user_id} ({mac})...")

            client = MovesenseSensorClient(mac)
            try:
                await client.connect()
                self.log(f"✅ Connected to {user_id}.")

                # 1. Stop DataLogger (State 2 = DATALOGGER_READY)
                self.log(f"⏹ Stopping DataLogger on {user_id}...")
                await client.stop_logging()
                await asyncio.sleep(0.4)

                # 2. Get latest log ID
                log_id = await client.get_latest_log_id()
                self.log(f"📋 Target Log ID: {log_id}")

                # 3. Download raw SBEM log file
                target_sbem = f"{self.session_id}_{sanitize_filename_part(user_id)}.sbem"
                target_ecg_csv = f"{self.session_id}_{sanitize_filename_part(user_id)}_ecg.csv"
                target_hr_csv = f"{self.session_id}_{sanitize_filename_part(user_id)}_hr_rr.csv"

                self.log(f"📥 Downloading Log ID {log_id} to {target_sbem}...")
                self.event_status_label.setText(f"📥 Downloading Log ID {log_id} from {user_id}...")
                
                def on_progress(bytes_dl):
                    self.event_status_label.setText(f"📥 Downloading {user_id}: {bytes_dl / 1024:.1f} KB received...")

                success = await client.download_log_data(log_id, target_sbem, progress_callback=on_progress)

                # 4. Integrity Verification & CSV Export
                file_size = os.path.getsize(target_sbem) if (os.path.exists(target_sbem) and success) else 0

                if file_size > 0:
                    self.log(f"✅ Verified log download: {target_sbem} ({file_size} bytes).")
                    self.event_status_label.setText(f"Parsing & exporting CSV for {user_id}...")
                    counts = await export_sbem_to_csv(
                        target_sbem, user_id, target_ecg_csv, target_hr_csv,
                        ecg_enabled=self.ecg_enabled
                    )
                    if self.ecg_enabled:
                        self.log(f"📊 Extracted {counts['ecg_count']} ECG samples and {counts['rr_count']} RR intervals.")
                        downloaded_files.append(
                            f"{target_sbem} ({file_size} bytes, {counts['ecg_count']} ECG, {counts['rr_count']} RR)"
                        )
                    else:
                        self.log(f"📊 Extracted {counts['rr_count']} RR intervals (HR/RR only mode).")
                        downloaded_files.append(
                            f"{target_sbem} ({file_size} bytes, {counts['rr_count']} RR intervals)"
                        )

                    # 5. Safe Memory Erase
                    self.log(f"🗑️ Safely erasing internal sensor memory on {user_id}...")
                    erased = await client.clear_logbook()
                    if erased:
                        cleared_sensors.append(user_id)
                        self.log(f"✅ Sensor memory cleared successfully for {user_id}.")
                    else:
                        self.log(f"⚠️ Erase command unacknowledged by {user_id}.")
                else:
                    self.log(f"⚠️ Download incomplete or empty for {user_id}. Sensor internal memory NOT cleared to prevent data loss!")

            except Exception as e:
                err_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                self.log(f"❌ Error during stop/retrieve for {user_id}: {err_desc}")
            finally:
                await client.disconnect()
                await asyncio.sleep(0.5)

        summary_text = (
            f"Session '{self.session_id}' finished.\n\n"
            f"Events Logged: {self.event_counter - 1} ({self.session_id}_events.csv)\n\n"
            f"Downloaded & Exported Files:\n" + 
            ("\n".join(f"- {f}" for f in downloaded_files) if downloaded_files else "None") + "\n\n"
            f"Sensors Safely Cleared: {', '.join(cleared_sensors) if cleared_sensors else 'None'}"
        )

        QMessageBox.information(self, "Recording Complete", summary_text)
        self.event_status_label.setText(f"Session finished. Total events logged: {self.event_counter - 1}")

        # Reset UI
        self.start_btn.setEnabled(True)
        self.session_input.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.add_all_btn.setEnabled(True)
        self.remove_all_btn.setEnabled(True)
        self.remove_selected_btn.setEnabled(True)
        self.radio_hr_only.setEnabled(True)
        self.radio_full.setEnabled(True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MovesenseApp()
    window.show()

    with loop:
        loop.run_forever()
