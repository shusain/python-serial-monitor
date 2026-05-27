#!/usr/bin/env python3
"""
svg2gcode.py

Simple SVG to G-code converter for plotting on a rectangular page.

Usage:
    python3 svg2gcode.py input.svg -o output.gcode --page 8.5x11 --units in

Features:
- Scales the SVG to 80% of the page size, preserving aspect ratio.
- Centers the artwork on the page.
- Samples paths and polyline/polygon elements and emits G0/G1 moves.
- Can simplify sampled points and optionally emit G2/G3 arcs for circle objects or SVG path arc commands.
- Produces G-code in mm (G21) by default; can accept inches input.

Dependencies: svgpathtools (adds to requirements.txt)
"""
import argparse
import math
import sys
from xml.etree import ElementTree as ET

try:
    from svgpathtools import parse_path, Arc, Line, CubicBezier, QuadraticBezier
except Exception:
    print("Missing dependency: svgpathtools. Install with 'pip install svgpathtools'", file=sys.stderr)
    raise


MM_PER_INCH = 25.4


def parse_page_size(val, default_units='in'):
    # accept formats like 8.5x11 or 210x297mm
    if 'x' not in val:
        raise argparse.ArgumentTypeError("page size must be in WIDTHxHEIGHT format")
    w_str, h_str = val.split('x')
    def parse_num(s):
        s = s.strip()
        if s.endswith('mm'):
            return float(s[:-2]), 'mm'
        if s.endswith('in'):
            return float(s[:-2]), 'in'
        return float(s), default_units
    w, wu = parse_num(w_str)
    h, hu = parse_num(h_str)
    # if units differ, convert both to inches
    if wu == 'mm':
        w = w / MM_PER_INCH
    if hu == 'mm':
        h = h / MM_PER_INCH
    return float(w), float(h)


def extract_paths(svgroot):
    ns = {'svg': 'http://www.w3.org/2000/svg'}
    # find all path, polyline, polygon, line elements
    elems = []
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}path'):
        d = elem.get('d')
        if d:
            elems.append(('path', d))
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}polyline'):
        pts = elem.get('points')
        if pts:
            elems.append(('polyline', pts))
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}polygon'):
        pts = elem.get('points')
        if pts:
            elems.append(('polygon', pts))
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}line'):
        x1 = elem.get('x1'); y1 = elem.get('y1'); x2 = elem.get('x2'); y2 = elem.get('y2')
        if None not in (x1, y1, x2, y2):
            elems.append(('line', (float(x1), float(y1), float(x2), float(y2))))
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}rect'):
        x = float(elem.get('x', '0'))
        y = float(elem.get('y', '0'))
        width = float(elem.get('width', '0'))
        height = float(elem.get('height', '0'))
        rx = float(elem.get('rx', '0') or '0')
        ry = float(elem.get('ry', '0') or '0')
        elems.append(('rect', (x, y, width, height, rx, ry)))
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}circle'):
        cx = float(elem.get('cx', '0'))
        cy = float(elem.get('cy', '0'))
        r = float(elem.get('r', '0'))
        elems.append(('circle', (cx, cy, r)))
    for elem in svgroot.findall('.//{http://www.w3.org/2000/svg}ellipse'):
        cx = float(elem.get('cx', '0'))
        cy = float(elem.get('cy', '0'))
        rx = float(elem.get('rx', '0'))
        ry = float(elem.get('ry', '0'))
        elems.append(('ellipse', (cx, cy, rx, ry)))
    return elems


def points_from_points_attr(points_str):
    pts = []
    parts = points_str.replace(',', ' ').split()
    for i in range(0, len(parts)-1, 2):
        try:
            x = float(parts[i]); y = float(parts[i+1])
            pts.append((x, y))
        except ValueError:
            continue
    return pts


def sample_path_d(d_str, resolution_mm=0.5):
    path = parse_path(d_str)
    L = path.length()
    # number of samples
    n = max(2, int(math.ceil(L / resolution_mm)))
    pts = []
    for i in range(n+1):
        t = i / n
        p = path.point(t)
        pts.append((p.real, p.imag))
    return pts


def sample_path_segment(segment, resolution_mm=0.5):
    if isinstance(segment, (Line, Arc)):
        return [(segment.start.real, segment.start.imag), (segment.end.real, segment.end.imag)]
    length = segment.length(error=1e-3)
    n = max(2, int(math.ceil(length / resolution_mm)))
    pts = []
    for i in range(n+1):
        t = i / n
        p = segment.point(t)
        pts.append((p.real, p.imag))
    return pts


def is_full_circle_arc(segment):
    return isinstance(segment, Arc) and abs(segment.start - segment.end) < 1e-6


