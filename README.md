# Bluetooth ECG Acquisition

Linux program for acquisition of RRs from ECG sensors over Bluetooth

## Step-by-Step Roadmap

Here is how I suggest we tackle this, step-by-step:
Step 1: System & Environment Setup

We need to make sure your Ubuntu machine is ready. We will install Python virtual environments, necessary system Bluetooth libraries (libglib2.0-dev, bluez), and our Python packages (bleak, PyQt6, qasync).
Step 2: The Command-Line Prototype (Core BLE)

Before building a UI, we must ensure we can reliably connect to the sensors and parse the data. We will write a simple Python script using Bleak to:

    Scan for Movesense devices.

    Connect to one sensor.

    Subscribe to the Heart Rate characteristic.

    Extract and print the RR intervals to the terminal.

Step 3: Expanding to Multiple Devices

We will upgrade the script from Step 2 to take an array of MAC addresses and connect to them concurrently using asyncio.gather().
Step 4: Building the User Interface

We will use PyQt6 to draft the visual layout:

    A text input for "Session ID".

    A list/table where you can click "Add Sensor", input its MAC address, and assign a "User ID".

    A giant "Start Recording" / "Stop Recording" button.

Step 5: Integration & Data Logging

We will tie the UI (Step 4) to the BLE logic (Step 3) using qasync. Finally, we will write the callback function that takes the RR interval, grabs the current datetime.now(), matches it to the User ID, and appends it to <session_id>.csv.

## Step 1: System & Environment Setup

First, we need to ensure your Linux system has the correct Bluetooth libraries installed before we create our Python environment.

1. Install System Dependencies
Open your Ubuntu terminal and run the following command to make sure the core Bluetooth stack (BlueZ) is up to date:
Bash

sudo apt-get update
sudo apt-get install bluetooth bluez bluez-tools

(Note: If your Bluetooth is currently off, turn it on via your Ubuntu system settings!)

2. Create a Python Virtual Environment
It's always best practice to keep project dependencies isolated. Let's create a dedicated folder and a virtual environment for this project. In your terminal, run:
Bash

mkdir movesense_ecg_project
cd movesense_ecg_project
python3 -m venv venv
source venv/bin/activate

(You should now see (venv) at the beginning of your terminal prompt.)

3. Install Python Libraries
Now, let's install the libraries we discussed earlier. Run:
Bash

pip install bleak PyQt6 qasync

Your First Script: The Movesense Scanner

Now that the environment is ready, let's write a quick script to find your Movesense sensors. When Movesense sensors are awake (usually by touching the two metal studs on the back), they broadcast their presence over Bluetooth.

How to run it:
Make sure your Movesense sensors are nearby and awake. Then, in your terminal, run:
Bash

python scanner.py

You should see an output listing the names and MAC addresses (which look like AA:BB:CC:DD:EE:FF on Ubuntu) of your sensors. Save those MAC addresses, as we will need them to connect!

## Step 2: The Command-Line Prototype (Core BLE)

To get the RR intervals, we are going to use the standard Bluetooth Heart Rate Measurement characteristic.

When the Movesense sensor sends a Heart Rate packet, it doesn't send nicely formatted text; it sends a stream of raw bytes. We have to look at the very first byte (the "Flags" byte) to know how to read the rest of the packet. Specifically, we check if the 4th bit is flipped to 1, which tells us RR interval data is attached at the end of the packet.

Create a new file named single_sensor.py and paste the following code. Make sure to replace the placeholder MAC address at the bottom with the one you found in Step 1.

## Step 3: Expanding to Multiple Devices

Excellent! Now we are getting to the really powerful part. Connecting to one Bluetooth device is standard, but connecting to multiple simultaneously requires asynchronous programming.

In Python, asyncio allows us to juggle multiple Bluetooth connections at the exact same time without the program freezing or waiting in line.
Step 3: Streaming Multiple Sensors Concurrently

To do this, we are going to modify our code in two main ways:

    A "Closure" for the Handler: The standard Bleak callback function only receives the raw data, it doesn't automatically know which sensor sent it. We will wrap our handler in a function that passes along a "User ID" or "Device Name" so we know who the RR interval belongs to.

    asyncio.gather(): Instead of connecting to one device and waiting, we will fire off multiple connection tasks simultaneously and tell Python to manage them all at once.

Here is the code for multi_sensor.py:

## Step 4: Building the User Interface

## Step 5: Integration & Data Logging

This is where the project really comes to life!

Combining a graphical user interface (GUI) with background Bluetooth tasks is traditionally tricky because the UI needs its own "event loop" to stay responsive to your clicks, while bleak needs an asyncio event loop to listen for Bluetooth data.

To solve this, we use the qasync library, which perfectly merges PyQt6 and asyncio together.
Step 4 & 5: The Complete Application (UI + Logging)

We are going to combine the UI building and the CSV data logging into one final, complete script.

Here is what this application will do:

    Provide a UI to set a Session ID, add MAC Addresses, and assign User IDs.

    Have a Start/Stop button.

    When started, it will connect to all listed sensors concurrently.

    As RR intervals arrive, it will immediately append a row to a CSV file named <Session_ID>.csv with the format: Timestamp, UserID, HR, RR_Interval.

Save the following code as movesense_app.py:
