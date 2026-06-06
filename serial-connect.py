import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import filedialog
import signal
import sys
import os
import threading
import re
import serial
import serial.tools.list_ports
from threading import Thread
from time import sleep
import json
import svg2gcode
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from collections import defaultdict
import matplotlib.pyplot as plt
from datetime import datetime

ser = None
data_store = defaultdict(lambda: {'times': [], 'values': []})

# G-code runner state
gcode_lines = []
gcode_path = None
gcode_run_thread = None
gcode_stop_event = threading.Event()
ack_event = threading.Event()
send_lock = threading.Lock()
origin_offset = (0.0, 0.0, 0.0)
current_pos = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}
last_sent_command = None
resend_requested = None

# Configuration persistence
config_path = os.path.join(os.path.expanduser('~'), '.serial_connect_config.json')
start_job_gcode_str = ''
stop_job_gcode_str = ''

def load_config():
    global start_job_gcode_str, stop_job_gcode_str
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r') as cf:
                data = json.load(cf)
            start_job_gcode_str = data.get('start_job', '')
            stop_job_gcode_str = data.get('stop_job', '')
            # octo settings (persisted)
            globals()['octo_saved_url'] = data.get('octo_url', '')
            globals()['octo_saved_key'] = data.get('octo_api_key', '')
    except Exception:
        start_job_gcode_str = ''
        stop_job_gcode_str = ''

def save_config():
    try:
        data = {
            'start_job': start_job_text.get('1.0', tk.END).rstrip('\n') if 'start_job_text' in globals() else start_job_gcode_str,
            'stop_job': stop_job_text.get('1.0', tk.END).rstrip('\n') if 'stop_job_text' in globals() else stop_job_gcode_str,
            'octo_url': octo_url_var.get() if 'octo_url_var' in globals() else '',
            'octo_api_key': octo_api_key_var.get() if 'octo_api_key_var' in globals() else ''
        }
        with open(config_path, 'w') as cf:
            json.dump(data, cf)
    except Exception:
        pass


def sort_port_key(port):
    match = re.match(r'^(.*?)(\d+)$', port)
    if match:
        return (match.group(1), int(match.group(2)))
    return (port, 0)


def get_available_ports():
    ports = [port.device for port in serial.tools.list_ports.comports() if port.device]
    return sorted(ports, key=sort_port_key)


def find_first_acm_port(ports):
    acm_ports = [p for p in ports if p.startswith('/dev/ttyACM')]
    return acm_ports[0] if acm_ports else None


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
    read_thread.daemon = True
    read_thread.start()

def read_from_port(serial_instance):
    global resend_requested
    while True:
        if not serial_instance.is_open:
            break
        try:
            sleep(0.1)
            while serial_instance.in_waiting:
                line = serial_instance.readline().decode('utf-8').strip()
                output_text.insert(tk.END, f"Received: {line}\n")
                output_text.yview(tk.END)
                # Try to parse position reports like 'X:0.00 Y:0.00 Z:0.00'
                try:
                    matches = re.findall(r'([XYZxyz]):\s*(-?\d+\.?\d*)', line)
                    if matches:
                        for axis, val in matches:
                            axis = axis.upper()
                            try:
                                current_pos[axis] = float(val)
                            except Exception:
                                pass
                        try:
                            current_x_var.set(f"X: {current_pos['X']:.3f}")
                            current_y_var.set(f"Y: {current_pos['Y']:.3f}")
                            current_z_var.set(f"Z: {current_pos['Z']:.3f}")
                        except Exception:
                            pass
                except Exception:
                    pass

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

                # Handle Marlin acknowledgement responses
                resp = line.strip().lower()
                if resp.startswith('ok'):
                    ack_event.set()
                elif 'resend' in resp:
                    match = re.search(r'resend[: ]\s*(\d+)', resp)
                    if match:
                        try:
                            resend_requested = int(match.group(1))
                        except ValueError:
                            resend_requested = None
                    ack_event.set()
                elif 'busy' in resp:
                    output_text.insert(tk.END, "Received busy response, waiting for ok...\n")
                    output_text.yview(tk.END)
                elif resp.startswith('error'):
                    ack_event.set()
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

def send_serial_command(command, wait_for_ok=False, timeout=5.0):
    global last_sent_command
    if not ser or not ser.is_open:
        messagebox.showwarning("Serial Not Connected", "Open a serial connection before sending commands.")
        return False

    payload = command
    if not payload.endswith('\n') and not payload.endswith('\r'):
        payload += '\r\n'

    with send_lock:
        try:
            last_sent_command = command
            ack_event.clear()
            ser.write(payload.encode('utf-8'))
            output_text.insert(tk.END, f"Sent: {command}\n")
            output_text.yview(tk.END)
        except Exception as e:
            messagebox.showerror("Serial Communication Error", f"Failed to send command: {e}")
            return False

        if wait_for_ok:
            if not ack_event.wait(timeout):
                output_text.insert(tk.END, f"Warning: no ok response for '{command}' within {timeout}s\n")
                output_text.yview(tk.END)
                return False
    return True


def send_gcode(command):
    return send_serial_command(command, wait_for_ok=False)


def apply_preview():
    global gcode_lines, gcode_path
    try:
        txt = preview_text.get('1.0', tk.END).rstrip('\n')
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        if not lines:
            messagebox.showinfo('Preview Empty', 'Preview has no G-code to apply.')
            return
        gcode_lines = lines
        gcode_path = None
        status_label.config(text='Applied preview to in-memory G-code')
    except Exception as e:
        messagebox.showerror('Error', f'Failed to apply preview: {e}')


def _fill_leveling_bounds(is_min):
    try:
        x = float(current_pos.get('X', 0.0))
        y = float(current_pos.get('Y', 0.0))
        if is_min:
            leveling_min_x_var.set(x)
            leveling_min_y_var.set(y)
            status_label.config(text='Filled leveling min bounds from current position')
        else:
            leveling_max_x_var.set(x)
            leveling_max_y_var.set(y)
            status_label.config(text='Filled leveling max bounds from current position')
    except Exception as e:
        messagebox.showerror('Error', f'Failed to fill bounds: {e}')


def _generate_leveling_gcode():
    try:
        min_x = float(leveling_min_x_var.get())
        max_x = float(leveling_max_x_var.get())
        min_y = float(leveling_min_y_var.get())
        max_y = float(leveling_max_y_var.get())
        tool_dia = float(leveling_tool_dia_var.get())
        overlap = float(leveling_overlap_var.get())
        start_z = float(leveling_start_z_var.get())
        final_z = float(leveling_final_z_var.get())
        z_step = float(leveling_z_step_var.get())
        safe_z = float(leveling_safe_z_var.get())
        plunge_feed = float(leveling_plunge_feed_var.get())
        feed = float(leveling_feed_var.get())
    except Exception:
        raise ValueError('Invalid leveling input values.')

    if max_x <= min_x or max_y <= min_y:
        raise ValueError('Max values must be larger than min values.')
    if tool_dia <= 0:
        raise ValueError('Tool diameter must be greater than zero.')
    if not (0.0 <= overlap < 1.0):
        raise ValueError('Overlap must be between 0.0 and 1.0 (exclusive).')
    if final_z >= start_z:
        raise ValueError('Final Z must be below start Z.')
    if z_step <= 0:
        raise ValueError('Z step must be greater than zero.')

    radius = tool_dia / 2.0
    step = tool_dia * (1.0 - overlap)
    if step <= 0.0:
        raise ValueError('Overlap too high for tool diameter.')

    left = min_x + radius
    right = max_x - radius
    bottom = min_y + radius
    top = max_y - radius
    if left > right or bottom > top:
        raise ValueError('Tool diameter too large for the specified work area.')

    lines = []
    lines.append('; Generated leveling G-code')
    lines.append('G21 ; units mm')
    lines.append('G90 ; absolute positioning')
    lines.append(f'G0 Z{safe_z:.3f} F{plunge_feed}')

    current_depth = start_z
    while current_depth > final_z:
        next_depth = max(final_z, current_depth - z_step)
        x = left
        y = bottom
        lines.append(f'G0 X{x:.3f} Y{y:.3f} F{feed}')
        lines.append(f'G1 Z{next_depth:.3f} F{plunge_feed}')

        l = left
        r = right
        b = bottom
        t = top
        while l <= r and b <= t:
            lines.append(f'G1 X{r:.3f} Y{b:.3f} F{feed}')
            b += step
            if b > t:
                break
            lines.append(f'G1 X{r:.3f} Y{t:.3f} F{feed}')
            r -= step
            if l > r:
                break
            lines.append(f'G1 X{l:.3f} Y{t:.3f} F{feed}')
            t -= step
            if b > t:
                break
            lines.append(f'G1 X{l:.3f} Y{b:.3f} F{feed}')
            l += step

        lines.append(f'G0 Z{safe_z:.3f} F{plunge_feed}')
        current_depth = next_depth

    lines.append(f'G0 Z{safe_z:.3f} F{plunge_feed}')
    lines.append('G0 X0 Y0 F3000 ; return to home')
    return lines


def generate_leveling_gcode_action():
    try:
        lines = _generate_leveling_gcode()
        if 'leveling_preview_text' in globals():
            leveling_preview_text.delete('1.0', tk.END)
            leveling_preview_text.insert('1.0', '\n'.join(lines))
        global gcode_lines, gcode_path
        gcode_lines = lines
        gcode_path = None
        status_label.config(text='Generated leveling G-code')
    except Exception as e:
        messagebox.showerror('Leveling Error', f'Failed to generate leveling G-code: {e}')