def path_to_gcode(d_str, scale, tx, ty, flip_y, pen_down_z, pen_up_z, feed, rapid_feed, resolution_mm=0.5, simplify=0.0):
    path = parse_path(d_str)
    lines = []
    current_end = None
    for segment in path:
        if current_end is None or abs(segment.start - current_end) > 1e-6:
            if current_end is not None:
                lines.append(f'G1 Z{pen_up_z:.3f} F{rapid_feed} ; pen up')
            sx, sy = transform_point((segment.start.real, segment.start.imag), scale, tx, ty, flip_y)
            lines.append(f'G0 X{sx:.3f} Y{sy:.3f} F{rapid_feed}')
            lines.append(f'G1 Z{pen_down_z:.3f} F{rapid_feed} ; pen down')

        if isinstance(segment, Arc):
            sx, sy = transform_point((segment.start.real, segment.start.imag), scale, tx, ty, flip_y)
            cx, cy = transform_point((segment.center.real, segment.center.imag), scale, tx, ty, flip_y)
            i = cx - sx
            j = cy - sy
            if is_full_circle_arc(segment):
                cmd = 'G3' if segment.sweep else 'G2'
                if flip_y:
                    cmd = 'G2' if cmd == 'G3' else 'G3'
                lines.append(f'{cmd} I{i:.3f} J{j:.3f} F{feed}')
            else:
                ex, ey = transform_point((segment.end.real, segment.end.imag), scale, tx, ty, flip_y)
                cmd = 'G3' if segment.sweep else 'G2'
                if flip_y:
                    cmd = 'G2' if cmd == 'G3' else 'G3'
                lines.append(f'{cmd} X{ex:.3f} Y{ey:.3f} I{i:.3f} J{j:.3f} F{feed}')
        else:
            pts = sample_path_segment(segment, resolution_mm)
            if simplify > 0.0:
                pts = simplify_points(pts, simplify)
            tpts = transform_points(pts, scale, tx, ty, flip_y=flip_y)
            if len(tpts) > 1:
                lines.extend(points_to_gcode(tpts[1:], feed))

        current_end = segment.end

    if current_end is not None:
        lines.append(f'G1 Z{pen_up_z:.3f} F{rapid_feed} ; pen up')
    return lines


def sample_circle(cx, cy, r, resolution_mm=0.5):
    circumference = 2.0 * math.pi * r
    n = max(16, int(math.ceil(circumference / resolution_mm)))
    pts = []
    for i in range(n+1):
        theta = 2.0 * math.pi * i / n
        pts.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
    return pts


def sample_ellipse(cx, cy, rx, ry, resolution_mm=0.5):
    # approximate ellipse circumference for sampling count
    circumference = 2.0 * math.pi * math.sqrt((rx * rx + ry * ry) / 2.0)
    n = max(16, int(math.ceil(circumference / resolution_mm)))
    pts = []
    for i in range(n+1):
        theta = 2.0 * math.pi * i / n
        pts.append((cx + rx * math.cos(theta), cy + ry * math.sin(theta)))
    return pts


def sample_rect(x, y, width, height, rx=0.0, ry=0.0):
    # ignore rounded corners for now
    return [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
        (x, y)
    ]


def simplify_points(points, tolerance):
    if tolerance <= 0 or len(points) < 3:
        return points

    def point_line_distance(point, start, end):
        x0, y0 = point
        x1, y1 = start
        x2, y2 = end
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(x0 - x1, y0 - y1)
        return abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / math.hypot(dx, dy)

    def rdp(pts):
        if len(pts) < 3:
            return pts
        start = pts[0]
        end = pts[-1]
        max_dist = 0.0
        index = 0
        for i in range(1, len(pts) - 1):
            dist = point_line_distance(pts[i], start, end)
            if dist > max_dist:
                max_dist = dist
                index = i
        if max_dist <= tolerance:
            return [start, end]
        left = rdp(pts[:index+1])
        right = rdp(pts[index:])
        return left[:-1] + right

    return rdp(points)


def transform_points(points, scale, tx, ty, flip_y=False):
    out = []
    for x, y in points:
        sx = x * scale + tx
        sy = y * scale
        if flip_y:
            sy = -sy
        sy += ty
        out.append((sx, sy))
    return out


def transform_point(point, scale, tx, ty, flip_y=False):
    x, y = point
    sx = x * scale + tx
    sy = y * scale
    if flip_y:
        sy = -sy
    sy += ty
    return sx, sy


