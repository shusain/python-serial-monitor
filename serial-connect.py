import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
from threading import Thread
from time import sleep
import json
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from collections import defaultdict
import matplotlib.pyplot as plt
from datetime import datetime

ser = None
data_store = defaultdict(lambda: {'times': [], 'values': []})


def open_connection():
    global ser
    port = port_combobox.get()
    baudrate = int(baudrate_combobox.get())
    
    try:
        ser = serial.Serial(port, baudrate)
        status_label.config(text=f"Connected to {port} at {baudrate} baud rate")
        start_read_thread()
    except Exception as e:
        status_label.config(text=f"Failed to connect: {e}")

def start_read_thread():
    read_thread = Thread(target=read_from_port, args=(ser,))
    read_thread.start()

def read_from_port(serial_instance):
    while True:
        if not serial_instance.is_open:
            break
        try:
            sleep(0.1)
            while serial_instance.in_waiting:
                line = serial_instance.readline().decode('utf-8').strip()
                output_text.insert(tk.END, f"Received: {line}\n")
                output_text.yview(tk.END)
                
                if parse_json_var.get():
                    try:
                        data = json.loads(line)
                        current_time = datetime.now()
                        for key, value in data.items():
                            data_store[key]['times'].append(current_time)
                            data_store[key]['values'].append(value)
                        plot_data()
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"Failed to read from the port: {e}")
            break

def plot_data():
    ax.clear()
    for key, data in data_store.items():
        ax.plot(data['times'], data['values'], label=key)
    ax.legend(loc='upper left')
    fig.autofmt_xdate()  # Rotate and format x-axis labels for better readability
    canvas.draw()

def send_message():
    message = input_entry.get()
    if ser and ser.is_open and message:
        try:
            ser.write(message.encode('utf-8'))
            output_text.insert(tk.END, f"Sent: {message}\n")
            output_text.yview(tk.END)
        except Exception as e:
            messagebox.showerror("Serial Communication Error", f"Failed to send message: {e}")



# Get a list of available serial ports and baud rates
available_ports = [port.device for port in serial.tools.list_ports.comports() if port.device]
baud_rates = [300, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 115200]

# Set up the main window
root = tk.Tk()
root.title("Serial Port Connector")

# Create and configure the widgets
port_label = ttk.Label(root, text="Serial Port:")
port_label.grid(column=0, row=0, padx=5, pady=5, sticky=tk.W)

port_combobox = ttk.Combobox(root, values=available_ports)
port_combobox.grid(column=1, row=0, padx=5, pady=5, sticky=tk.W)

baudrate_label = ttk.Label(root, text="Baud Rate:")
baudrate_label.grid(column=0, row=1, padx=5, pady=5, sticky=tk.W)

baudrate_combobox = ttk.Combobox(root, values=baud_rates)
baudrate_combobox.grid(column=1, row=1, padx=5, pady=5, sticky=tk.W)
baudrate_combobox.set(baud_rates[4])  # Set default baud rate to 9600

open_button = ttk.Button(root, text="Open Connection", command=open_connection)
open_button.grid(column=0, row=2, columnspan=2, padx=5, pady=5)

status_label = ttk.Label(root, text="Status: Not connected")
status_label.grid(column=0, row=3, columnspan=2, padx=5, pady=5)

input_label = ttk.Label(root, text="Input:")
input_label.grid(column=0, row=4, padx=5, pady=5, sticky=tk.W)

input_entry = ttk.Entry(root, width=50)
input_entry.grid(column=1, row=4, padx=5, pady=5, sticky=tk.W)

send_button = ttk.Button(root, text="Send Message", command=send_message)
send_button.grid(column=2, row=4, padx=5, pady=5)

output_text = tk.Text(root, wrap='word', width=50, height=10)
output_text.grid(column=0, row=5, columnspan=3, padx=5, pady=5)

parse_json_var = tk.BooleanVar(value=False)
parse_json_cb = ttk.Checkbutton(root, text='Parse serial data as JSON and plot', variable=parse_json_var)
parse_json_cb.grid(column=0, row=6, columnspan=3, padx=5, pady=5)


def rescan_serial_ports():
    port_combobox['values'] = [port.device for port in serial.tools.list_ports.comports()]

rescan_button = ttk.Button(root, text="Rescan Serial Ports", command=rescan_serial_ports)
rescan_button.grid(column=2, row=0, padx=5, pady=5)

rescan_serial_ports()

fig, ax = plt.subplots(figsize=(5, 4))
canvas = FigureCanvasTkAgg(fig, master=root)
canvas_widget = canvas.get_tk_widget()
canvas_widget.grid(column=0, row=7, columnspan=3, padx=5, pady=5)
# Start the Tkinter event loop
root.mainloop()