def parse_drl_file(path):
    """Parse a simple KiCad DRL file export and return dict with units, tools, and coordinates.
    Returns: {'units': 'mm'|'in', 'tools': {id: dia}, 'hits': [(x,y,tool_id), ...]}
    """
    tools = {}
    hits = []
    units = 'mm'
    current_tool = None
    with open(path, 'r') as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith(';'):
                if ln.startswith(';') and 'METRIC' in ln.upper():
                    units = 'mm'
                continue
            if ln.upper().startswith('METRIC'):
                units = 'mm'
                continue
            if ln.upper().startswith('INCH') or 'IMPERIAL' in ln.upper():
                units = 'in'
                continue
            # tool definition like T1C0.800
            m = re.match(r'^T(\d+)C([0-9.]+)$', ln, re.IGNORECASE)
            if m:
                tid = int(m.group(1))
                dia = float(m.group(2))
                tools[tid] = dia
                continue
            # tool select like T1
            m = re.match(r'^T(\d+)$', ln, re.IGNORECASE)
            if m:
                current_tool = int(m.group(1))
                continue
            # coords like X30.605Y18.88
            m = re.match(r'^X\s*([-0-9.]+)Y\s*([-0-9.]+)$', ln, re.IGNORECASE)
            if m:
                x = float(m.group(1)); y = float(m.group(2))
                hits.append((x, y, current_tool))
                continue
            # alternate format with spaces
            m = re.match(r'^X\s*([-0-9.]+)\s*Y\s*([-0-9.]+)', ln, re.IGNORECASE)
            if m:
                x = float(m.group(1)); y = float(m.group(2))
                hits.append((x, y, current_tool))
                continue
    return {'units': units, 'tools': tools, 'hits': hits}


def generate_drill_gcode(parsed, safe_z=5.0, depth=-1.5, plunge_f=300.0, travel_f=3000.0, dwell=0.1):
    return generate_drill_gcode(parsed, safe_z=safe_z, depth=depth, plunge_f=plunge_f, travel_f=travel_f, dwell=dwell, group_by_tool=False, tool_pause=False, tool_pause_mcode='M0', tool_change_x=None, tool_change_y=None, tool_change_z=None, peck_enabled=False, peck_step=1.0)


def generate_drill_gcode(parsed, safe_z=5.0, depth=-1.5, plunge_f=300.0, travel_f=3000.0, dwell=0.1, group_by_tool=False, tool_pause=False, tool_pause_mcode='M0', tool_change_x=None, tool_change_y=None, tool_change_z=None, peck_enabled=False, peck_step=1.0):
    """Generate drill G-code. Supports grouping by tool and optional peck drilling.
    - parsed: dict from parse_drl_file
    - safe_z: safe travel height (mm, positive)
    - depth: final drill depth (mm, negative)
    - plunge_f: plunge feed
    - travel_f: travel feed
    - dwell: dwell seconds at bottom
    - group_by_tool: if True, emit tool-change pauses between tool groups
    - tool_pause_mcode: M-code to pause for tool change (default M0)
    - peck_enabled: if True, perform pecking (full retract to safe_z between pecks)
    - peck_step: peck depth per pass (mm, positive)
    """
    lines = []
    lines.append('; Generated from DRL')
    lines.append('G21 ; units mm')
    lines.append('G90 ; absolute')
    lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')

    hits = parsed.get('hits', [])
    if group_by_tool:
        # group hits by tool id (None preserved)
        groups = {}
        for x, y, t in hits:
            groups.setdefault(t, []).append((x, y))
        # iterate tools in sorted order (None last)
        tool_ids = sorted([k for k in groups.keys() if k is not None])
        if None in groups:
            tool_ids.append(None)

        for tid in tool_ids:
            tools_for_tid = groups.get(tid, [])
            dia = parsed.get('tools', {}).get(tid, None)
            lines.append(f'; Tool {tid} diameter={dia if dia is not None else "?"}')
            if tool_pause:
                if tool_change_x is not None and tool_change_y is not None and tool_change_z is not None:
                    lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')
                    lines.append(f'G0 X{tool_change_x:.3f} Y{tool_change_y:.3f} F{travel_f}')
                    lines.append(f'G0 Z{tool_change_z:.3f} F{travel_f}')
                    lines.append(f'; Move to tool change position before pause')
                lines.append(f'; PAUSE FOR TOOL {tid} CHANGE')
                lines.append(tool_pause_mcode)

            for (x, y) in tools_for_tid:
                lines.append(f'G0 X{x:.3f} Y{y:.3f} F{travel_f}')
                if peck_enabled and peck_step > 0:
                    # perform pecking from surface (Z=0) down to depth, retract to 1mm above Z0 between pecks
                    retract_z = 1.0
                    cur = 0.0
                    while True:
                        next_z = max(depth, cur - abs(peck_step))
                        lines.append(f'G1 Z{next_z:.3f} F{plunge_f}')
                        if dwell and dwell > 0:
                            lines.append(f'G4 P{dwell:.3f}')
                        lines.append(f'G0 Z{retract_z:.3f} F{travel_f}')
                        if next_z == depth:
                            break
                        cur = next_z
                else:
                    lines.append(f'G1 Z{depth:.3f} F{plunge_f}')
                    if dwell and dwell > 0:
                        lines.append(f'G4 P{dwell:.3f}')
                    lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')

    else:
        # simple serial order
        for (x, y, tool) in hits:
            lines.append(f'G0 X{x:.3f} Y{y:.3f} F{travel_f}')
            if peck_enabled and peck_step > 0:
                cur = 0.0
                while True:
                    next_z = max(depth, cur - abs(peck_step))
                    lines.append(f'G1 Z{next_z:.3f} F{plunge_f}')
                    if dwell and dwell > 0:
                        lines.append(f'G4 P{dwell:.3f}')
                    lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')
                    if next_z == depth:
                        break
                    cur = next_z
            else:
                lines.append(f'G1 Z{depth:.3f} F{plunge_f}')
                if dwell and dwell > 0:
                    lines.append(f'G4 P{dwell:.3f}')
                lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')

    lines.append('G0 X0 Y0 F{:.0f} ; return to origin'.format(travel_f))
    return lines


def get_selected_drill_hits(parsed):
    """Return parsed drill hits filtered by the selected tool checkboxes."""
    if not drill_tool_include_vars:
        return parsed.get('hits', [])
    selected_tool_ids = [tid for tid, var in drill_tool_include_vars.items() if var.get()]
    return [hit for hit in parsed.get('hits', []) if hit[2] in selected_tool_ids]


def load_drl_file():
    path = filedialog.askopenfilename(title='Select DRL file', filetypes=[('Drill', ('*.drl', '*.drill', '*.drl.txt')), ('All files', '*.*')])
    if not path:
        return
    try:
        parsed = parse_drl_file(path)
        # remember last parsed DRL for quick regeneration
        globals()['last_parsed_drl'] = parsed
        globals()['last_drl_path'] = path
        # convert units if inches
        if parsed.get('units') == 'in':
            # convert inches to mm
            parsed['hits'] = [(x*25.4, y*25.4, t) for (x, y, t) in parsed['hits']]
        # update tool selection checkboxes
        try:
            counts = {}
            for (_, _, t) in parsed.get('hits', []):
                counts[t] = counts.get(t, 0) + 1
            drill_tool_include_vars.clear()
            for child in drill_tools_container.winfo_children():
                child.destroy()
            if counts:
                for row, (tid, cnt) in enumerate(sorted(counts.items(), key=lambda x: (x[0] is None, x[0]))):
                    dia = parsed.get('tools', {}).get(tid, '?')
                    tool_text = f'Tool {tid if tid is not None else "?"}: dia={dia} mm — {cnt} holes'
                    var = tk.BooleanVar(value=True)
                    drill_tool_include_vars[tid] = var
                    cb = ttk.Checkbutton(drill_tools_container, text=tool_text, variable=var, command=regenerate_drill_gcode)
                    cb.grid(column=0, row=row, sticky='w', padx=2, pady=1)
            else:
                ttk.Label(drill_tools_container, text='No tools detected').grid(column=0, row=0, sticky='w')
        except Exception:
            pass
        # populate drill preview based on selected tools
        filtered_parsed = {
            'units': parsed.get('units'),
            'tools': parsed.get('tools', {}),
            'hits': get_selected_drill_hits(parsed)
        }
        lines = generate_drill_gcode(
            filtered_parsed,
            safe_z=float(drill_safe_z_var.get()),
            depth=float(drill_depth_var.get()),
            plunge_f=float(drill_plunge_feed_var.get()),
            travel_f=float(drill_travel_feed_var.get()),
            dwell=float(drill_dwell_var.get()),
            group_by_tool=bool(drill_group_by_tool_var.get()),
            tool_pause=bool(drill_tool_pause_var.get()),
            tool_pause_mcode=str(drill_tool_pause_mcode_var.get()),
            tool_change_x=float(drill_tool_change_x_var.get()),
            tool_change_y=float(drill_tool_change_y_var.get()),
            tool_change_z=float(drill_tool_change_z_var.get()),
            peck_enabled=bool(drill_peck_enable_var.get()),
            peck_step=float(drill_peck_step_var.get())
        )
        if 'drill_preview_text' in globals():
            drill_preview_text.delete('1.0', tk.END)
            drill_preview_text.insert('1.0', '\n'.join(lines))
        global gcode_lines, gcode_path
        gcode_lines = lines
        gcode_path = path
        status_label.config(text=f'Loaded DRL and generated G-code: {os.path.basename(path)}')
    except Exception as e:
        messagebox.showerror('DRL Load Failed', f'Failed to load DRL: {e}')


def load_drill_to_buffer():
    # copy preview into running buffer
    try:
        txt = drill_preview_text.get('1.0', tk.END).rstrip('\n')
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        if not lines:
            messagebox.showinfo('Empty', 'Drill preview empty')
            return
        global gcode_lines, gcode_path
        gcode_lines = lines
        gcode_path = None
        status_label.config(text='Loaded drill G-code into buffer')
    except Exception as e:
        messagebox.showerror('Error', f'Failed to load buffer: {e}')


def regenerate_drill_gcode():
    """Regenerate G-code from last parsed DRL using current UI settings."""
    if 'last_parsed_drl' not in globals() or not globals().get('last_parsed_drl'):
        messagebox.showinfo('No DRL Loaded', 'No DRL has been loaded yet to regenerate from.')
        return
    parsed = globals().get('last_parsed_drl')
    try:
        filtered_parsed = {
            'units': parsed.get('units'),
            'tools': parsed.get('tools', {}),
            'hits': get_selected_drill_hits(parsed)
        }
        lines = generate_drill_gcode(
            filtered_parsed,
            safe_z=float(drill_safe_z_var.get()),
            depth=float(drill_depth_var.get()),
            plunge_f=float(drill_plunge_feed_var.get()),
            travel_f=float(drill_travel_feed_var.get()),
            dwell=float(drill_dwell_var.get()),
            group_by_tool=bool(drill_group_by_tool_var.get()),
            tool_pause=bool(drill_tool_pause_var.get()),
            tool_pause_mcode=str(drill_tool_pause_mcode_var.get()),
            tool_change_x=float(drill_tool_change_x_var.get()),
            tool_change_y=float(drill_tool_change_y_var.get()),
            tool_change_z=float(drill_tool_change_z_var.get()),
            peck_enabled=bool(drill_peck_enable_var.get()),
            peck_step=float(drill_peck_step_var.get())
        )
        drill_preview_text.delete('1.0', tk.END)
        drill_preview_text.insert('1.0', '\n'.join(lines))
        global gcode_lines, gcode_path
        gcode_lines = lines
        gcode_path = None
        status_label.config(text='Regenerated drill G-code from last DRL')
    except Exception as e:
        messagebox.showerror('Regenerate Failed', f'Failed to regenerate: {e}')


