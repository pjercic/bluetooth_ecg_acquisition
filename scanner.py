import asyncio
from bleak import BleakScanner

async def scan_for_movesense():
    print("Scanning for Bluetooth devices for 5 seconds...")
    # Discover devices nearby
    devices = await BleakScanner.discover(timeout=5.0)
    
    print("\n--- Scan Results ---")
    movesense_found = False
    
    for d in devices:
        # Movesense sensors usually broadcast a name starting with "Movesense"
        if d.name and "Movesense" in d.name:
            print(f"✅ Found Sensor! Name: {d.name} | MAC Address: {d.address}")
            movesense_found = True
            
    if not movesense_found:
        print("❌ No Movesense sensors found.")
        print("Tip: Make sure they are awake (touch the metal snaps) and NOT currently connected to your phone.")

if __name__ == "__main__":
    # Run the asynchronous scan function
    asyncio.run(scan_for_movesense())
