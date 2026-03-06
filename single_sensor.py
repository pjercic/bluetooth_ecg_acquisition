import asyncio
from bleak import BleakClient

# The standard Bluetooth UUID for Heart Rate Measurement
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

def hr_data_handler(sender, data):
    """
    This function is called every time the sensor sends a new data packet.
    We must decode the raw bytes based on the BLE Heart Rate specification.
    """
    flags = data[0]
    
    # Bit 0 tells us if Heart Rate is 8-bit or 16-bit
    hr_format = flags & 0x01
    
    # Bit 4 tells us if RR intervals are present in this packet
    rr_present = (flags & 0x10) >> 4

    offset = 1
    
    # Extract Heart Rate (just for context)
    if hr_format == 0:
        hr = data[offset]
        offset += 1
    else:
        hr = int.from_bytes(data[offset:offset+2], byteorder='little')
        offset += 2

    # Bit 3 tells us if Energy Expended data is present (we skip it if so)
    if flags & 0x08:
        offset += 2

    # Extract RR Intervals (if present)
    if rr_present:
        # A single packet can contain multiple RR intervals
        while offset < len(data):
            # RR intervals are 16-bit values
            rr_raw = int.from_bytes(data[offset:offset+2], byteorder='little')
            
            # BLE specification: RR is sent in units of 1/1024 seconds. 
            # Convert it to milliseconds for easier reading.
            rr_ms = (rr_raw / 1024.0) * 1000.0
            
            print(f"❤️ HR: {hr} bpm | 📈 RR Interval: {rr_ms:.1f} ms")
            offset += 2

async def connect_and_stream(mac_address):
    print(f"Attempting to connect to {mac_address}...")
    
    # Connect to the sensor
    async with BleakClient(mac_address) as client:
        print(f"✅ Connected to {mac_address}!")
        
        # Subscribe to the Heart Rate characteristic
        print("Subscribing to Heart Rate data... (Press Ctrl+C to stop)")
        await client.start_notify(HR_MEASUREMENT_UUID, hr_data_handler)
        
        # Keep the script running to continuously receive data
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Cleanly stop notifications before disconnecting
            await client.stop_notify(HR_MEASUREMENT_UUID)
            print("\nDisconnected.")

if __name__ == "__main__":
    # ⚠️ REPLACE THIS WITH YOUR MOVESENSE MAC ADDRESS FROM STEP 1 - scanning
    # Example format: "AA:BB:CC:DD:EE:FF"
    TARGET_MAC_ADDRESS = "YOUR:MAC:ADDRESS:HERE" 
    
    try:
        asyncio.run(connect_and_stream(TARGET_MAC_ADDRESS))
    except KeyboardInterrupt:
        print("\nProgram stopped by user.")