def upload_to_octoprint(select_only=False):
    try:
        import requests
    except Exception:
        messagebox.showerror('Missing Dependency', 'The requests package is required for OctoPrint integration. Install with `pip install requests`.')
        return
    if not octo_url_var.get():
        messagebox.showerror('No URL', 'Enter OctoPrint URL in the OctoPrint section.')
        return
    api_key = octo_api_key_var.get()
    base = octo_url_var.get().rstrip('/')
    if not gcode_lines:
        messagebox.showinfo('No G-code', 'No G-code loaded to upload.')
        return
    gcode_text = '\n'.join(gcode_lines) + '\n'
    url = f"{base}/api/files/local"
    headers = {'X-Api-Key': api_key} if api_key else {}
    files = {'file': ('drill.gcode', gcode_text.encode('utf-8'))}
    data = {'select': 'true'}
    if not select_only:
        data['print'] = 'true'
    try:
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        if resp.status_code in (200, 201):
            messagebox.showinfo('Uploaded', 'G-code uploaded to OctoPrint successfully.')
        else:
            messagebox.showerror('Upload Failed', f'OctoPrint upload failed: {resp.status_code} {resp.text}')
    except Exception as e:
        messagebox.showerror('Upload Error', f'Failed to upload to OctoPrint: {e}')


def upload_and_print_to_octoprint():
    # Save octo settings then upload
    save_config()
    upload_to_octoprint(select_only=False)


def send_command_via_octoprint(command):
    try:
        import requests
    except Exception:
        messagebox.showerror('Missing Dependency', 'The requests package is required for OctoPrint integration. Install with `pip install requests`.')
        return False
    if not octo_url_var.get():
        messagebox.showerror('No URL', 'Enter OctoPrint URL in the OctoPrint section.')
        return False
    api_key = octo_api_key_var.get()
    base = octo_url_var.get().rstrip('/')
    url = f"{base}/api/printer/command"
    headers = {'X-Api-Key': api_key} if api_key else {}
    try:
        resp = requests.post(url, headers=headers, json={'command': command}, timeout=30)
        if resp.status_code in (204, 200):
            return True
        messagebox.showerror('OctoPrint Error', f'Command failed: {resp.status_code} {resp.text}')
        return False
    except Exception as e:
        messagebox.showerror('OctoPrint Error', f'Failed to send command: {e}')
        return False


def send_g92_z_command():
    try:
        z = float(drill_g92_z_var.get())
    except Exception:
        messagebox.showerror('Invalid Z', 'Enter a valid numeric Z value for G92.')
        return
    command = f'G92 Z{z:.3f}'
    if octo_url_var.get():
        if send_command_via_octoprint(command):
            messagebox.showinfo('G92 Sent', f'Sent: {command} to OctoPrint.')
    else:
        if send_gcode(command):
            messagebox.showinfo('G92 Sent', f'Sent: {command}')


def send_message(event=None):
    message = input_entry.get()
    if message:
        sent = send_gcode(message)
        if sent:
            input_entry.delete(0, tk.END)
            input_entry.focus_set()


def send_estop():
    send_gcode('M112')


def home_axis(axis):
    send_gcode(f'G28 {axis}')


def jog_axis(direction):
    try:
        amount = float(jog_amount_var.get())
    except ValueError:
        messagebox.showerror("Invalid Amount", "Enter a numeric jog amount.")
        return

    axis = jog_axis_var.get()
    delta = amount if direction == 'positive' else -amount
    send_gcode('G91')
    send_gcode(f'G0 {axis}{delta} F3000')
    send_gcode('G90')


def load_gcode_path(path):
    global gcode_lines, gcode_path
    if not path:
        return
    try:
        with open(path, 'r') as f:
            lines = [ln.rstrip('\n') for ln in f if ln.strip()]
        gcode_lines = lines
        gcode_path = path
        status_label.config(text=f"Loaded G-code: {os.path.basename(path)}")
        # populate preview editor if present
        try:
            if 'preview_text' in globals():
                preview_text.delete('1.0', tk.END)
                preview_text.insert('1.0', '\n'.join(gcode_lines))
        except Exception:
            pass
    except Exception as e:
        messagebox.showerror('Error', f'Failed to load file: {e}')


def load_gcode():
    path = filedialog.askopenfilename(title='Select G-code file', filetypes=[('G-code', ('*.gcode', '*.nc', '*.txt')), ('All files','*.*')])
    if not path:
        return
    load_gcode_path(path)


def process_svg_to_gcode():
    global gcode_lines, gcode_path
    svg_path = filedialog.askopenfilename(title='Select SVG file', filetypes=[('SVG','*.svg'), ('All files','*.*')])
    if not svg_path:
        return

    try:
        simplify = float(svg_simplify_var.get())
    except Exception:
        simplify = 0.2

    mode = svg_mode_var.get()
    try:
        page = svg_page_var.get() or '8.5x11'
        units = svg_units_var.get() or 'in'
        
        if mode == 'pen_plot':
            pen_up = float(svg_pen_up_var.get())
            pen_down = float(svg_pen_down_var.get())
            tool_width = 0.0
            cutting_mode = False
        else:  # cutting
            pen_up = 5.0
            pen_down = 0.0
            tool_width = float(svg_tool_width_var.get())
            cutting_mode = True
    except Exception:
        page = '8.5x11'
        units = 'in'
        if mode == 'pen_plot':
            pen_up = 5.0
            pen_down = 0.0
            tool_width = 0.0
            cutting_mode = False
        else:
            pen_up = 5.0
            pen_down = 0.0
            tool_width = 0.0
            cutting_mode = True

    try:
        result_lines = svg2gcode.convert_svg_to_gcode(
            svg_path,
            output=None,
            page=page,
            units=units,
            pen_up=pen_up,
            pen_down=pen_down,
            arc_circles=svg_arc_circles_var.get(),
            arc_paths=svg_arc_paths_var.get(),
            no_scale=False,
            no_offset=False,
            simplify=simplify,
            write_output=False,
            tool_width=tool_width,
            cutting_mode=cutting_mode,
        )
        if not result_lines:
            raise ValueError('SVG conversion produced no G-code lines.')
        gcode_lines = result_lines
        gcode_path = svg_path
        # show generated g-code in preview editor
        try:
            if 'preview_text' in globals():
                preview_text.delete('1.0', tk.END)
                preview_text.insert('1.0', '\n'.join(gcode_lines))
        except Exception:
            pass
        status_label.config(text=f'Converted SVG into memory: {os.path.basename(svg_path)}')
    except Exception as e:
        messagebox.showerror('SVG Conversion Failed', f'Failed to convert SVG: {e}')


