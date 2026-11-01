import sys
import os
import csv
import zlib
import struct
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Any
from bleak import BleakClient, BleakScanner
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QListWidget, QMessageBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView)
from qasync import QEventLoop, asyncSlot

# ==============================================================================
# Movesense Whiteboard BLE Constants & Utilities
# ==============================================================================

WB_SERVICE_UUID = "0000fdf3-0000-1000-8000-00805f9b34fb"
WB_WRITE_CHAR_UUID = "6b200001-ff4e-4979-8186-fb7ba486fcd7"
WB_NOTIFY_CHAR_UUID = "6b200002-ff4e-4979-8186-fb7ba486fcd7"

# Whiteboard Protocol V9 Message Types
WB_MSG_CLIENT_ON_GET_RESOURCE_RESULT = 0x02
WB_MSG_CLIENT_ON_GET_RESULT = 0x05
WB_MSG_CLIENT_ON_PUT_RESULT = 0x07
WB_MSG_CLIENT_ON_POST_RESULT = 0x08
WB_MSG_CLIENT_ON_DELETE_RESULT = 0x19
WB_MSG_CLIENT_ON_NOTIFY = 0x14

WB_MSG_RESOURCE_GET_REQUEST = 0x0A
WB_MSG_PROVIDER_GET_REQUEST = 0x0D
WB_MSG_PROVIDER_PUT_REQUEST = 0x0E
WB_MSG_PROVIDER_POST_REQUEST = 0x0F
WB_MSG_PROVIDER_DELETE_REQUEST = 0x17
WB_MSG_PROVIDER_SUBSCRIBE_REQUEST = 0x11
WB_MSG_PROVIDER_UNSUBSCRIBE_REQUEST = 0x13

WB_MSG_HELLO = 0x12
WB_MSG_HELLO_ACK = 0x13
WB_MSG_DONT_UNDERSTAND = 0x1A

# Known Static Resource IDs (Little Endian 3-byte representation)
RES_ID_DATALOGGER_CONFIG = bytes([0xF0, 0x33, 0x00])  # /Mem/DataLogger/Config
RES_ID_DATALOGGER_STATE  = bytes([0xF0, 0x33, 0x01])  # /Mem/DataLogger/State
RES_ID_LOGBOOK_ENTRIES   = bytes([0xF0, 0x34, 0x00])  # /Mem/Logbook/Entries

# Whiteboard Type IDs
WB_TYPE_UINT8 = 0x0003
WB_TYPE_INT32 = 0x0006


def sf_encode(payload: bytes) -> bytes:
    """Encodes a payload with HDLC framing (0x7E) and CRC32 IEEE LE."""
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
    """Decodes an HDLC framed packet and verifies CRC32 IEEE."""
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
    if recvd_crc != calc_crc:
        return b""
    return payload


def pack_serial(serial_str: str) -> bytes:
    """Packs a 12-char Suunto serial into 12 6-bit packed bytes (Suunto standard)."""
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
    """Unpacks 12 6-bit packed bytes into a 12-char Suunto serial string."""
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
# Movesense Whiteboard Client
# ==============================================================================

