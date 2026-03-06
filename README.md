# bluetooth_ecg_acquisition
Linux program for acquisition of RRs from ECG sensors over Bluetooth
Step 1: System & Environment Setup

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