def generate_cutout_gcode(origin_x, origin_y, width, length, tool_diameter, depth=-3.0, step_down=1.0, feed=800.0, plunge_feed=300.0, safe_z=5.0, bridge_enabled=False, bridge_count=1, bridge_length=2.0, grid_enabled=False, grid_cols=1, grid_rows=1):
    """Generate a rectangular cut-out path with optional grid slicing, layered depth, and retaining bridges.
    
    When grid is enabled, the inner workpiece is divided into grid_cols x grid_rows cells.
    Tool radius is accounted for in the outer border and grid calculations.
    """
    radius = tool_diameter / 2.0
    left = origin_x - radius
    right = origin_x + width + radius
    bottom = origin_y - radius
    top = origin_y + length + radius
    travel_f = 3000.0

    # Inner dimensions (workpiece size after accounting for tool radius)
    inner_left = origin_x
    inner_right = origin_x + width
    inner_bottom = origin_y
    inner_top = origin_y + length
    inner_width = width
    inner_length = length

    # Calculate grid
    grid_lines_x = []
    grid_lines_y = []
    if grid_enabled and grid_cols > 1:
        col_width = inner_width / grid_cols
        for col_idx in range(1, grid_cols):
            x = inner_left + col_idx * col_width
            grid_lines_x.append(x)
    if grid_enabled and grid_rows > 1:
        row_height = inner_length / grid_rows
        for row_idx in range(1, grid_rows):
            y = inner_bottom + row_idx * row_height
            grid_lines_y.append(y)

    total_depth = abs(depth)
    if step_down <= 0:
        step_down = total_depth
    step_down = min(step_down, total_depth)
    passes = int((total_depth + step_down - 1e-6) // step_down)
    if passes < 1:
        passes = 1

    lines = []
    lines.append('; Generated Cut Out Path with Grid')
    lines.append('G21 ; units mm')
    lines.append('G90 ; absolute')
    lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')

    def append_cut(x, y, feed_rate):
        lines.append(f'G1 X{x:.3f} Y{y:.3f} F{feed_rate}')

    def append_loop(current_depth, final_pass):
        # Grid cuts first
        if grid_enabled:
            # Vertical grid lines
            for x in grid_lines_x:
                lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')
                lines.append(f'G0 X{x:.3f} Y{inner_bottom:.3f} F{travel_f}')
                lines.append(f'G1 Z{current_depth:.3f} F{plunge_feed}')
                append_cut(x, inner_top, feed)
            # Horizontal grid lines
            for y in grid_lines_y:
                lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')
                lines.append(f'G0 X{inner_left:.3f} Y{y:.3f} F{travel_f}')
                lines.append(f'G1 Z{current_depth:.3f} F{plunge_feed}')
                append_cut(inner_right, y, feed)
        
        # Outer border last
        lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')
        lines.append(f'G0 X{left:.3f} Y{bottom:.3f} F{travel_f}')
        lines.append(f'G1 Z{current_depth:.3f} F{plunge_feed}')
        append_cut(right, bottom, feed)
        append_cut(right, top, feed)
        if bridge_enabled and bridge_count > 0 and bridge_length > 0 and (right - left) > bridge_length * bridge_count:
            total_gap = bridge_length * bridge_count
            cut_segment = ((right - left) - total_gap) / (bridge_count + 1)
            x = right
            for bridge_index in range(bridge_count):
                x -= cut_segment
                append_cut(x, top, feed)
                lines.append(f'G1 Z{safe_z:.3f} F{travel_f}')
                x -= bridge_length
                lines.append(f'G1 X{x:.3f} Y{top:.3f} F{travel_f}')
                lines.append(f'G1 Z{current_depth:.3f} F{plunge_feed}')
            append_cut(left, top, feed)
        else:
            append_cut(left, top, feed)
        append_cut(left, bottom, feed)
        append_cut(right, bottom, feed)
        
        lines.append(f'G0 Z{safe_z:.3f} F{travel_f}')

    for pass_index in range(1, passes + 1):
        current_depth = -min(pass_index * step_down, total_depth)
        final_pass = (pass_index == passes)
        lines.append(f'; Pass {pass_index}/{passes} to Z{current_depth:.3f}')
        append_loop(current_depth, final_pass)

    return lines


def make_cutout_from_ui():
    global gcode_lines, gcode_path
    try:
        origin_x = float(cutout_origin_x_var.get())
        origin_y = float(cutout_origin_y_var.get())
        width = float(cutout_width_var.get())
        length = float(cutout_length_var.get())
        tool_diameter = float(cutout_tool_diameter_var.get())
        depth = float(cutout_depth_var.get())
        step_down = float(cutout_step_down_var.get())
        feed = float(cutout_feed_var.get())
        plunge_feed = float(cutout_plunge_feed_var.get())
        safe_z = float(cutout_safe_z_var.get())
        bridge_enabled = bool(cutout_bridge_enabled_var.get())
        bridge_count = int(cutout_bridge_count_var.get())
        bridge_length = float(cutout_bridge_length_var.get())
        grid_enabled = bool(cutout_grid_enabled_var.get())
        grid_cols = int(cutout_grid_cols_var.get())
        grid_rows = int(cutout_grid_rows_var.get())
    except Exception as e:
        messagebox.showerror('Invalid Cutout Settings', f'Check cutout values: {e}')
        return

    try:
        result_lines = generate_cutout_gcode(
            origin_x,
            origin_y,
            width,
            length,
            tool_diameter,
            depth=depth,
            step_down=step_down,
            feed=feed,
            plunge_feed=plunge_feed,
            safe_z=safe_z,
            bridge_enabled=bridge_enabled,
            bridge_count=bridge_count,
            bridge_length=bridge_length,
            grid_enabled=grid_enabled,
            grid_cols=grid_cols,
            grid_rows=grid_rows,
        )
        gcode_lines = result_lines
        gcode_path = None
        try:
            if 'preview_text' in globals():
                preview_text.delete('1.0', tk.END)
                preview_text.insert('1.0', '\n'.join(gcode_lines))
        except Exception:
            pass
        status_label.config(text='Generated cutout G-code in memory')
    except Exception as e:
        messagebox.showerror('Cutout Generation Failed', f'Failed to generate cutout G-code: {e}')


def _gcode_runner():
    global gcode_lines, gcode_path, resend_requested
    if not ser or not getattr(ser, 'is_open', False):
        messagebox.showwarning('Serial Not Connected', 'Open a serial connection before running G-code.')
        return
    status_label.config(text=f'Running: {os.path.basename(gcode_path) if gcode_path else "G-code"}')

    # send start-job g-code if present
    try:
        start_lines = []
        if 'start_job_text' in globals():
            start_lines = [ln for ln in start_job_text.get('1.0', tk.END).splitlines() if ln.strip()]
        else:
            start_lines = [ln for ln in start_job_gcode_str.splitlines() if ln.strip()]
        for ln in start_lines:
            pl = apply_origin_to_line(ln.strip(), origin_offset)
            send_serial_command(pl, wait_for_ok=True, timeout=30.0)
    except Exception:
        pass
    i = 0
    while i < len(gcode_lines):
        if gcode_stop_event.is_set():
            break
        line = gcode_lines[i]
        # skip comments
        s = line.strip()
        if not s or s.startswith(';') or s.startswith('('):
            i += 1
            continue
        # Apply origin offset to X/Y/Z coordinates before sending
        try:
            payload_line = apply_origin_to_line(s, origin_offset)
        except Exception:
            payload_line = s
        success = send_serial_command(payload_line, wait_for_ok=True, timeout=30.0)
        if not success:
            output_text.insert(tk.END, f"Stopping stream after failed acknowledgement for: {s}\n")
            output_text.yview(tk.END)
            break
        if resend_requested is not None:
            output_text.insert(tk.END, f"Resend requested for line {resend_requested}, retrying...\n")
            output_text.yview(tk.END)
            resend_requested = None
            continue
        i += 1
        sleep(0.01)
    status_label.config(text='G-code run finished' if not gcode_stop_event.is_set() else 'G-code run stopped')
    # send stop-job g-code if present
    try:
        stop_lines = []
        if 'stop_job_text' in globals():
            stop_lines = [ln for ln in stop_job_text.get('1.0', tk.END).splitlines() if ln.strip()]
        else:
            stop_lines = [ln for ln in stop_job_gcode_str.splitlines() if ln.strip()]
        for ln in stop_lines:
            pl = apply_origin_to_line(ln.strip(), origin_offset)
            send_serial_command(pl, wait_for_ok=True, timeout=30.0)
    except Exception:
        pass
    gcode_stop_event.clear()


def run_gcode():
    global gcode_run_thread
    if not gcode_lines:
        messagebox.showinfo('No G-code', 'Load a G-code file first.')
        return
    if gcode_run_thread and gcode_run_thread.is_alive():
        messagebox.showinfo('Running', 'G-code is already running.')
        return
    gcode_stop_event.clear()
    gcode_run_thread = threading.Thread(target=_gcode_runner, daemon=True)
    gcode_run_thread.start()


def apply_origin_to_line(line, origin):
    # Replace X/Y/Z numeric values by subtracting origin offsets
    ox, oy, oz = origin
    def _repl(m):
        axis = m.group(1).upper()
        try:
            val = float(m.group(2))
        except Exception:
            return m.group(0)
        if axis == 'X':
            val -= ox
        elif axis == 'Y':
            val -= oy
        elif axis == 'Z':
            val -= oz
        return f"{axis}{val:.3f}"
    try:
        return re.sub(r'([XYZxyz])\s*(-?\d+\.?\d*)', _repl, line)
    except Exception:
        return line


def stop_gcode():
    gcode_stop_event.set()


def set_origin():
    global origin_offset
    try:
        ox = float(current_pos.get('X', 0.0))
        oy = float(current_pos.get('Y', 0.0))
        oz = float(current_pos.get('Z', 0.0))
        origin_offset = (ox, oy, oz)
        origin_var.set(f"Origin: X{ox:.3f} Y{oy:.3f} Z{oz:.3f}")
        status_label.config(text=f"Origin set to X{ox:.3f} Y{oy:.3f} Z{oz:.3f}")
    except Exception as e:
        messagebox.showerror('Error', f'Failed to set origin: {e}')


def clear_origin():
    global origin_offset
    origin_offset = (0.0, 0.0, 0.0)
    origin_var.set('Origin: X0.000 Y0.000 Z0.000')
    status_label.config(text='Origin cleared')


# Get a list of available serial ports and baud rates
available_ports = get_available_ports()
default_port = find_first_acm_port(available_ports) or (available_ports[0] if available_ports else '')
baud_rates = [300, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 115200]

# Set up the main window
root = tk.Tk()
root.title("Serial Port Connector")

# Basic modern styling
bg_color = '#f5f6f8'
root.configure(bg=bg_color)
style = ttk.Style()
try:
    style.theme_use('clam')
except Exception:
    pass
default_font = ('Helvetica', 10)
style.configure('.', font=default_font)
style.configure('TLabelframe', background=bg_color)
style.configure('TLabelframe.Label', background=bg_color, font=('Helvetica', 10, 'bold'))
style.configure('TLabel', background=bg_color)
style.configure('TButton', padding=6)


# Create and configure the widgets
port_label = ttk.Label(root, text="Serial Port:")
port_label.grid(column=0, row=0, padx=5, pady=5, sticky=tk.W)

port_combobox = ttk.Combobox(root, values=available_ports)
port_combobox.grid(column=1, row=0, padx=5, pady=5, sticky=tk.W)
if default_port:
    port_combobox.set(default_port)

baudrate_label = ttk.Label(root, text="Baud Rate:")
baudrate_label.grid(column=0, row=1, padx=5, pady=5, sticky=tk.W)

baudrate_combobox = ttk.Combobox(root, values=baud_rates)
baudrate_combobox.grid(column=1, row=1, padx=5, pady=5, sticky=tk.W)
baudrate_combobox.set(baud_rates[9])  # Set default baud rate to 115200

open_button = ttk.Button(root, text="Open Connection", command=open_connection)
open_button.grid(column=0, row=2, columnspan=2, padx=5, pady=5)

status_label = ttk.Label(root, text="Status: Not connected")
status_label.grid(column=0, row=3, columnspan=2, padx=5, pady=5)

# Current position and origin display
pos_frame = ttk.Frame(root)
pos_frame.grid(column=2, row=3, padx=5, pady=5, sticky='e')

current_x_var = tk.StringVar(value='X: 0.000')
current_y_var = tk.StringVar(value='Y: 0.000')
current_z_var = tk.StringVar(value='Z: 0.000')
origin_var = tk.StringVar(value='Origin: X0.000 Y0.000 Z0.000')
svg_simplify_var = tk.DoubleVar(value=0.2)
svg_arc_circles_var = tk.BooleanVar(value=True)
svg_arc_paths_var = tk.BooleanVar(value=False)
svg_page_var = tk.StringVar(value='8.5x11')
svg_units_var = tk.StringVar(value='in')
svg_pen_up_var = tk.DoubleVar(value=5.0)
svg_pen_down_var = tk.DoubleVar(value=0.0)

# SVG mode selection and options
svg_mode_var = tk.StringVar(value='pen_plot')  # 'pen_plot' or 'cutting'
svg_tool_width_var = tk.DoubleVar(value=0.0)
svg_cutting_offset_var = tk.StringVar(value='outside')  # 'inside' or 'outside'
svg_cut_depth_var = tk.DoubleVar(value=-5.0)
svg_layer_var = tk.StringVar(value='all')  # 'all' or specific layer name

cutout_origin_x_var = tk.DoubleVar(value=0.0)
cutout_origin_y_var = tk.DoubleVar(value=0.0)
cutout_width_var = tk.DoubleVar(value=50.0)
cutout_length_var = tk.DoubleVar(value=50.0)
cutout_tool_diameter_var = tk.DoubleVar(value=6.0)
cutout_depth_var = tk.DoubleVar(value=-3.0)
cutout_step_down_var = tk.DoubleVar(value=1.0)
cutout_feed_var = tk.DoubleVar(value=800.0)
cutout_plunge_feed_var = tk.DoubleVar(value=300.0)
cutout_safe_z_var = tk.DoubleVar(value=5.0)
cutout_bridge_enabled_var = tk.BooleanVar(value=False)
cutout_bridge_count_var = tk.IntVar(value=1)
cutout_bridge_length_var = tk.DoubleVar(value=2.0)
cutout_grid_enabled_var = tk.BooleanVar(value=False)
cutout_grid_cols_var = tk.IntVar(value=2)
cutout_grid_rows_var = tk.IntVar(value=2)

leveling_min_x_var = tk.DoubleVar(value=0.0)
leveling_max_x_var = tk.DoubleVar(value=200.0)
leveling_min_y_var = tk.DoubleVar(value=0.0)
leveling_max_y_var = tk.DoubleVar(value=200.0)
leveling_tool_dia_var = tk.DoubleVar(value=6.0)
leveling_overlap_var = tk.DoubleVar(value=0.5)
leveling_start_z_var = tk.DoubleVar(value=0.0)
leveling_final_z_var = tk.DoubleVar(value=-3.0)
leveling_z_step_var = tk.DoubleVar(value=1.0)
leveling_safe_z_var = tk.DoubleVar(value=5.0)
leveling_plunge_feed_var = tk.DoubleVar(value=300.0)
leveling_feed_var = tk.DoubleVar(value=1500.0)

# Drill tab variables
drill_safe_z_var = tk.DoubleVar(value=5.0)
drill_depth_var = tk.DoubleVar(value=-1.5)
drill_plunge_feed_var = tk.DoubleVar(value=300.0)
drill_travel_feed_var = tk.DoubleVar(value=1000.0)
drill_dwell_var = tk.DoubleVar(value=0.1)
drill_group_by_tool_var = tk.BooleanVar(value=True)
drill_tool_pause_var = tk.BooleanVar(value=True)
drill_tool_pause_mcode_var = tk.StringVar(value='M0')
drill_tool_change_x_var = tk.DoubleVar(value=240.0)
drill_tool_change_y_var = tk.DoubleVar(value=11.0)
drill_tool_change_z_var = tk.DoubleVar(value=5.0)
drill_g92_z_var = tk.DoubleVar(value=0.0)
# Drill tool selection state
drill_tool_include_vars = {}
# Peck drilling options
drill_peck_enable_var = tk.BooleanVar(value=False)
drill_peck_step_var = tk.DoubleVar(value=1.0)
# OctoPrint settings (persisted)
octo_url_var = tk.StringVar(value='http://192.168.0.110/')
octo_api_key_var = tk.StringVar(value='')

ttk.Label(pos_frame, textvariable=current_x_var).grid(column=0, row=0, sticky='e')
ttk.Label(pos_frame, textvariable=current_y_var).grid(column=0, row=1, sticky='e')
ttk.Label(pos_frame, textvariable=current_z_var).grid(column=0, row=2, sticky='e')
ttk.Label(pos_frame, textvariable=origin_var).grid(column=0, row=3, sticky='e')

# Use a notebook so the plot can be viewed separately from the serial console
notebook = ttk.Notebook(root)
console_tab = ttk.Frame(notebook)
plot_tab = ttk.Frame(notebook)
svg_tab = ttk.Frame(notebook)
job_tab = ttk.Frame(notebook)
leveling_tab = ttk.Frame(notebook)
drill_tab = ttk.Frame(notebook)
notebook.add(console_tab, text='Console')
notebook.add(plot_tab, text='Plot')
notebook.add(svg_tab, text='SVG')
cutout_tab = ttk.Frame(notebook)
notebook.add(cutout_tab, text='Cut Out')
notebook.add(job_tab, text='Jobs')
notebook.add(leveling_tab, text='Leveling')
notebook.add(drill_tab, text='Drill')
notebook.grid(column=0, row=4, columnspan=3, padx=5, pady=5, sticky='nsew')

# Cut out tab UI
cutout_frame = ttk.LabelFrame(cutout_tab, text='Cut Out Parameters')
cutout_frame.grid(column=0, row=0, padx=8, pady=8, sticky='ew')

cutout_origin_x_label = ttk.Label(cutout_frame, text='Origin X (mm):')
cutout_origin_x_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)
cutout_origin_x_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_origin_x_var)
cutout_origin_x_entry.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

