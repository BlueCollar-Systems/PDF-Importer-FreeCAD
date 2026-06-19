import pandas as pd
import math
import os
import re
from fractions import Fraction

# --- CONFIGURATION ---
CSV_FILENAME = 'Structural Steel-shapes-database-v16.0.csv'
BASE_OUTPUT_DIR = 'Structural Steel_SVG_Blueprints_v15'

# Visualization Settings
STROKE_WIDTH = 0.04    
TEXT_SIZE = 0.5
LABEL_SIZE = 0.8
LINE_SPACING = 0.8
SLOPE_STD = 2.0 / 12.0 # 16.67% Slope

# --- FORMATTING ---

def format_structural(val):
    if val is None: return "-"
    try: f = float(val)
    except (ValueError, TypeError): return str(val)
    if f == 0: return "0"
    step = 1.0 / 16.0
    nearest = round(f / step) * step
    frac = Fraction(nearest).limit_denominator(16)
    if frac.denominator == 1: return f'{int(frac)}"'
    whole = int(frac)
    num = frac.numerator - (whole * frac.denominator)
    return f'{whole}-{num}/{frac.denominator}"' if whole > 0 else f'{num}/{frac.denominator}"'

def format_decimal(val):
    if val is None: return "-"
    try: f = float(val)
    except (ValueError, TypeError): return str(val)
    return f"{f:.4f}\""

def clean_float(val):
    if pd.isna(val): return 0.0
    s = str(val).replace('"', '').strip()
    if s in ['-', 'â€“', '']: return 0.0
    try:
        if '/' in s and '-' not in s:
            n, d = s.split('/')
            return float(n)/float(d)
        return float(s)
    except (ValueError, TypeError, ZeroDivisionError): return 0.0

def get_val(row, keys):
    for k in keys:
        if k in row:
            v = clean_float(row[k])
            if v > 0: return v
    return 0.0

def get_slope(row):
    for c in row.index:
        if 'tan' in c.lower() or 'slope' in c.lower():
            return clean_float(row[c])
    return 0.0

# --- PATH ENGINE ---

class PathBuilder:
    def __init__(self):
        self.cmds = []
        self.cur_x = 0; self.cur_y = 0
        self.min_x = float('inf'); self.max_x = float('-inf')
        self.min_y = float('inf'); self.max_y = float('-inf')
        
    def _update_bounds(self, x, y):
        if x < self.min_x: self.min_x = x
        if x > self.max_x: self.max_x = x
        if y < self.min_y: self.min_y = y
        if y > self.max_y: self.max_y = y
    
    def M(self, x, y):
        self.cmds.append(f"M {x:.4f},{-y:.4f}")
        self.cur_x, self.cur_y = x, y
        self._update_bounds(x, y)
        
    def L(self, x, y):
        self.cmds.append(f"L {x:.4f},{-y:.4f}")
        self.cur_x, self.cur_y = x, y
        self._update_bounds(x, y)
        
    def ArcCorner(self, cx, cy, r, sa, ea):
        # HSS Convex Corner Logic
        sx = cx + r*math.cos(sa); sy = cy + r*math.sin(sa)
        ex = cx + r*math.cos(ea); ey = cy + r*math.sin(ea)
        
        if abs(self.cur_x - sx) > 0.001: self.L(sx, sy)
        
        # Sweep Logic for Convex Corners:
        # We want the "bulge out".
        # In Cartesian, Counter-Clockwise (CCW) is positive angle.
        # In SVG (Y-Flip), CCW traversal requires a specific sweep.
        # For HSS Outer, we traverse CCW. We want Convex.
        # Trial and Error fix: Sweep 0 worked for inner (concave). Sweep 1 should work for outer?
        # User reported "inverted". So if 1 was inverted, we use 0? Or vice versa.
        # Let's try explicit logic: Start -> End.
        # Top-Right: 90deg -> 0deg. (Down). Sweep 0.
        self.cmds.append(f"A {r:.4f} {r:.4f} 0 0 0 {ex:.4f},{-ey:.4f}")
        self.cur_x, self.cur_y = ex, ey
        self._update_bounds(ex, ey)

    def Fillet(self, cx, cy, r, sa, ea):
        # Concave Fillets (I-beams)
        if r < 0.001: return
        sx = cx + r*math.cos(sa); sy = cy + r*math.sin(sa)
        ex = cx + r*math.cos(ea); ey = cy + r*math.sin(ea)
        if abs(self.cur_x - sx) > 0.001: self.L(sx, sy)
        # Concave fillets usually use sweep 0 for standard winding
        self.cmds.append(f"A {r:.4f} {r:.4f} 0 0 0 {ex:.4f},{-ey:.4f}")
        self.cur_x, self.cur_y = ex, ey
        self._update_bounds(ex, ey)

    def Close(self): self.cmds.append("Z")
    def D(self): return " ".join(self.cmds)

def solve_tan_r(k_y, slope, web_x, tip_x, tip_y):
    try:
        c_val = tip_y - slope * tip_x
        y_web = slope * web_x + c_val
        num = y_web - k_y
        den = math.sqrt(slope*slope + 1.0) - slope
        if abs(den) < 1e-6: return 0.0
        r = num / den
        return max(0.0, min(r, 20.0))
    except (ValueError, ZeroDivisionError): return 0.0

# --- SHAPE GENERATORS ---

def gen_i_shape(row, tapered=False):
    d = get_val(row, ['d']); bf = get_val(row, ['bf'])
    tf = get_val(row, ['tf']); tw = get_val(row, ['tw'])
    k = get_val(row, ['kdes', 'kdet']) or tf
    slope = SLOPE_STD if tapered else 0.0
    r = 0.0; t_toe = tf; t_web_s = tf
    
    if tapered:
        proj = (bf - tw)/2.0
        t_toe = max(tf - (proj * slope / 2.0), 0.05)
        r = solve_tan_r(d/2 - k, slope, tw/2, bf/2, d/2 - t_toe)
        t_web_s = tf + (proj * slope / 2.0)
    else: r = max(k - tf, 0)

    p = PathBuilder()
    
    # Q1 Top Right
    p.M(0, d/2); p.L(bf/2, d/2); p.L(bf/2, d/2 - t_toe)
    if r > 0.001:
        if tapered: p.Fillet(tw/2+r, d/2-k, r, math.pi/2+math.atan(slope), math.pi)
        else: p.Fillet(tw/2+r, d/2-tf-r, r, math.pi/2, math.pi)
    else: p.L(tw/2, d/2 - (t_web_s if tapered else tf))
    p.L(tw/2, -d/2+k)
    
    # Q4 Bot Right
    if r > 0.001:
        if tapered: p.Fillet(tw/2+r, -d/2+k, r, math.pi, 1.5*math.pi-math.atan(slope))
        else: p.Fillet(tw/2+r, -d/2+tf+r, r, math.pi, 1.5*math.pi)
    else: p.L(tw/2, -d/2 + (t_web_s if tapered else tf))
    p.L(bf/2, -d/2+t_toe); p.L(bf/2, -d/2); p.L(0, -d/2)
    
    # Q3 Bot Left
    p.L(-bf/2, -d/2); p.L(-bf/2, -d/2+t_toe)
    if r > 0.001:
        if tapered: p.Fillet(-tw/2-r, -d/2+k, r, 1.5*math.pi+math.atan(slope), 2*math.pi)
        else: p.Fillet(-tw/2-r, -d/2+tf+r, r, 1.5*math.pi, 2*math.pi)
    else: p.L(-tw/2, -d/2 + (t_web_s if tapered else tf))
    p.L(-tw/2, d/2-k)
    
    # Q2 Top Left (Fixed "Shooting Point")
    if r > 0.001:
        # Correct Angles: Start at Web (0 radians), End at Flange (pi/2 - alpha)
        if tapered: p.Fillet(-tw/2-r, d/2-k, r, 0, 0.5*math.pi-math.atan(slope))
        else: p.Fillet(-tw/2-r, d/2-tf-r, r, 0, 0.5*math.pi)
    else: p.L(-tw/2, d/2 - (t_web_s if tapered else tf))
    
    p.L(-bf/2, d/2-t_toe); p.L(-bf/2, d/2); p.L(0, d/2); p.Close()
    return p