class MovesenseWhiteboardClient:
    """Async Whiteboard Protocol V9 client over BLE GATT for Movesense sensors."""

    def __init__(self, mac: str, client_id: int = 1):
        self.mac = mac
        self.client_id = client_id
        self.client: Optional[BleakClient] = None
        self.req_id_counter = 1
        self.pending_requests: Dict[int, asyncio.Future] = {}
        self.serial_number = ""
        self.data_stream_queue: asyncio.Queue = asyncio.Queue()

    def _next_req_id(self) -> int:
        req_id = self.req_id_counter
        self.req_id_counter = (self.req_id_counter + 1) & 0xFFFF
        if self.req_id_counter == 0:
            self.req_id_counter = 1
        return req_id

    def _on_gatt_notification(self, sender: Any, data: bytearray):
        payload = sf_decode(bytes(data))
        if not payload or len(payload) < 6:
            return

        sync = payload[0]
        if sync != 0xA5:
            return

        msg_type = payload[1]
        payload_len = struct.unpack("<H", payload[2:4])[0]
        req_id = struct.unpack("<H", payload[4:6])[0]
        body = payload[6:6 + payload_len]

        # Handle HELLO_ACK
        if msg_type == WB_MSG_HELLO_ACK:
            fut = self.pending_requests.pop(0, None)
            if fut and not fut.done():
                fut.set_result({'type': msg_type, 'body': body})
            return

        # Handle Notifications (Log data stream or live subscriptions)
        if msg_type == WB_MSG_CLIENT_ON_NOTIFY:
            self.data_stream_queue.put_nowait(body)
            return

        # Handle Request responses
        fut = self.pending_requests.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result({'type': msg_type, 'body': body})

    async def connect(self, timeout: float = 12.0) -> bool:
        """Connects to the sensor, enables Whiteboard notifications, and performs HELLO."""
        self.client = BleakClient(self.mac, timeout=timeout)
        await self.client.connect()
        await self.client.start_notify(WB_NOTIFY_CHAR_UUID, self._on_gatt_notification)
        await asyncio.sleep(0.3)

        # Send HELLO Handshake
        fut = asyncio.get_running_loop().create_future()
        self.pending_requests[0] = fut

        hello_payload = bytearray(38)
        hello_payload[0] = 0x09
        hello_payload[1] = 0x09
        hello_payload[2] = 0x00
        hello_payload[3:15] = pack_serial("199999999999")  # Host dummy serial
        hello_payload[15:19] = bytes([0x04, 0x01, 0x02, 0x00])  # Version 2.1.4
        hello_payload[23] = 0x04
        packet = bytes([0xA5, WB_MSG_HELLO, 0x26, 0x00, 0x00, 0x00]) + bytes(hello_payload)

        await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(packet), response=True)
        try:
            resp = await asyncio.wait_for(fut, timeout=5.0)
            ack_body = resp['body']
            if len(ack_body) >= 15:
                self.serial_number = unpack_serial(ack_body[3:15])
            return True
        except asyncio.TimeoutError:
            self.pending_requests.pop(0, None)
            raise RuntimeError(f"Whiteboard HELLO handshake timed out for {self.mac}")

    async def disconnect(self):
        """Disconnects cleanly from the sensor."""
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(WB_NOTIFY_CHAR_UUID)
            except Exception:
                pass
            await self.client.disconnect()
        self.client = None

    async def resolve_resource(self, path: str, timeout: float = 5.0) -> Optional[bytes]:
        """Resolves a whiteboard resource path string into a 3-byte ResourceId."""
        req_id = self._next_req_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = fut

        path_bytes = path.encode('ascii')
        client_le = struct.pack("<H", self.client_id)
        req_payload = bytes([0x00]) + client_le + bytes([len(path_bytes)]) + path_bytes
        req_header = bytes([0xA5, WB_MSG_RESOURCE_GET_REQUEST, len(req_payload), 0x00]) + struct.pack("<H", req_id)

        await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(req_header + req_payload), response=True)
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            body = resp['body']
            if len(body) >= 8:
                status_code = struct.unpack("<H", body[6:8])[0]
                if status_code == 200:
                    return body[0:3]
            return None
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            return None

    async def get_datalogger_state(self, timeout: float = 5.0) -> int:
        """Reads the current DataLogger state (2 = Ready, 3 = Logging)."""
        req_id = self._next_req_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = fut

        client_bytes = bytes([self.client_id & 0xFF, (self.client_id >> 8) & 0xFF, 0x00])
        payload = RES_ID_DATALOGGER_STATE + client_bytes + bytes([0x00])  # 0 parameters
        header = bytes([0xA5, WB_MSG_PROVIDER_GET_REQUEST, len(payload), 0x00]) + struct.pack("<H", req_id)

        await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            body = resp['body']
            if len(body) >= 11:
                status_code = struct.unpack("<H", body[6:8])[0]
                if status_code == 200:
                    return body[10]  # uint8 state value
            return 1  # Unknown/Invalid
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            raise RuntimeError(f"Failed to get DataLogger state for {self.mac}")

    async def set_datalogger_state(self, new_state: int, timeout: float = 6.0) -> bool:
        """Sets the DataLogger state (3 = Start Logging, 2 = Stop Logging / Ready)."""
        req_id = self._next_req_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = fut

        client_bytes = bytes([self.client_id & 0xFF, (self.client_id >> 8) & 0xFF, 0x00])
        param_payload = bytes([0x01, 0x03, 0x00, new_state & 0xFF])  # 1 param, WB_TYPE_UINT8, value
        payload = RES_ID_DATALOGGER_STATE + client_bytes + param_payload
        header = bytes([0xA5, WB_MSG_PROVIDER_PUT_REQUEST, len(payload), 0x00]) + struct.pack("<H", req_id)

        await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            body = resp['body']
            if len(body) >= 8:
                status_code = struct.unpack("<H", body[6:8])[0]
                return status_code in (200, 202)
            return False
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            raise RuntimeError(f"Timeout setting DataLogger state {new_state} for {self.mac}")

    async def get_logbook_entries(self, timeout: float = 6.0) -> List[Dict[str, Any]]:
        """Queries /Mem/Logbook/Entries to list all saved log files on the sensor."""
        req_id = self._next_req_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = fut

        client_bytes = bytes([self.client_id & 0xFF, (self.client_id >> 8) & 0xFF, 0x00])
        payload = RES_ID_LOGBOOK_ENTRIES + client_bytes + bytes([0x00])  # 0 parameters
        header = bytes([0xA5, WB_MSG_PROVIDER_GET_REQUEST, len(payload), 0x00]) + struct.pack("<H", req_id)

        entries = []
        await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            body = resp['body']
            if len(body) >= 8:
                status_code = struct.unpack("<H", body[6:8])[0]
                if status_code == 200 and len(body) > 10:
                    raw_data = body[8:]
                    if len(raw_data) >= 12:
                        entry_id = struct.unpack("<I", raw_data[4:8])[0]
                        if entry_id > 0:
                            entries.append({'id': entry_id, 'size': 0})
            return entries
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            return entries

    async def download_log_data(self, log_id: int, target_filepath: str, timeout: float = 30.0) -> bool:
        """Downloads log file data from the sensor and saves it to the target file."""
        resource_path = f"/Mem/Logbook/byId/{log_id}/Data"
        res_id = await self.resolve_resource(resource_path)
        if not res_id:
            return False

        # Drain previous stream data
        while not self.data_stream_queue.empty():
            self.data_stream_queue.get_nowait()

        # Subscribe / Request data streaming
        req_id = self._next_req_id()
        client_bytes = bytes([self.client_id & 0xFF, (self.client_id >> 8) & 0xFF, 0x00])
        payload = res_id + client_bytes + bytes([0x00])
        header = bytes([0xA5, WB_MSG_PROVIDER_GET_REQUEST, len(payload), 0x00]) + struct.pack("<H", req_id)

        dirname = os.path.dirname(target_filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        with open(target_filepath, 'wb') as f_out:
            await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
            while True:
                try:
                    chunk = await asyncio.wait_for(self.data_stream_queue.get(), timeout=timeout)
                    if not chunk or len(chunk) < 4:
                        break
                    offset = struct.unpack("<I", chunk[0:4])[0]
                    data_bytes = chunk[4:]
                    if len(data_bytes) == 0:
                        break  # End marker
                    f_out.seek(offset)
                    f_out.write(data_bytes)
                except asyncio.TimeoutError:
                    break

        return os.path.exists(target_filepath) and os.path.getsize(target_filepath) > 0

    async def clear_logbook(self, timeout: float = 8.0) -> bool:
        """Erases all stored logs from the sensor logbook memory (DELETE /Mem/Logbook/Entries)."""
        req_id = self._next_req_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = fut

        client_bytes = bytes([self.client_id & 0xFF, (self.client_id >> 8) & 0xFF, 0x00])
        payload = RES_ID_LOGBOOK_ENTRIES + client_bytes + bytes([0x00])
        header = bytes([0xA5, WB_MSG_PROVIDER_DELETE_REQUEST, len(payload), 0x00]) + struct.pack("<H", req_id)

        await self.client.write_gatt_char(WB_WRITE_CHAR_UUID, sf_encode(header + payload), response=True)
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            body = resp['body']
            if len(body) >= 8:
                status_code = struct.unpack("<H", body[6:8])[0]
                return status_code in (200, 204)
            return False
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            raise RuntimeError(f"Timeout clearing logbook memory for {self.mac}")


# ==============================================================================
# Main GUI Application
# ==============================================================================

class MovesenseApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Movesense Whiteboard DataLogger & Logbook Acquisition")
        self.resize(640, 740)

        # Application State
        self.sensors_to_connect = [] 
        self.is_recording = False
        self.session_id = ""
        self.event_counter = 1

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

        self.event_status_label = QLabel("No events logged.")
        self.event_status_label.setStyleSheet("color: #666; font-style: italic;")
        main_layout.addWidget(self.event_status_label)

    def remove_selected_sensor(self):
        """Removes the selected sensor from Step 2."""
        selected_row = self.sensor_list_ui.currentRow()
        if 0 <= selected_row < len(self.sensors_to_connect):
            del self.sensors_to_connect[selected_row]
            self.sensor_list_ui.takeItem(selected_row)

    def remove_all_sensors(self):
        """Removes all listed sensors from Step 2."""
        self.sensors_to_connect.clear()
        self.sensor_list_ui.clear()

    def add_event(self):
        """Logs an event marker with the current timestamp and incremented Event#."""
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
        """Returns the next unique default User_N identifier."""
        counter = 1
        while f"User_{counter}".lower() in existing_ids:
            counter += 1
        return f"User_{counter}"

    @asyncSlot()
    async def scan_sensors(self):
        """Scans for BLE devices and populates the table with Movesense sensors."""
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
        """Validates User IDs and MAC addresses, ensuring all User IDs are unique, then stages them."""
        existing_users_lower = {sensor['user'].lower(): sensor['user'] for sensor in self.sensors_to_connect}
        existing_macs = {sensor['mac'] for sensor in self.sensors_to_connect}

        new_entries = []
        new_users_seen = {}

        for row in range(self.scanned_table.rowCount()):
            mac = self.scanned_table.item(row, 1).text()
            name = self.scanned_table.item(row, 0).text()
            user_widget = self.scanned_table.cellWidget(row, 2)
            user_id = user_widget.text().strip()
            
            if not user_id:
                user_id = user_widget.placeholderText()

            if mac in existing_macs:
                continue

            user_id_lower = user_id.lower()

            if user_id_lower in existing_users_lower:
                conflicting = existing_users_lower[user_id_lower]
                QMessageBox.warning(
                    self,
                    "Duplicate User ID",
                    f"User ID '{user_id}' is already assigned to a sensor in the ready list (as '{conflicting}').\n\n"
                    "Each sensor must have a unique User ID."
                )
                return

            if user_id_lower in new_users_seen:
                QMessageBox.warning(
                    self,
                    "Duplicate User ID",
                    f"User ID '{user_id}' is entered multiple times in the table.\n\n"
                    "Each sensor must have a unique User ID."
                )
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
        """Notifies Whiteboard on each sensor to start internal DataLogger logging, then disconnects BLE."""
        self.session_id = self.session_input.text().strip()
        
        if not self.session_id:
            QMessageBox.warning(self, "Error", "Please enter a Session ID.")
            return
        if not self.sensors_to_connect:
            QMessageBox.warning(self, "Error", "Please add at least one sensor from the scan list.")
            return

        # Double check User ID uniqueness
        user_ids_lower = [sensor['user'].lower() for sensor in self.sensors_to_connect]
        if len(user_ids_lower) != len(set(user_ids_lower)):
            QMessageBox.critical(self, "Duplicate User ID", "Duplicate User IDs detected in the ready list.")
            return

        self.is_recording = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.session_input.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.add_all_btn.setEnabled(False)
        self.remove_all_btn.setEnabled(False)
        self.remove_selected_btn.setEnabled(False)

        # Initialize event log CSV
        events_filename = f"{self.session_id}_events.csv"
        with open(events_filename, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Timestamp", "Event"])

        self.event_counter = 1
        self.add_event_btn.setText("📍 Add Event (Event1)")
        self.add_event_btn.setEnabled(True)
        self.event_status_label.setText("Starting sensors via Movesense Whiteboard...")

        started_sensors = []
        failed_sensors = []

        # Connect to each sensor sequentially and start Whiteboard DataLogger
        for sensor in self.sensors_to_connect:
            user_id = sensor['user']
            mac = sensor['mac']
            print(f"\n🔄 Connecting to {user_id} ({mac}) via Whiteboard...")
            self.event_status_label.setText(f"Connecting to {user_id} ({mac})...")
            
            wb = MovesenseWhiteboardClient(mac)
            try:
                await wb.connect()
                print(f"✅ Handshake OK with {user_id} (Serial: {wb.serial_number})")
                
                # Command Whiteboard DataLogger to start logging (State 3 = DATALOGGER_LOGGING)
                print(f"🚀 Starting DataLogger on {user_id}...")
                success = await wb.set_datalogger_state(3)
                if success:
                    print(f"✅ {user_id} DataLogger is now LOGGING internally.")
                    started_sensors.append(user_id)
                else:
                    print(f"❌ {user_id} refused state change to LOGGING.")
                    failed_sensors.append(user_id)

            except Exception as e:
                print(f"❌ Failed to start Whiteboard on {user_id}: {e}")
                failed_sensors.append(user_id)

            finally:
                # Disconnect to enter low-energy autonomous logging mode
                await wb.disconnect()
                print(f"🔌 BLE disconnected for {user_id} (Sensor operating autonomously in low-power mode).")
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
            self.event_status_label.setText("Ready.")
            return

        status_msg = f"Sensors logging internally: {', '.join(started_sensors)} (Low-Power Mode Active)"
        if failed_sensors:
            status_msg += f" | Failed: {', '.join(failed_sensors)}"
        self.event_status_label.setText(status_msg)
        print(f"\n🎉 Session '{self.session_id}' is running! Sensors are logging in low-power mode.")

    @asyncSlot()
    async def stop_recording(self):
        """Reconnects to each sensor, stops logging, downloads data, verifies file, and clears sensor memory."""
        self.is_recording = False
        self.stop_btn.setEnabled(False)
        self.add_event_btn.setEnabled(False)
        self.event_status_label.setText("Stopping sensors and retrieving logs via Whiteboard...")

        downloaded_files = []
        cleared_sensors = []

        for sensor in self.sensors_to_connect:
            user_id = sensor['user']
            mac = sensor['mac']
            print(f"\n🔄 Reconnecting to {user_id} ({mac}) to stop logging & retrieve data...")
            self.event_status_label.setText(f"Retrieving data from {user_id} ({mac})...")

            wb = MovesenseWhiteboardClient(mac)
            try:
                await wb.connect()
                print(f"✅ Connected to {user_id}.")

                # Step 1: Stop DataLogger (State 2 = DATALOGGER_READY)
                print(f"⏹ Stopping DataLogger on {user_id}...")
                await wb.set_datalogger_state(2)
                await asyncio.sleep(0.5)

                # Step 2: Query Logbook entries
                entries = await wb.get_logbook_entries()
                print(f"📋 Found {len(entries)} log entries on {user_id}.")

                log_id = entries[-1]['id'] if entries else 1
                target_filename = f"{self.session_id}_{user_id}.sbem"

                # Step 3: Download log data
                print(f"📥 Downloading Log ID {log_id} to {target_filename}...")
                downloaded = await wb.download_log_data(log_id, target_filename)

                # Step 4: Integrity Verification
                file_exists = os.path.exists(target_filename)
                file_size = os.path.getsize(target_filename) if file_exists else 0

                if file_exists and file_size > 0:
                    print(f"✅ Log file verified: {target_filename} ({file_size} bytes).")
                    downloaded_files.append(f"{target_filename} ({file_size} bytes)")

                    # Step 5: Safe Erase
                    print(f"🗑️ Safely clearing internal logbook memory on {user_id}...")
                    erased = await wb.clear_logbook()
                    if erased:
                        cleared_sensors.append(user_id)
                        print(f"✅ Internal sensor memory successfully erased for {user_id}.")
                else:
                    print(f"⚠️ Log download incomplete for {user_id}. Sensor memory NOT cleared to prevent data loss.")

            except Exception as e:
                print(f"❌ Error stopping/downloading from {user_id}: {e}")

            finally:
                await wb.disconnect()
                await asyncio.sleep(0.5)

        summary_text = (
            f"Session '{self.session_id}' finished.\n\n"
            f"Events Logged: {self.event_counter - 1} ({self.session_id}_events.csv)\n\n"
            f"Downloaded Files:\n" + ("\n".join(f"- {f}" for f in downloaded_files) if downloaded_files else "None") + "\n\n"
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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MovesenseApp()
    window.show()

    with loop:
        loop.run_forever()