cutout_origin_y_label = ttk.Label(cutout_frame, text='Origin Y (mm):')
cutout_origin_y_label.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)
cutout_origin_y_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_origin_y_var)
cutout_origin_y_entry.grid(column=3, row=0, padx=5, pady=3, sticky=tk.W)

cutout_width_label = ttk.Label(cutout_frame, text='Width (mm):')
cutout_width_label.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)
cutout_width_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_width_var)
cutout_width_entry.grid(column=1, row=1, padx=5, pady=3, sticky=tk.W)

cutout_length_label = ttk.Label(cutout_frame, text='Length (mm):')
cutout_length_label.grid(column=2, row=1, padx=5, pady=3, sticky=tk.W)
cutout_length_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_length_var)
cutout_length_entry.grid(column=3, row=1, padx=5, pady=3, sticky=tk.W)

cutout_tool_dia_label = ttk.Label(cutout_frame, text='Tool diameter (mm):')
cutout_tool_dia_label.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)
cutout_tool_dia_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_tool_diameter_var)
cutout_tool_dia_entry.grid(column=1, row=2, padx=5, pady=3, sticky=tk.W)

cutout_feed_label = ttk.Label(cutout_frame, text='Feed rate (mm/min):')
cutout_feed_label.grid(column=2, row=2, padx=5, pady=3, sticky=tk.W)
cutout_feed_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_feed_var)
cutout_feed_entry.grid(column=3, row=2, padx=5, pady=3, sticky=tk.W)

cutout_depth_label = ttk.Label(cutout_frame, text='Cut depth Z:')
cutout_depth_label.grid(column=0, row=3, padx=5, pady=3, sticky=tk.W)
cutout_depth_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_depth_var)
cutout_depth_entry.grid(column=1, row=3, padx=5, pady=3, sticky=tk.W)

cutout_step_down_label = ttk.Label(cutout_frame, text='Step down per pass (mm):')
cutout_step_down_label.grid(column=2, row=3, padx=5, pady=3, sticky=tk.W)
cutout_step_down_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_step_down_var)
cutout_step_down_entry.grid(column=3, row=3, padx=5, pady=3, sticky=tk.W)

cutout_safe_label = ttk.Label(cutout_frame, text='Safe Z:')
cutout_safe_label.grid(column=0, row=4, padx=5, pady=3, sticky=tk.W)
cutout_safe_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_safe_z_var)
cutout_safe_entry.grid(column=1, row=4, padx=5, pady=3, sticky=tk.W)

cutout_bridge_check = ttk.Checkbutton(cutout_frame, text='Enable retaining bridges', variable=cutout_bridge_enabled_var)
cutout_bridge_check.grid(column=0, row=5, columnspan=2, padx=5, pady=3, sticky=tk.W)

cutout_bridge_count_label = ttk.Label(cutout_frame, text='Bridge count:')
cutout_bridge_count_label.grid(column=2, row=5, padx=5, pady=3, sticky=tk.W)
cutout_bridge_count_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_bridge_count_var)
cutout_bridge_count_entry.grid(column=3, row=5, padx=5, pady=3, sticky=tk.W)

cutout_bridge_length_label = ttk.Label(cutout_frame, text='Bridge gap length (mm):')
cutout_bridge_length_label.grid(column=0, row=6, padx=5, pady=3, sticky=tk.W)
cutout_bridge_length_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_bridge_length_var)
cutout_bridge_length_entry.grid(column=1, row=6, padx=5, pady=3, sticky=tk.W)

cutout_grid_check = ttk.Checkbutton(cutout_frame, text='Enable grid slicing', variable=cutout_grid_enabled_var)
cutout_grid_check.grid(column=0, row=7, columnspan=2, padx=5, pady=3, sticky=tk.W)

cutout_grid_cols_label = ttk.Label(cutout_frame, text='Columns:')
cutout_grid_cols_label.grid(column=2, row=7, padx=5, pady=3, sticky=tk.W)
cutout_grid_cols_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_grid_cols_var)
cutout_grid_cols_entry.grid(column=3, row=7, padx=5, pady=3, sticky=tk.W)

cutout_grid_rows_label = ttk.Label(cutout_frame, text='Rows:')
cutout_grid_rows_label.grid(column=0, row=8, padx=5, pady=3, sticky=tk.W)
cutout_grid_rows_entry = ttk.Entry(cutout_frame, width=10, textvariable=cutout_grid_rows_var)
cutout_grid_rows_entry.grid(column=1, row=8, padx=5, pady=3, sticky=tk.W)

cutout_button_frame = ttk.Frame(cutout_tab)
cutout_button_frame.grid(column=0, row=1, padx=8, pady=8, sticky='w')

cutout_generate_button = ttk.Button(cutout_button_frame, text='Generate Cut Out', command=make_cutout_from_ui)
cutout_generate_button.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

cutout_send_button = ttk.Button(cutout_button_frame, text='Generate & Load', command=make_cutout_from_ui)
cutout_send_button.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

cutout_note_label = ttk.Label(cutout_tab, text='Cutout path uses tool radius offset. Bridges on top edge (final pass). Grid slicing divides inner workpiece into even cells.')
cutout_note_label.grid(column=0, row=2, padx=8, pady=2, sticky=tk.W)