def gen_channel(row, tapered=False):
    d = get_val(row, ['d']); bf = get_val(row, ['bf'])
    tf = get_val(row, ['tf']); tw = get_val(row, ['tw'])
    k = get_val(row, ['kdes', 'kdet']) or tf
    slope = SLOPE_STD if tapered else 0.0
    r = 0.0; t_toe = tf; t_web_s = tf
    if tapered:
        proj = bf - tw
        t_toe = max(tf - (proj * slope / 2.0), 0.05)
        r = solve_tan_r(d/2 - k, slope, tw, bf, d/2 - t_toe)
        t_web_s = tf + (proj * slope / 2.0)
    else: r = max(k - tf, 0)

    p = PathBuilder()
    p.M(0, d/2); p.L(bf, d/2); p.L(bf, d/2 - t_toe)
    if r>0.001:
        if tapered: p.Fillet(tw+r, d/2-k, r, math.pi/2+math.atan(slope), math.pi)
        else: p.Fillet(tw+r, d/2-tf-r, r, math.pi/2, math.pi)
    else: p.L(tw, d/2 - (t_web_s if tapered else tf))
    p.L(tw, -d/2+k)
    if r>0.001:
        if tapered: p.Fillet(tw+r, -d/2+k, r, math.pi, 1.5*math.pi-math.atan(slope))
        else: p.Fillet(tw+r, -d/2+tf+r, r, math.pi, 1.5*math.pi)
    else: p.L(tw, -d/2 + (t_web_s if tapered else tf))
    p.L(bf, -d/2+t_toe); p.L(bf, -d/2); p.L(0, -d/2); p.Close()
    return p

def gen_hss_rect(row):
    h, b = get_val(row, ['Ht', 'h']), get_val(row, ['B', 'b'])
    t = get_val(row, ['tdes', 'tnom', 't'])
    ro = 2.0*t; ri = max(0, ro-t)
    p = PathBuilder()
    # Outer (CCW)
    p.M(0, h/2); p.L(b/2-ro, h/2)
    p.ArcCorner(b/2-ro, h/2-ro, ro, math.pi/2, 0)
    p.L(b/2, -h/2+ro)
    p.ArcCorner(b/2-ro, -h/2+ro, ro, 0, -math.pi/2)
    p.L(-b/2+ro, -h/2)
    p.ArcCorner(-b/2+ro, -h/2+ro, ro, 1.5*math.pi, math.pi)
    p.L(-b/2, h/2-ro)
    p.ArcCorner(-b/2+ro, h/2-ro, ro, math.pi, 0.5*math.pi)
    p.Close()
    # Inner (Hole) (CW to make hole)
    bi, hi = b-2*t, h-2*t
    p.M(0, hi/2); p.L(bi/2-ri, hi/2)
    p.ArcCorner(bi/2-ri, hi/2-ri, ri, math.pi/2, 0)
    p.L(bi/2, -hi/2+ri); p.ArcCorner(bi/2-ri, -hi/2+ri, ri, 0, -math.pi/2)
    p.L(-bi/2+ri, -hi/2); p.ArcCorner(-bi/2+ri, -hi/2+ri, ri, 1.5*math.pi, math.pi)
    p.L(-bi/2, hi/2-ri); p.ArcCorner(-bi/2+ri, hi/2-ri, ri, math.pi, 0.5*math.pi)
    p.Close()
    return p

def gen_2l(row, lbl):
    # Parse gap from label (e.g., 2L4x4x1/4x3/8 -> gap 3/8)
    # Default gap 3/8 if not found
    gap = 0.375 
    m = re.search(r'X(\d+/\d+|\d+\.?\d*)$', lbl) # Find last dimension
    if m:
        try: gap = clean_float(m.group(1))
        except (ValueError, TypeError): pass
        
    d = get_val(row, ['d']); b = get_val(row, ['b'])
    t = get_val(row, ['t']); k = get_val(row, ['kdes', 'kdet']) or t
    r = max(k-t, 0)
    
    # 2L is Back-to-Back. ] [
    # Right Angle (Normal L, shifted Right)
    p = PathBuilder()
    off = gap / 2.0
    
    # Right Angle (Standard L at +offset)
    p.M(off+b, 0); p.L(off, 0); p.L(off, d); p.L(off+t, d)
    if r > 0.001: 
        # Fillet center at (off+t+r, t+r)
        p.Fillet(off+t+r, t+r, r, math.pi, 1.5*math.pi)
    else: p.L(off+t, t)
    p.L(off+b, t); p.Close()
    
    # Left Angle (Standard L, Flipped X, Shifted Left)
    # Heel at -off. Toe at -off-b.
    p.M(-off-b, 0); p.L(-off, 0); p.L(-off, d); p.L(-off-t, d)
    if r > 0.001:
        # Fillet Center (-off-t-r, t+r)
        # Angles: Start 0 (Right), End 1.5pi (Down). Sweep?
        # Standard: pi -> 1.5pi. Flipped X: 0 -> 1.5pi (270).
        # We need arc from Right to Down? No.
        # Flipped L:
        # Leg is Up. Inside is Left.
        # Inner corner is at (-off-t, t).
        # Fillet center (-off-t-r, t+r).
        # Start at Vertical leg inner face: (-off-t, ...). Angle 0 (Right).
        # End at Horiz leg inner face: (..., t). Angle 1.5pi (-90).
        # 0 -> -90. Sweep 0 (Negative).
        p.Fillet(-off-t-r, t+r, r, 0, 1.5*math.pi)
    else: p.L(-off-t, t)
    p.L(-off-b, t); p.Close()
    
    return p, 2*b + gap # Return path and full width

