import sys
import csv
import asyncio
from datetime import datetime
from bleak import BleakClient, BleakScanner
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QListWidget, QMessageBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView)
from qasync import QEventLoop, asyncSlot

HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

class MovesenseApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Movesense ECG & RR Recorder")
        self.resize(600, 650)

        # Application State
        self.sensors_to_connect = [] 
        self.active_clients = []     
        self.is_recording = False
        self.session_id = ""

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

        main_layout.addWidget(QLabel("--- Step 2: Ready to Connect ---"))

        # --- Ready List ---
        self.sensor_list_ui = QListWidget()
        main_layout.addWidget(self.sensor_list_ui)

        # --- Controls ---
        self.start_btn = QPushButton("▶ START RECORDING")
        self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        self.start_btn.clicked.connect(self.start_recording) 

        self.stop_btn = QPushButton("⏹ STOP RECORDING")
        self.stop_btn.setStyleSheet("background-color: #F44336; color: white; font-weight: bold; padding: 10px;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_recording)

        main_layout.addWidget(self.start_btn)
        main_layout.addWidget(self.stop_btn)

    @asyncSlot()
    async def scan_sensors(self):
        """Scans for BLE devices and populates the table with Movesense sensors."""
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning... Please wait 5 seconds...")
        self.scanned_table.setRowCount(0) # Clear previous scan results

        try:
            devices = await BleakScanner.discover(timeout=5.0)
            
            row = 0
            for d in devices:
                # Filter for Movesense devices
                if d.name and "Movesens" in d.name:
                    self.scanned_table.insertRow(row)
                    
                    # Add Name and MAC
                    self.scanned_table.setItem(row, 0, QTableWidgetItem(d.name))
                    self.scanned_table.setItem(row, 1, QTableWidgetItem(d.address))
                    
                    # Add a Text Input for User ID
                    user_input = QLineEdit()
                    user_input.setPlaceholderText(f"User_{row+1}")
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
        """Iterates through the table, grabs the User IDs, and stages them for connection."""
        added_count = 0
        
        for row in range(self.scanned_table.rowCount()):
            mac = self.scanned_table.item(row, 1).text()
            
            # Extract text from the QLineEdit widget in the 3rd column
            user_widget = self.scanned_table.cellWidget(row, 2)
            user_id = user_widget.text().strip()
            
            # Fallback to the placeholder (e.g., "User_1") if they left it blank
            if not user_id:
                user_id = user_widget.placeholderText()

            # Prevent adding duplicate MAC addresses
            if not any(sensor['mac'] == mac for sensor in self.sensors_to_connect):
                self.sensors_to_connect.append({'mac': mac, 'user': user_id})
                self.sensor_list_ui.addItem(f"User: {user_id}  |  MAC: {mac}")
                added_count += 1

        if added_count > 0:
            # Clear the table after successfully adding them to the lower list
            self.scanned_table.setRowCount(0)

    def create_hr_handler(self, user_id, session_filename):
        def hr_data_handler(sender, data):
            if not self.is_recording:
                return

            flags = data[0]
            hr_format = flags & 0x01
            rr_present = (flags & 0x10) >> 4

            offset = 1
            if hr_format == 0:
                hr = data[offset]
                offset += 1
            else:
                hr = int.from_bytes(data[offset:offset+2], byteorder='little')
                offset += 2

            if flags & 0x08:
                offset += 2

            if rr_present:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                
                with open(session_filename, mode='a', newline='') as file:
                    writer = csv.writer(file)
                    while offset < len(data):
                        rr_raw = int.from_bytes(data[offset:offset+2], byteorder='little')
                        rr_ms = (rr_raw / 1024.0) * 1000.0
                        
                        writer.writerow([timestamp, user_id, hr, round(rr_ms, 1)])
                        print(f"[{user_id}] Logged RR: {rr_ms:.1f} ms")
                        offset += 2

        return hr_data_handler

    async def connect_to_device(self, mac, user_id, session_filename):
        print(f"Connecting to {user_id} ({mac})...")
        try:
            client = BleakClient(mac)
            await client.connect()
            self.active_clients.append(client)
            print(f"✅ Connected to {user_id}")

            handler = self.create_hr_handler(user_id, session_filename)
            await client.start_notify(HR_MEASUREMENT_UUID, handler)
            
        except Exception as e:
            print(f"❌ Failed to connect to {user_id}: {e}")

    @asyncSlot()
    async def start_recording(self):
        self.session_id = self.session_input.text().strip()
        
        if not self.session_id:
            QMessageBox.warning(self, "Error", "Please enter a Session ID.")
            return
        if not self.sensors_to_connect:
            QMessageBox.warning(self, "Error", "Please add at least one sensor from the scan list.")
            return

        self.is_recording = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.session_input.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.add_all_btn.setEnabled(False)

        session_filename = f"{self.session_id}.csv"
        with open(session_filename, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Timestamp", "UserID", "HeartRate_bpm", "RR_Interval_ms"])

        print(f"Started recording session: {self.session_id}")

        tasks = []
        for sensor in self.sensors_to_connect:
            tasks.append(self.connect_to_device(sensor['mac'], sensor['user'], session_filename))
        
        await asyncio.gather(*tasks)

    @asyncSlot()
    async def stop_recording(self):
        print("Stopping recording and disconnecting sensors...")
        self.is_recording = False
        self.stop_btn.setEnabled(False)
        
        # Disconnect clients
        for client in self.active_clients:
            if client.is_connected:
                await client.stop_notify(HR_MEASUREMENT_UUID)
                await client.disconnect()
                
        self.active_clients.clear()
        print(f"Session {self.session_id} saved successfully.")
        QMessageBox.information(self, "Saved", f"Data saved to {self.session_id}.csv")
        
        # Reset UI elements
        self.start_btn.setEnabled(True)
        self.session_input.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.add_all_btn.setEnabled(True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MovesenseApp()
    window.show()

    with loop:
        loop.run_forever()