# General Send G-code panel (works with any tab output)
send_gcode_frame = ttk.LabelFrame(root, text='Send G-code Output')
send_gcode_frame.grid(column=0, row=5, columnspan=3, padx=8, pady=4, sticky='ew')

send_serial_button = ttk.Button(send_gcode_frame, text='Send Over Serial', command=lambda: (run_gcode() if gcode_lines else messagebox.showinfo('No G-code', 'No G-code loaded.')))
send_serial_button.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

send_octo_button = ttk.Button(send_gcode_frame, text='Upload & Print via OctoPrint', command=lambda: upload_and_print_to_octoprint())
send_octo_button.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

send_octo_only_button = ttk.Button(send_gcode_frame, text='Upload Only', command=lambda: upload_to_octoprint(select_only=True))
send_octo_only_button.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)

octo_url_label = ttk.Label(send_gcode_frame, text='OctoPrint URL:')
octo_url_label.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)
octo_url_entry = ttk.Entry(send_gcode_frame, width=40, textvariable=octo_url_var)
octo_url_entry.grid(column=1, row=1, columnspan=2, padx=5, pady=3, sticky=tk.W)

octo_api_label = ttk.Label(send_gcode_frame, text='API Key:')
octo_api_label.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)
octo_api_entry = ttk.Entry(send_gcode_frame, width=40, show='*', textvariable=octo_api_key_var)
octo_api_entry.grid(column=1, row=2, columnspan=2, padx=5, pady=3, sticky=tk.W)

input_label = ttk.Label(console_tab, text="Input:")
input_label.grid(column=0, row=0, padx=5, pady=5, sticky=tk.W)

input_entry = ttk.Entry(console_tab, width=80)
input_entry.grid(column=1, row=0, padx=5, pady=5, sticky=tk.W)
input_entry.focus_set()

send_button = ttk.Button(console_tab, text="Send Message", command=send_message)
send_button.grid(column=2, row=0, padx=5, pady=5)
input_entry.bind('<Return>', send_message)

output_text = tk.Text(console_tab, wrap='word', width=80, height=10, bg='white', fg='black', bd=2, relief='sunken')
output_text.grid(column=0, row=1, columnspan=3, padx=8, pady=8)

parse_json_var = tk.BooleanVar(value=False)
parse_json_cb = ttk.Checkbutton(plot_tab, text='Parse serial data as JSON and plot', variable=parse_json_var)
parse_json_cb.grid(column=0, row=0, padx=5, pady=5, sticky='w')

# Marlin controls
jog_axis_var = tk.StringVar(value='X')
jog_amount_var = tk.StringVar(value='10')

marlin_frame = ttk.LabelFrame(console_tab, text='Marlin Utility Controls')
marlin_frame.grid(column=0, row=2, columnspan=3, padx=5, pady=5, sticky='ew')

# A colored emergency stop button (uses tk.Button for reliable background color)
estop_button = tk.Button(marlin_frame,
                         text='⛔ Emergency Stop',
                         command=send_estop,
                         bg='#d9534f',
                         fg='white',
                         activebackground='#c9302c',
                         activeforeground='white',
                         relief='raised',
                         bd=2,
                         padx=8,
                         pady=4,
                         font=('Helvetica', 10, 'bold'))
estop_button.grid(column=4, row=0, rowspan=2, padx=6, pady=6, sticky='nsew')

home_label = ttk.Label(marlin_frame, text='Homing:')
home_label.grid(column=0, row=0, padx=5, pady=5, sticky=tk.W)

home_x_button = ttk.Button(marlin_frame, text='Home X', command=lambda: home_axis('X'))
home_x_button.grid(column=1, row=0, padx=2, pady=5)
home_y_button = ttk.Button(marlin_frame, text='Home Y', command=lambda: home_axis('Y'))
home_y_button.grid(column=2, row=0, padx=2, pady=5)
home_z_button = ttk.Button(marlin_frame, text='Home Z', command=lambda: home_axis('Z'))
home_z_button.grid(column=3, row=0, padx=2, pady=5)

jog_label = ttk.Label(marlin_frame, text='Jogging:')
jog_label.grid(column=0, row=2, padx=5, pady=5, sticky=tk.W)

axis_x_radio = ttk.Radiobutton(marlin_frame, text='X', variable=jog_axis_var, value='X')
axis_x_radio.grid(column=1, row=2, padx=2, pady=5)
axis_y_radio = ttk.Radiobutton(marlin_frame, text='Y', variable=jog_axis_var, value='Y')
axis_y_radio.grid(column=2, row=2, padx=2, pady=5)
axis_z_radio = ttk.Radiobutton(marlin_frame, text='Z', variable=jog_axis_var, value='Z')
axis_z_radio.grid(column=3, row=2, padx=2, pady=5)

amount_label = ttk.Label(marlin_frame, text='Amount:')
amount_label.grid(column=0, row=3, padx=5, pady=5, sticky=tk.W)

jog_amount_entry = ttk.Entry(marlin_frame, width=10, textvariable=jog_amount_var)
jog_amount_entry.grid(column=1, row=3, padx=2, pady=5)

jog_pos_button = ttk.Button(marlin_frame, text='↑', command=lambda: jog_axis('positive'))
jog_pos_button.grid(column=2, row=3, padx=2, pady=5)
jog_neg_button = ttk.Button(marlin_frame, text='↓', command=lambda: jog_axis('negative'))
jog_neg_button.grid(column=3, row=3, padx=2, pady=5)

# Steppers controls (enable/disable)
stepper_label = ttk.Label(marlin_frame, text='Steppers:')
stepper_label.grid(column=0, row=4, padx=5, pady=5, sticky=tk.W)

enable_steppers_button = ttk.Button(marlin_frame, text='Enable Steppers (M17)', command=lambda: send_gcode('M17'))
enable_steppers_button.grid(column=1, row=4, padx=2, pady=5)

disable_steppers_button = ttk.Button(marlin_frame, text='Disable Steppers (M18)', command=lambda: send_gcode('M18'))
disable_steppers_button.grid(column=2, row=4, padx=2, pady=5)

# Info commands (M122, M119, M115)
info_label = ttk.Label(marlin_frame, text='Info:')
info_label.grid(column=0, row=5, padx=5, pady=5, sticky=tk.W)

m122_button = ttk.Button(marlin_frame, text='Stepper Drivers', command=lambda: send_gcode('M122'))
m122_button.grid(column=1, row=5, padx=2, pady=5)

m119_button = ttk.Button(marlin_frame, text='Endstops', command=lambda: send_gcode('M119'))
m119_button.grid(column=2, row=5, padx=2, pady=5)

m115_button = ttk.Button(marlin_frame, text='Machine Info', command=lambda: send_gcode('M115'))
m115_button.grid(column=3, row=5, padx=2, pady=5)

# Origin controls
set_origin_button = ttk.Button(marlin_frame, text='Set Origin Here', command=lambda: set_origin())
set_origin_button.grid(column=1, row=6, padx=2, pady=5)

clear_origin_button = ttk.Button(marlin_frame, text='Clear Origin', command=lambda: clear_origin())
clear_origin_button.grid(column=2, row=6, padx=2, pady=5)

get_pos_button = ttk.Button(marlin_frame, text='Get Position', command=lambda: send_gcode('M114'))
get_pos_button.grid(column=3, row=6, padx=2, pady=5)


def rescan_serial_ports():
    ports = get_available_ports()
    port_combobox['values'] = ports
    if not port_combobox.get() and ports:
        default = find_first_acm_port(ports) or ports[0]
        port_combobox.set(default)

rescan_button = ttk.Button(root, text="Rescan Serial Ports", command=rescan_serial_ports)
rescan_button.grid(column=2, row=0, padx=5, pady=5)

# G-code controls: process SVG, load, run, stop (moved to SVG tab)
gcode_frame = ttk.Frame(svg_tab)
gcode_frame.grid(column=0, row=3, columnspan=3, padx=8, pady=4, sticky='w')

process_svg_button = ttk.Button(gcode_frame, text='Process SVG', command=process_svg_to_gcode)
process_svg_button.grid(column=0, row=0, padx=2, pady=2)

load_button = ttk.Button(gcode_frame, text='Load G-code', command=load_gcode)
load_button.grid(column=1, row=0, padx=2, pady=2)

run_button = ttk.Button(gcode_frame, text='Run G-code', command=run_gcode)
run_button.grid(column=2, row=0, padx=2, pady=2)

stop_button = ttk.Button(gcode_frame, text='Stop G-code', command=stop_gcode)
stop_button.grid(column=3, row=0, padx=2, pady=2)

# SVG mode selector
mode_frame = ttk.LabelFrame(svg_tab, text='SVG Processing Mode')
mode_frame.grid(column=0, row=4, columnspan=3, padx=8, pady=4, sticky='w')

def update_svg_mode():
    """Show/hide mode-specific options based on selected mode"""
    mode = svg_mode_var.get()
    if mode == 'pen_plot':
        pen_options_frame.grid()
        cutting_options_frame.grid_remove()
    else:
        pen_options_frame.grid_remove()
        cutting_options_frame.grid()

mode_label = ttk.Label(mode_frame, text='Mode:')
mode_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

mode_radio_pen = ttk.Radiobutton(mode_frame, text='Pen Plot (XY plotting)', variable=svg_mode_var, value='pen_plot', command=update_svg_mode)
mode_radio_pen.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

mode_radio_cut = ttk.Radiobutton(mode_frame, text='Cutting (tool path with offset)', variable=svg_mode_var, value='cutting', command=update_svg_mode)
mode_radio_cut.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)

# Common options frame
common_frame = ttk.LabelFrame(svg_tab, text='Common SVG Options')
common_frame.grid(column=0, row=5, columnspan=3, padx=8, pady=4, sticky='ew')

page_label = ttk.Label(common_frame, text='Page size:')
page_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

page_entry = ttk.Entry(common_frame, width=12, textvariable=svg_page_var)
page_entry.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

units_label = ttk.Label(common_frame, text='Units:')
units_label.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)

units_combobox = ttk.Combobox(common_frame, values=['in', 'mm'], width=5, textvariable=svg_units_var, state='readonly')
units_combobox.grid(column=3, row=0, padx=5, pady=3, sticky=tk.W)