def compute_bbox_of_elements(elems):
    xs = []
    ys = []
    for typ, val in elems:
        if typ == 'path':
            pts = sample_path_d(val, resolution_mm=1.0)
        elif typ in ('polyline', 'polygon'):
            pts = points_from_points_attr(val)
        elif typ == 'line':
            x1, y1, x2, y2 = val
            pts = [(x1, y1), (x2, y2)]
        elif typ == 'rect':
            x, y, width, height, rx, ry = val
            pts = sample_rect(x, y, width, height, rx, ry)
        elif typ == 'circle':
            cx, cy, r = val
            pts = sample_circle(cx, cy, r, resolution_mm=1.0)
        elif typ == 'ellipse':
            cx, cy, rx, ry = val
            pts = sample_ellipse(cx, cy, rx, ry, resolution_mm=1.0)
        else:
            pts = []
        for x, y in pts:
            xs.append(x); ys.append(y)
    if not xs or not ys:
        return 0,0,0,0
    return min(xs), min(ys), max(xs), max(ys)


def points_to_gcode(pts, feed):
    lines = []
    for x, y in pts:
        lines.append(f"G1 X{float(x):.3f} Y{float(y):.3f} F{feed}")
    return lines


def circle_to_gcode(cx, cy, r, scale, tx, ty, flip_y, pen_down_z, pen_up_z, feed, rapid_feed):
    # Draw a full circle using a single G2/G3 command with center offsets only.
    # Marlin can draw a complete circle when X/Y are omitted and I/J specify the center.
    start = transform_point((cx + r, cy), scale, tx, ty, flip_y)
    center = transform_point((cx, cy), scale, tx, ty, flip_y)
    i = center[0] - start[0]
    j = center[1] - start[1]
    cmd = 'G3'
    if flip_y:
        cmd = 'G2'
    lines = []
    lines.append(f"G0 X{start[0]:.3f} Y{start[1]:.3f} F{rapid_feed}")
    lines.append(f"G1 Z{pen_down_z:.3f} F{rapid_feed} ; pen down")
    lines.append(f"{cmd} I{i:.3f} J{j:.3f} F{feed}")
    lines.append(f"G1 Z{pen_up_z:.3f} F{rapid_feed} ; pen up")
    return lines


def convert_svg_to_gcode(svgfile, output=None, page='8.5x11', units='in', resolution=0.5, pen_up=5.0, pen_down=0.0, feed=1500.0, rapid_feed=3000.0, no_scale=False, no_offset=False, arc_circles=False, arc_paths=False, simplify=0.0, write_output=True):
    page_w_in, page_h_in = parse_page_size(page, default_units=units)
    page_w_mm = page_w_in * MM_PER_INCH
    page_h_mm = page_h_in * MM_PER_INCH

    tree = ET.parse(svgfile)
    root = tree.getroot()
    elems = extract_paths(root)
    if not elems:
        raise ValueError('No supported vector elements found in SVG.')

    minx, miny, maxx, maxy = compute_bbox_of_elements(elems)
    svg_w = maxx - minx
    svg_h = maxy - miny
    if svg_w == 0 or svg_h == 0:
        raise ValueError('SVG has zero width or height')

    if no_scale:
        scale = 1.0
    else:
        target_w = page_w_mm * 0.8
        target_h = page_h_mm * 0.8
        scale = min(target_w / svg_w, target_h / svg_h)

    if no_offset:
        tx = 0.0
        ty = 0.0
        flip_y = False
    else:
        svg_cx = (minx + maxx) / 2.0
        svg_cy = (miny + maxy) / 2.0
        page_cx = page_w_mm / 2.0
        page_cy = page_h_mm / 2.0
        tx = page_cx - svg_cx * scale
        ty = page_cy + svg_cy * scale
        flip_y = True

    out_lines = []
    out_lines.append('; Generated by svg2gcode.py')
    out_lines.append('G21 ; units mm')
    out_lines.append('G90 ; absolute coordinates')
    out_lines.append(f'G1 Z{pen_up:.3f} F{rapid_feed} ; pen up')

    page_warning = False
    trans_minx = trans_miny = trans_maxx = trans_maxy = None
    for typ, val in elems:
        if typ == 'path':
            if arc_paths:
                out_lines.extend(path_to_gcode(val, scale, tx, ty, flip_y, pen_down, pen_up, feed, rapid_feed, resolution_mm=resolution, simplify=simplify))
                continue
            pts = sample_path_d(val, resolution_mm=resolution)
        elif typ in ('polyline', 'polygon'):
            pts = points_from_points_attr(val)
        elif typ == 'line':
            x1,y1,x2,y2 = val
            pts = [(x1,y1),(x2,y2)]
        elif typ == 'rect':
            x,y,width,height,rx,ry = val
            pts = sample_rect(x, y, width, height, rx, ry)
        elif typ == 'circle':
            cx, cy, r = val
            if arc_circles:
                out_lines.extend(circle_to_gcode(cx, cy, r, scale, tx, ty, flip_y, pen_down, pen_up, feed, rapid_feed))
                continue
            pts = sample_circle(cx, cy, r, resolution_mm=resolution)
        elif typ == 'ellipse':
            cx, cy, rx, ry = val
            pts = sample_ellipse(cx, cy, rx, ry, resolution_mm=resolution)
        else:
            pts = []
        if not pts:
            continue
        if simplify > 0.0:
            pts = simplify_points(pts, simplify)
        tpts = transform_points(pts, scale, tx, ty, flip_y=flip_y)
        if tpts:
            xs = [p[0] for p in tpts]
            ys = [p[1] for p in tpts]
            minx_t, maxx_t = min(xs), max(xs)
            miny_t, maxy_t = min(ys), max(ys)
            if trans_minx is None:
                trans_minx, trans_miny, trans_maxx, trans_maxy = minx_t, miny_t, maxx_t, maxy_t
            else:
                trans_minx = min(trans_minx, minx_t)
                trans_miny = min(trans_miny, miny_t)
                trans_maxx = max(trans_maxx, maxx_t)
                trans_maxy = max(trans_maxy, maxy_t)
            if minx_t < 0 or miny_t < 0 or maxx_t > page_w_mm or maxy_t > page_h_mm:
                page_warning = True
        sx, sy = tpts[0]
        out_lines.append(f'G0 X{sx:.3f} Y{sy:.3f} F{rapid_feed}')
        out_lines.append(f'G1 Z{pen_down:.3f} F{rapid_feed} ; pen down')
        if len(tpts) > 1:
            out_lines.extend(points_to_gcode(tpts[1:], feed))
        out_lines.append(f'G1 Z{pen_up:.3f} F{rapid_feed} ; pen up')

    out_lines.append('G0 X0 Y0 F{:.0f} ; return to origin'.format(rapid_feed))

    if page_warning:
        print('WARNING: some drawing coordinates fall outside the page bounds.', file=sys.stderr)
        print(f'  Page size: {page_w_mm:.3f} x {page_h_mm:.3f} mm', file=sys.stderr)
        print(f'  Bounds: {trans_minx:.3f},{trans_miny:.3f} to {trans_maxx:.3f},{trans_maxy:.3f}', file=sys.stderr)

    if write_output:
        out_path = output if output else svgfile.rsplit('.',1)[0] + '.gcode'
        with open(out_path, 'w') as f:
            f.write('\n'.join(out_lines))
        return out_path

    return out_lines


