import sys
import csv
import asyncio
from datetime import datetime
from bleak import BleakClient
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QListWidget, QMessageBox)
from qasync import QEventLoop, asyncSlot

HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

class MovesenseApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Movesense ECG & RR Recorder")
        self.resize(500, 400)

        # Application State
        self.sensors_to_connect = [] # Stores dicts: {'mac': mac, 'user': user}
        self.active_clients = []     # Stores active BleakClient instances
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

        main_layout.addWidget(QLabel("--- Add Sensors ---"))

        # --- Sensor Input ---
        sensor_layout = QHBoxLayout()
        self.mac_input = QLineEdit()
        self.mac_input.setPlaceholderText("MAC Address (AA:BB:CC...)")
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("User ID (e.g., Alice)")
        
        add_btn = QPushButton("Add Sensor")
        add_btn.clicked.connect(self.add_sensor)

        sensor_layout.addWidget(self.mac_input)
        sensor_layout.addWidget(self.user_input)
        sensor_layout.addWidget(add_btn)
        main_layout.addLayout(sensor_layout)

        # --- Sensor List ---
        self.sensor_list_ui = QListWidget()
        main_layout.addWidget(self.sensor_list_ui)

        # --- Controls ---
        self.start_btn = QPushButton("▶ START RECORDING")
        self.start_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        # Note the @asyncSlot decorator used later allows this button to trigger async code
        self.start_btn.clicked.connect(self.start_recording) 

        self.stop_btn = QPushButton("⏹ STOP RECORDING")
        self.stop_btn.setStyleSheet("background-color: #F44336; color: white; font-weight: bold; padding: 10px;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_recording)

        main_layout.addWidget(self.start_btn)
        main_layout.addWidget(self.stop_btn)

    def add_sensor(self):
        mac = self.mac_input.text().strip().upper()
        user = self.user_input.text().strip()

        if not mac or not user:
            QMessageBox.warning(self, "Input Error", "Please provide both MAC and User ID.")
            return

        self.sensors_to_connect.append({'mac': mac, 'user': user})
        self.sensor_list_ui.addItem(f"User: {user} | MAC: {mac}")
        
        self.mac_input.clear()
        self.user_input.clear()

    def create_hr_handler(self, user_id, session_filename):
        """Creates a customized BLE handler that writes directly to the CSV."""
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
                
                # Open CSV in append mode ('a')
                with open(session_filename, mode='a', newline='') as file:
                    writer = csv.writer(file)
                    while offset < len(data):
                        rr_raw = int.from_bytes(data[offset:offset+2], byteorder='little')
                        rr_ms = (rr_raw / 1024.0) * 1000.0
                        
                        # Write row: Timestamp, UserID, HR, RR(ms)
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
            QMessageBox.warning(self, "Connection Error", f"Failed to connect to {user_id}.")

    @asyncSlot()
    async def start_recording(self):
        self.session_id = self.session_input.text().strip()
        
        if not self.session_id:
            QMessageBox.warning(self, "Error", "Please enter a Session ID.")
            return
        if not self.sensors_to_connect:
            QMessageBox.warning(self, "Error", "Please add at least one sensor.")
            return

        self.is_recording = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.session_input.setEnabled(False)

        # Setup the CSV file with headers
        session_filename = f"{self.session_id}.csv"
        with open(session_filename, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Timestamp", "UserID", "HeartRate_bpm", "RR_Interval_ms"])

        print(f"Started recording session: {self.session_id}")

        # Connect to all sensors concurrently
        tasks = []
        for sensor in self.sensors_to_connect:
            tasks.append(self.connect_to_device(sensor['mac'], sensor['user'], session_filename))
        
        await asyncio.gather(*tasks)

    @asyncSlot()
    async def stop_recording(self):
        print("Stopping recording and disconnecting sensors...")
        self.is_recording = False
        self.stop_btn.setEnabled(False)
        self.start_btn.setEnabled(True)
        self.session_input.setEnabled(True)

        # Disconnect all active clients gracefully
        for client in self.active_clients:
            if client.is_connected:
                await client.stop_notify(HR_MEASUREMENT_UUID)
                await client.disconnect()
                
        self.active_clients.clear()
        print(f"Session {self.session_id} saved successfully.")
        QMessageBox.information(self, "Saved", f"Data saved to {self.session_id}.csv")

if __name__ == "__main__":
    # Standard PyQt application setup
    app = QApplication(sys.argv)
    
    # QEventLoop is from qasync. It replaces the standard asyncio/PyQt loops
    # so they can run together seamlessly.
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MovesenseApp()
    window.show()

    # Run the merged event loop forever
    with loop:
        loop.run_forever()
