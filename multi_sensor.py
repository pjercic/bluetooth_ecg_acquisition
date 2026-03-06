import asyncio
from bleak import BleakClient

HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

def create_hr_handler(user_id):
    """
    This function creates a customized callback for each sensor.
    It "remembers" the user_id so we know who the data belongs to.
    """
    def hr_data_handler(sender, data):
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
            while offset < len(data):
                rr_raw = int.from_bytes(data[offset:offset+2], byteorder='little')
                rr_ms = (rr_raw / 1024.0) * 1000.0
                # We now print the specific User ID with the data!
                print(f"[{user_id}] ❤️ HR: {hr} bpm | 📈 RR: {rr_ms:.1f} ms")
                offset += 2
                
    return hr_data_handler

async def connect_and_stream(mac_address, user_id):
    print(f"[{user_id}] Attempting to connect to {mac_address}...")
    
    try:
        async with BleakClient(mac_address) as client:
            print(f"[{user_id}] ✅ Connected!")
            
            # Use our custom handler factory
            handler = create_hr_handler(user_id)
            await client.start_notify(HR_MEASUREMENT_UUID, handler)
            
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                await client.stop_notify(HR_MEASUREMENT_UUID)
                print(f"[{user_id}] Disconnected cleanly.")
    except Exception as e:
        print(f"[{user_id}] ❌ Connection failed or dropped: {e}")

async def main():
    # ⚠️ REPLACE THESE WITH YOUR ACTUAL MAC ADDRESSES
    # You can add as many as your laptop's Bluetooth hardware supports
    sensors = {
        "YOUR:FIRST:MAC:ADDRESS": "User_A",
        "YOUR:SECOND:MAC:ADDRESS": "User_B"
    }
    
    print("Starting multi-sensor connection sequence...")
    
    # Create a list of concurrent connection tasks
    tasks = []
    for mac, user in sensors.items():
        tasks.append(connect_and_stream(mac, user))
        
    # Run all connection tasks at the exact same time
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram stopped by user.")