def main():
    parser = argparse.ArgumentParser(description='Convert SVG to simple plotting G-code')
    parser.add_argument('svgfile')
    parser.add_argument('-o', '--output', help='Output gcode file', default=None)
    parser.add_argument('--page', help='Page size WIDTHxHEIGHT (default 8.5x11 inches)', default='8.5x11')
    parser.add_argument('--units', choices=['in','mm'], default='in', help='Units of page size input')
    parser.add_argument('--resolution', type=float, default=0.5, help='Sampling resolution in mm')
    parser.add_argument('--pen-up', type=float, default=5.0, help='Pen up Z (mm)')
    parser.add_argument('--pen-down', type=float, default=0.0, help='Pen down Z (mm)')
    parser.add_argument('--feed', type=float, default=1500.0, help='Feed rate for drawing moves (mm/min)')
    parser.add_argument('--rapid-feed', type=float, default=3000.0, help='Feed rate for rapid moves (mm/min)')
    parser.add_argument('--no-scale', action='store_true', help='Do not scale the SVG to fit the page')
    parser.add_argument('--no-offset', action='store_true', help='Do not translate the SVG; keep raw coordinates')
    parser.add_argument('--arc-circles', action='store_true', help='Use G2/G3 arc moves for circles instead of many short line segments')
    parser.add_argument('--arc-paths', action='store_true', help='Convert SVG path arc commands (A) into G2/G3 moves')
    parser.add_argument('--simplify', type=float, default=0.0, help='Simplify sampled points with the given tolerance in mm')
    args = parser.parse_args()

    try:
        out_path = convert_svg_to_gcode(
            args.svgfile,
            output=args.output,
            page=args.page,
            units=args.units,
            resolution=args.resolution,
            pen_up=args.pen_up,
            pen_down=args.pen_down,
            feed=args.feed,
            rapid_feed=args.rapid_feed,
            no_scale=args.no_scale,
            no_offset=args.no_offset,
            arc_circles=args.arc_circles,
            arc_paths=args.arc_paths,
            simplify=args.simplify,
        )
        print(f'Wrote {out_path}')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
if __name__ == '__main__':
    main()