# (Gen Angle/Round/Tee same as v14, re-included)
def gen_tee(row, tapered=False):
    d, bf = get_val(row, ['d']), get_val(row, ['bf'])
    tf, tw = get_val(row, ['tf']), get_val(row, ['tw'])
    k = get_val(row, ['kdes']) or tf
    slope = SLOPE_STD if tapered else 0.0
    r=0.0; t_toe=tf; t_web_s=tf
    if tapered:
        proj = (bf-tw)/2.0
        t_toe = max(tf - (proj*slope/2.0), 0.05)
        r = solve_tan_r(d-k, slope, tw/2, bf/2, d-t_toe)
        t_web_s = tf + (proj*slope/2.0)
    else: r = max(k-tf, 0)
    p=PathBuilder(); p.M(0,d); p.L(bf/2,d); p.L(bf/2, d-t_toe)
    if r>0.001:
        if tapered: p.Fillet(tw/2+r, d-k, r, math.pi/2+math.atan(slope), math.pi)
        else: p.Fillet(tw/2+r, d-tf-r, r, math.pi/2, math.pi)
    else: p.L(tw/2, d-(t_web_s if tapered else tf))
    p.L(tw/2, 0); p.L(-tw/2, 0); p.L(-tw/2, d-k)
    if r>0.001:
        if tapered: p.Fillet(-tw/2-r, d-k, r, 0, 0.5*math.pi-math.atan(slope))
        else: p.Fillet(-tw/2-r, d-tf-r, r, 0, 0.5*math.pi)
    else: p.L(-tw/2, d-(t_web_s if tapered else tf))
    p.L(-bf/2, d-t_toe); p.L(-bf/2, d); p.Close()
    return p

def gen_angle(row):
    d, b = get_val(row, ['d']), get_val(row, ['b'])
    t, k = get_val(row, ['t']), get_val(row, ['kdes'])
    if k<=0: k = t + 0.125
    r = max(k-t, 0); p = PathBuilder()
    p.M(b,0); p.L(0,0); p.L(0,d); p.L(t,d)
    if r > 0.001: p.Fillet(t+r, t+r, r, math.pi, 1.5*math.pi)
    else: p.L(t,t)
    p.L(b,t); p.Close()
    return p

def gen_round(row):
    od = get_val(row, ['OD']); t = get_val(row, ['tdes', 'tnom', 't'])
    r = od/2; ri = r-t
    p = PathBuilder()
    p.M(-r,0); p.cmds.append(f"A {r:.4f} {r:.4f} 0 0 1 {r:.4f},0 A {r:.4f} {r:.4f} 0 0 1 {-r:.4f},0 Z")
    p._update_bounds(-r, -r); p._update_bounds(r, r)
    p.M(-ri,0); p.cmds.append(f"A {ri:.4f} {ri:.4f} 0 0 0 {ri:.4f},0 A {ri:.4f} {ri:.4f} 0 0 0 {-ri:.4f},0 Z")
    return p

# --- MAIN ---