simplify_label = ttk.Label(common_frame, text='Simplify (mm):')
simplify_label.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)

simplify_entry = ttk.Entry(common_frame, width=10, textvariable=svg_simplify_var)
simplify_entry.grid(column=1, row=1, padx=5, pady=3, sticky=tk.W)

arc_circles_check = ttk.Checkbutton(common_frame, text='Arc circles', variable=svg_arc_circles_var)
arc_circles_check.grid(column=2, row=1, padx=5, pady=3, sticky=tk.W)

arc_paths_check = ttk.Checkbutton(common_frame, text='Arc A-paths', variable=svg_arc_paths_var)
arc_paths_check.grid(column=3, row=1, padx=5, pady=3, sticky=tk.W)

# Pen plotting options frame
pen_options_frame = ttk.LabelFrame(svg_tab, text='Pen Plotting Options')
pen_options_frame.grid(column=0, row=6, columnspan=3, padx=8, pady=4, sticky='ew')

pen_up_label = ttk.Label(pen_options_frame, text='Pen up Z:')
pen_up_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

pen_up_entry = ttk.Entry(pen_options_frame, width=10, textvariable=svg_pen_up_var)
pen_up_entry.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

pen_down_label = ttk.Label(pen_options_frame, text='Pen down Z:')
pen_down_label.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)

pen_down_entry = ttk.Entry(pen_options_frame, width=10, textvariable=svg_pen_down_var)
pen_down_entry.grid(column=3, row=0, padx=5, pady=3, sticky=tk.W)

# Cutting tool options frame
cutting_options_frame = ttk.LabelFrame(svg_tab, text='Cutting Tool Options')
cutting_options_frame.grid(column=0, row=6, columnspan=3, padx=8, pady=4, sticky='ew')
cutting_options_frame.grid_remove()

tool_width_label = ttk.Label(cutting_options_frame, text='Tool width (mm):')
tool_width_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

tool_width_entry = ttk.Entry(cutting_options_frame, width=10, textvariable=svg_tool_width_var)
tool_width_entry.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

offset_label = ttk.Label(cutting_options_frame, text='Offset direction:')
offset_label.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)

offset_combobox = ttk.Combobox(cutting_options_frame, values=['inside', 'outside'], width=8, textvariable=svg_cutting_offset_var, state='readonly')
offset_combobox.grid(column=3, row=0, padx=5, pady=3, sticky=tk.W)

depth_label = ttk.Label(cutting_options_frame, text='Cut depth Z:')
depth_label.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)

depth_entry = ttk.Entry(cutting_options_frame, width=10, textvariable=svg_cut_depth_var)
depth_entry.grid(column=1, row=1, padx=5, pady=3, sticky=tk.W)

layer_label = ttk.Label(cutting_options_frame, text='Layer:')
layer_label.grid(column=2, row=1, padx=5, pady=3, sticky=tk.W)

layer_combobox = ttk.Combobox(cutting_options_frame, values=['all', 'layer_1', 'layer_2', 'layer_3'], width=12, textvariable=svg_layer_var, state='readonly')
layer_combobox.grid(column=3, row=1, padx=5, pady=3, sticky=tk.W)

# Load persisted config (start/stop job g-code)
load_config()

preview_shown_var = tk.BooleanVar(value=False)

# SVG visualization frame
svg_viz_frame = ttk.LabelFrame(svg_tab, text='SVG Preview')
svg_viz_frame.grid(column=0, row=7, columnspan=3, padx=8, pady=4, sticky='ew')
svg_viz_frame.grid_remove()

svg_viz_canvas_frame = ttk.Frame(svg_viz_frame)
svg_viz_canvas_frame.grid(column=0, row=0, columnspan=3, padx=5, pady=5, sticky='ew')
svg_viz_canvas = tk.Canvas(svg_viz_canvas_frame, width=400, height=300, bg='white', relief='sunken', bd=2)
svg_viz_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

svg_viz_scroll = ttk.Scrollbar(svg_viz_canvas_frame, orient='vertical', command=svg_viz_canvas.yview)
svg_viz_scroll.pack(side=tk.RIGHT, fill=tk.Y)
svg_viz_canvas.configure(yscrollcommand=svg_viz_scroll.set)

svg_preview_button = ttk.Button(svg_viz_frame, text='Show/Hide SVG Preview', command=lambda: svg_viz_frame.grid_remove() if svg_viz_frame.winfo_viewable() else svg_viz_frame.grid())
svg_preview_button.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)

# G-code preview frame (toggleable)
preview_toggle = ttk.Checkbutton(pen_options_frame, text='Show G-code Preview', variable=preview_shown_var)
preview_toggle.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)

preview_frame = ttk.LabelFrame(svg_tab, text='G-code Preview (editable)')
preview_frame.grid(column=0, row=8, columnspan=3, padx=8, pady=4, sticky='ew')
preview_frame.grid_remove()

def _toggle_preview():
    if preview_shown_var.get():
        preview_frame.grid()
    else:
        preview_frame.grid_remove()

preview_shown_var.trace_add('write', lambda *a: _toggle_preview())

preview_text = tk.Text(preview_frame, height=12, wrap='none')
preview_text.grid(column=0, row=0, columnspan=4, padx=5, pady=5, sticky='ew')
preview_scroll_x = ttk.Scrollbar(preview_frame, orient='horizontal', command=preview_text.xview)
preview_scroll_x.grid(column=0, row=1, columnspan=4, sticky='ew')
preview_text.configure(xscrollcommand=preview_scroll_x.set)

apply_preview_button = ttk.Button(preview_frame, text='Apply Preview (use for run)', command=lambda: apply_preview())
apply_preview_button.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)

# Job start/stop G-code (persisted)
job_frame = ttk.LabelFrame(job_tab, text='Job G-code (persisted)')
job_frame.grid(column=0, row=0, columnspan=3, padx=8, pady=4, sticky='ew')

start_label = ttk.Label(job_frame, text='Start Job G-code:')
start_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)
start_job_text = tk.Text(job_frame, height=4, width=60)
start_job_text.grid(column=0, row=1, padx=5, pady=3, sticky='w')

stop_label = ttk.Label(job_frame, text='Stop Job G-code:')
stop_label.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)
stop_job_text = tk.Text(job_frame, height=4, width=60)
stop_job_text.grid(column=1, row=1, padx=5, pady=3, sticky='w')

save_job_button = ttk.Button(job_frame, text='Save Job G-code', command=lambda: save_config())
save_job_button.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)

# Leveling controls
leveling_frame = ttk.LabelFrame(leveling_tab, text='Leveling Parameters')
leveling_frame.grid(column=0, row=0, padx=8, pady=4, sticky='ew')

min_x_label = ttk.Label(leveling_frame, text='Min X:')
min_x_label.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)
min_x_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_min_x_var)
min_x_entry.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)

max_x_label = ttk.Label(leveling_frame, text='Max X:')
max_x_label.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)
max_x_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_max_x_var)
max_x_entry.grid(column=3, row=0, padx=5, pady=3, sticky=tk.W)

min_y_label = ttk.Label(leveling_frame, text='Min Y:')
min_y_label.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)
min_y_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_min_y_var)
min_y_entry.grid(column=1, row=1, padx=5, pady=3, sticky=tk.W)

max_y_label = ttk.Label(leveling_frame, text='Max Y:')
max_y_label.grid(column=2, row=1, padx=5, pady=3, sticky=tk.W)
max_y_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_max_y_var)
max_y_entry.grid(column=3, row=1, padx=5, pady=3, sticky=tk.W)

fill_min_button = ttk.Button(leveling_frame, text='Use Current Position as Min', command=lambda: _fill_leveling_bounds(True))
fill_min_button.grid(column=0, row=2, columnspan=2, padx=5, pady=3, sticky=tk.W)
fill_max_button = ttk.Button(leveling_frame, text='Use Current Position as Max', command=lambda: _fill_leveling_bounds(False))
fill_max_button.grid(column=2, row=2, columnspan=2, padx=5, pady=3, sticky=tk.W)

tool_label = ttk.Label(leveling_frame, text='Tool Dia (mm):')
tool_label.grid(column=0, row=3, padx=5, pady=3, sticky=tk.W)
tool_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_tool_dia_var)
tool_entry.grid(column=1, row=3, padx=5, pady=3, sticky=tk.W)

overlap_label = ttk.Label(leveling_frame, text='Overlap (%):')
overlap_label.grid(column=2, row=3, padx=5, pady=3, sticky=tk.W)
overlap_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_overlap_var)
overlap_entry.grid(column=3, row=3, padx=5, pady=3, sticky=tk.W)

defaults_label = ttk.Label(leveling_frame, text='(0.5 = 50% overlap)')
defaults_label.grid(column=0, row=4, columnspan=2, padx=5, pady=3, sticky=tk.W)

start_z_label = ttk.Label(leveling_frame, text='Start Z (mm):')
start_z_label.grid(column=0, row=5, padx=5, pady=3, sticky=tk.W)
start_z_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_start_z_var)
start_z_entry.grid(column=1, row=5, padx=5, pady=3, sticky=tk.W)

final_z_label = ttk.Label(leveling_frame, text='Final Z (mm):')
final_z_label.grid(column=2, row=5, padx=5, pady=3, sticky=tk.W)
final_z_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_final_z_var)
final_z_entry.grid(column=3, row=5, padx=5, pady=3, sticky=tk.W)

z_step_label = ttk.Label(leveling_frame, text='Z Step (mm):')
z_step_label.grid(column=0, row=6, padx=5, pady=3, sticky=tk.W)
z_step_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_z_step_var)
z_step_entry.grid(column=1, row=6, padx=5, pady=3, sticky=tk.W)

safe_z_label = ttk.Label(leveling_frame, text='Safe Z (mm):')
safe_z_label.grid(column=2, row=6, padx=5, pady=3, sticky=tk.W)
safe_z_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_safe_z_var)
safe_z_entry.grid(column=3, row=6, padx=5, pady=3, sticky=tk.W)

feed_label = ttk.Label(leveling_frame, text='Feed F (mm/min):')
feed_label.grid(column=0, row=7, padx=5, pady=3, sticky=tk.W)
feed_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_feed_var)
feed_entry.grid(column=1, row=7, padx=5, pady=3, sticky=tk.W)