def run():
    print("ðŸš€ GENERATING v15 BLUEPRINTS")
    if not os.path.exists(BASE_OUTPUT_DIR): os.makedirs(BASE_OUTPUT_DIR)

    files = [f for f in os.listdir('.') if f.endswith('.csv') and 'Structural Steel' in f.lower()]
    df = pd.read_csv(files[0], low_memory=False)
    df.columns = [c.strip() for c in df.columns]

    total = 0; generated = 0; failed = 0
    for _, row in df.iterrows():
        lbl = str(row.get('Structural Steel_Manual_Label', '')).strip()
        fam = str(row.get('Type', '')).strip().upper()
        if not lbl or lbl == 'nan': continue
        total += 1

        sub = fam
        if 'HSS' in fam: sub = 'HSS_Round' if get_val(row, ['OD']) > 0 else 'HSS_Rect'
        if fam == 'PIPE': sub = 'HSS_Round'
        if fam == '2L': sub = 'Double_Angle'
        
        path = None
        d = get_val(row, ['d', 'd (in)'])
        bf = get_val(row, ['bf', 'bf (in)', 'b', 'b (in)'])
        
        try:
            specs = []
            if fam in ['W', 'M', 'S', 'HP']:
                is_tapered = (fam == 'S')
                path = gen_i_shape(row, tapered=is_tapered)
                tf, tw = get_val(row, ['tf']), get_val(row, ['tw'])
                specs = [("Depth", format_structural(d)), ("Width", format_structural(bf)), ("Half Web", format_structural(tw/2)), ("Web Thk", format_structural(tw)), ("Flange Thk", format_structural(tf))]
            elif fam in ['C', 'MC']:
                path = gen_channel(row, tapered=False) # MC Parallel
                tf, tw = get_val(row, ['tf']), get_val(row, ['tw'])
                specs = [("Depth", format_structural(d)), ("Width", format_structural(bf)), ("Half Web", format_structural(tw/2)), ("Web Thk", format_structural(tw)), ("Flange Thk", format_structural(tf))]
            elif fam in ['WT', 'MT', 'ST']:
                is_tapered = (fam == 'ST')
                path = gen_tee(row, tapered=is_tapered)
                tf, tw = get_val(row, ['tf']), get_val(row, ['tw'])
                specs = [("Depth", format_structural(d)), ("Width", format_structural(bf)), ("Stem", format_structural(tw)), ("Flange", format_structural(tf))]
            elif fam == 'L':
                path = gen_angle(row)
                t = get_val(row, ['t'])
                specs = [("Depth", format_structural(d)), ("Width", format_structural(bf)), ("Wall Thk", format_structural(t))]
            elif fam == '2L':
                path, full_w = gen_2l(row, lbl)
                specs = [("Depth", format_structural(d)), ("Single Width", format_structural(bf)), ("Total Width", format_structural(full_w))]
                bf = full_w # Update width for viewbox
            elif sub == 'HSS_Rect':
                ht, b_dim = get_val(row, ['Ht']), get_val(row, ['B'])
                thk = get_val(row, ['tdes', 'tnom'])
                path = gen_hss_rect(row)
                specs = [("Height", format_decimal(ht)), ("Width", format_decimal(b_dim)), ("Wall", format_decimal(thk))]
                d, bf = ht, b_dim
            elif sub == 'HSS_Round':
                od = get_val(row, ['OD'])
                thk = get_val(row, ['tdes', 'tnom'])
                id_val = od - 2*thk
                path = gen_round(row)
                specs = [("OD", format_decimal(od)), ("ID", format_decimal(id_val)), ("Wall", format_decimal(thk))]
                d, bf = od, od

            if path and d > 0:
                clean_lbl = re.sub(r'[^a-zA-Z0-9\._\-]', '_', lbl)
                out_dir = os.path.join(BASE_OUTPUT_DIR, 'Pipe' if fam == 'PIPE' else sub)
                if not os.path.exists(out_dir): os.makedirs(out_dir)
                
                w_box, h_box = bf, d
                txt_x = w_box/2 + 1.0; txt_y = -h_box/2 + 1.5
                vb_w = w_box + 8.0; vb_h = max(h_box, len(specs)*LINE_SPACING) + 4.0
                vb_x = -w_box/2 - 2.0; vb_y = -h_box/2 - 3.0
                
                with open(os.path.join(out_dir, f"{clean_lbl}.svg"), 'w') as f:
                    f.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb_x} {vb_y} {vb_w} {vb_h}" width="{vb_w}in" height="{vb_h}in">')
                    f.write(f'<style>.shape{{fill:#E0E0E0;stroke:black;stroke-width:{STROKE_WIDTH};fill-rule:evenodd;}} .txt{{font-family:Arial;font-size:{TEXT_SIZE}px;fill:black;font-weight:bold;}} .header{{font-family:Arial;font-size:{LABEL_SIZE}px;fill:black;font-weight:bold;}} .lbl{{fill:#555;font-weight:normal;}}</style>')
                    
                    f.write(f'<text x="0" y="{vb_y + 1.5}" text-anchor="middle" class="header">{lbl}</text>')
                    
                    f.write(f'<path class="shape" d="{path.D()}" />')
                    for i, (l, v) in enumerate(specs):
                        f.write(f'<text x="{txt_x}" y="{txt_y+i*LINE_SPACING}" class="txt"><tspan class="lbl">{l}: </tspan>{v}</text>')
                    f.write('</svg>')
                    generated += 1

        except Exception as e:
            failed += 1
            print(f"  [ERROR] Shape '{lbl}': {e}")

    print(f"âœ… COMPLETED. Generated {generated} of {total} shapes, {failed} failures. Output in {BASE_OUTPUT_DIR}")

if __name__ == '__main__':
    run()