plunge_label = ttk.Label(leveling_frame, text='Plunge F (mm/min):')
plunge_label.grid(column=2, row=7, padx=5, pady=3, sticky=tk.W)
plunge_entry = ttk.Entry(leveling_frame, width=10, textvariable=leveling_plunge_feed_var)
plunge_entry.grid(column=3, row=7, padx=5, pady=3, sticky=tk.W)

leveling_preview_frame = ttk.LabelFrame(leveling_tab, text='Leveling G-code Preview')
leveling_preview_frame.grid(column=0, row=1, padx=8, pady=4, sticky='ew')

leveling_preview_text = tk.Text(leveling_preview_frame, height=12, wrap='none')
leveling_preview_text.grid(column=0, row=0, columnspan=4, padx=5, pady=5, sticky='ew')
leveling_preview_scroll_x = ttk.Scrollbar(leveling_preview_frame, orient='horizontal', command=leveling_preview_text.xview)
leveling_preview_scroll_x.grid(column=0, row=1, columnspan=4, sticky='ew')
leveling_preview_scroll_y = ttk.Scrollbar(leveling_preview_frame, orient='vertical', command=leveling_preview_text.yview)
leveling_preview_scroll_y.grid(column=4, row=0, rowspan=2, sticky='ns')
leveling_preview_text.configure(xscrollcommand=leveling_preview_scroll_x.set, yscrollcommand=leveling_preview_scroll_y.set)

generate_leveling_button = ttk.Button(leveling_preview_frame, text='Generate Leveling G-code', command=generate_leveling_gcode_action)
generate_leveling_button.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)

load_leveling_button = ttk.Button(leveling_preview_frame, text='Load Leveling to Buffer', command=generate_leveling_gcode_action)
load_leveling_button.grid(column=1, row=2, padx=5, pady=3, sticky=tk.W)

run_leveling_button = ttk.Button(leveling_preview_frame, text='Run Leveling', command=lambda: [generate_leveling_gcode_action(), run_gcode()])
run_leveling_button.grid(column=2, row=2, padx=5, pady=3, sticky=tk.W)

# Drill tab UI
drill_frame = ttk.LabelFrame(drill_tab, text='DRL Import / Drill Controls')
drill_frame.grid(column=0, row=0, padx=8, pady=4, sticky='ew')

drill_load_button = ttk.Button(drill_frame, text='Load DRL File', command=load_drl_file)
drill_load_button.grid(column=0, row=0, padx=5, pady=3, sticky=tk.W)

drill_safe_label = ttk.Label(drill_frame, text='Safe Z (mm):')
drill_safe_label.grid(column=1, row=0, padx=5, pady=3, sticky=tk.W)
drill_safe_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_safe_z_var)
drill_safe_entry.grid(column=2, row=0, padx=5, pady=3, sticky=tk.W)

drill_depth_label = ttk.Label(drill_frame, text='Drill Depth (mm):')
drill_depth_label.grid(column=0, row=1, padx=5, pady=3, sticky=tk.W)
drill_depth_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_depth_var)
drill_depth_entry.grid(column=1, row=1, padx=5, pady=3, sticky=tk.W)

drill_plunge_label = ttk.Label(drill_frame, text='Plunge F (mm/min):')
drill_plunge_label.grid(column=2, row=1, padx=5, pady=3, sticky=tk.W)
drill_plunge_entry = ttk.Entry(drill_frame, width=10, textvariable=drill_plunge_feed_var)
drill_plunge_entry.grid(column=3, row=1, padx=5, pady=3, sticky=tk.W)

drill_travel_label = ttk.Label(drill_frame, text='Travel F (mm/min):')
drill_travel_label.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)
drill_travel_entry = ttk.Entry(drill_frame, width=10, textvariable=drill_travel_feed_var)
drill_travel_entry.grid(column=1, row=2, padx=5, pady=3, sticky=tk.W)

drill_dwell_label = ttk.Label(drill_frame, text='Dwell (s):')
drill_dwell_label.grid(column=2, row=2, padx=5, pady=3, sticky=tk.W)
drill_dwell_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_dwell_var)
drill_dwell_entry.grid(column=3, row=2, padx=5, pady=3, sticky=tk.W)

# Grouping and peck options
drill_group_check = ttk.Checkbutton(drill_frame, text='Group by tool', variable=drill_group_by_tool_var)
drill_group_check.grid(column=0, row=3, padx=5, pady=3, sticky=tk.W)

drill_tool_pause_check = ttk.Checkbutton(drill_frame, text='Pause for tool change', variable=drill_tool_pause_var)
drill_tool_pause_check.grid(column=1, row=3, padx=5, pady=3, sticky=tk.W)

drill_tool_pause_entry = ttk.Entry(drill_frame, width=6, textvariable=drill_tool_pause_mcode_var)
drill_tool_pause_entry.grid(column=2, row=3, padx=5, pady=3, sticky=tk.W)

drill_peck_check = ttk.Checkbutton(drill_frame, text='Enable peck drilling', variable=drill_peck_enable_var)
drill_peck_check.grid(column=0, row=4, padx=5, pady=3, sticky=tk.W)

drill_peck_step_label = ttk.Label(drill_frame, text='Peck step (mm):')
drill_peck_step_label.grid(column=1, row=4, padx=5, pady=3, sticky=tk.W)
drill_peck_step_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_peck_step_var)
drill_peck_step_entry.grid(column=2, row=4, padx=5, pady=3, sticky=tk.W)

drill_change_label = ttk.Label(drill_frame, text='Tool-change park X/Y/Z:')
drill_change_label.grid(column=0, row=5, padx=5, pady=3, sticky=tk.W)
drill_change_x_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_tool_change_x_var)
drill_change_x_entry.grid(column=1, row=5, padx=5, pady=3, sticky=tk.W)
drill_change_y_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_tool_change_y_var)
drill_change_y_entry.grid(column=2, row=5, padx=5, pady=3, sticky=tk.W)
drill_change_z_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_tool_change_z_var)
drill_change_z_entry.grid(column=3, row=5, padx=5, pady=3, sticky=tk.W)

drill_g92_label = ttk.Label(drill_frame, text='G92 Z:')
drill_g92_label.grid(column=0, row=6, padx=5, pady=3, sticky=tk.W)
drill_g92_entry = ttk.Entry(drill_frame, width=8, textvariable=drill_g92_z_var)
drill_g92_entry.grid(column=1, row=6, padx=5, pady=3, sticky=tk.W)
drill_g92_button = ttk.Button(drill_frame, text='Send G92 Z', command=lambda: send_g92_z_command())
drill_g92_button.grid(column=2, row=6, padx=5, pady=3, sticky=tk.W)

drill_tools_frame = ttk.LabelFrame(drill_tab, text='Detected Tools / Counts')
drill_tools_frame.grid(column=0, row=1, padx=8, pady=4, sticky='ew')
drill_tools_container = ttk.Frame(drill_tools_frame)
drill_tools_container.grid(column=0, row=0, padx=5, pady=5, sticky='ew')

drill_preview_frame = ttk.LabelFrame(drill_tab, text='Drill G-code Preview')
drill_preview_frame.grid(column=0, row=2, padx=8, pady=4, sticky='ew')

drill_preview_text = tk.Text(drill_preview_frame, height=12, wrap='none')
drill_preview_text.grid(column=0, row=0, columnspan=4, padx=5, pady=5, sticky='ew')
drill_preview_scroll_x = ttk.Scrollbar(drill_preview_frame, orient='horizontal', command=drill_preview_text.xview)
drill_preview_scroll_x.grid(column=0, row=1, columnspan=4, sticky='ew')
drill_preview_text.configure(xscrollcommand=drill_preview_scroll_x.set)

drill_loadbuf_button = ttk.Button(drill_preview_frame, text='Load Drill to Buffer', command=load_drill_to_buffer)
drill_loadbuf_button.grid(column=0, row=2, padx=5, pady=3, sticky=tk.W)

drill_regen_button = ttk.Button(drill_preview_frame, text='Regenerate G-code', command=lambda: regenerate_drill_gcode())
drill_regen_button.grid(column=0, row=2, padx=5, pady=3, sticky=tk.E)

drill_run_button = ttk.Button(drill_preview_frame, text='Run Drill G-code', command=lambda: run_gcode())
drill_run_button.grid(column=1, row=2, padx=5, pady=3, sticky=tk.W)

drill_tools_frame = ttk.LabelFrame(drill_tab, text='Detected Tools / Counts')
try:
    if start_job_gcode_str:
        start_job_text.insert('1.0', start_job_gcode_str)
    if stop_job_gcode_str:
        stop_job_text.insert('1.0', stop_job_gcode_str)
except Exception:
    pass

# populate persisted octo settings if loaded earlier
try:
    if 'octo_saved_url' in globals() and octo_saved_url:
        octo_url_var.set(octo_saved_url)
    if 'octo_saved_key' in globals() and octo_saved_key:
        octo_api_key_var.set(octo_saved_key)
except Exception:
    pass

rescan_serial_ports()
if default_port:
    root.after(100, open_connection)

fig, ax = plt.subplots(figsize=(5, 4))
canvas = FigureCanvasTkAgg(fig, master=plot_tab)
canvas_widget = canvas.get_tk_widget()
canvas_widget.grid(column=0, row=1, columnspan=3, padx=5, pady=5)

# Ensure clean shutdown on window close or Ctrl-C (SIGINT)
def on_closing():
    try:
        if ser and getattr(ser, 'is_open', False):
            try:
                ser.close()
            except Exception:
                pass
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        try:
            plt.close('all')
        except Exception:
            pass

def _handle_sigint(signum, frame):
    # Schedule GUI-safe shutdown from the signal handler
    try:
        root.after(0, on_closing)
    except Exception:
        pass
    # Start a short watchdog to forcibly exit if cleanup doesn't finish
    def _watchdog():
        sleep(0.5)
        try:
            os._exit(0)
        except Exception:
            pass

    try:
        t = threading.Thread(target=_watchdog, daemon=True)
        t.start()
    except Exception:
        try:
            os._exit(0)
        except Exception:
            pass

root.protocol("WM_DELETE_WINDOW", on_closing)
signal.signal(signal.SIGINT, _handle_sigint)
# Start the Tkinter event loop
root.mainloop()
