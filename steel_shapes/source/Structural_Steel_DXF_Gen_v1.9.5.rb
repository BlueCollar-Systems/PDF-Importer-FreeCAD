# AISC 2D DXF Generator v1.9.5 (Syntax Audited)
#
# CHANGES v1.9.5:
# 1. SYNTAX AUDIT: Fixed "no .<digit> floating literal" error.
#    - Ensured all decimals have leading zeros (e.g., 0.5 instead of .5).
# 2. COMPATIBILITY: Verified for SketchUp Ruby 2.7+.
# 3. CORE: Uses the "Universal" R12 writer (v1.9.4) for max compatibility.
#
# USAGE: Load in SketchUp Ruby Console -> AISC_DXF_Final.run

require 'sketchup.rb'
require 'csv'

module AISC_DXF_Final
  
  # --- CONFIG ---
  SPACING_X       = 50.0
  SPACING_Y       = 50.0
  ROW_LIMIT       = 20
  TEXT_HEIGHT     = 1.5
  
  MAX_CHORD       = 0.03 
  MIN_STEPS       = 12
  MAX_STEPS       = 96
  
  STD_SLOPE       = 2.0 / 12.0 # Standard 2:12
  MC_SLOPE        = 0.0        # Default MC to Parallel

  SUPPORTED_FAMILIES = %w[
    W M HP S C MC L WT MT ST
    HSS_Rect HSS_Round PIPE 2L
  ].freeze

  # --- HELPER: Prevent Duplicate Vertices ---
  def self.push_pt(pts, x, y)
    return if pts.any? && (pts.last[0] - x).abs < 1.0e-6 && (pts.last[1] - y).abs < 1.0e-6
    pts << [x, y]
  end

  # --- HELPER: Normalize Points to SW Origin (0,0) ---
  def self.normalize_sw!(point_sets)
    return if point_sets.empty?
    all_pts = point_sets.flatten(1)
    return if all_pts.empty?
    
    min_x = all_pts.map { |p| p[0] }.min
    min_y = all_pts.map { |p| p[1] }.min
    
    point_sets.each do |poly|
      poly.map! { |p| [p[0] - min_x, p[1] - min_y] }
    end
  end

  # --- DXF WRITER CLASS (R12 SIMPLIFIED) ---
  class DXFDoc
    def initialize(path)
      @f = File.open(path, "w")
      write_header
      write_tables
      write_entities_start
    end

    def tag(code, value)
      @f.puts code
      @f.puts value
    end

    def write_header
      tag 0, "SECTION"
      tag 2, "HEADER"
      tag 9, "$ACADVER"
      tag 1, "AC1009"  # R12
      tag 9, "$INSUNITS"
      tag 70, 1        # Inches
      tag 0, "ENDSEC"
    end

    def write_tables
      tag 0, "SECTION"
      tag 2, "TABLES"
      
      tag 0, "TABLE"
      tag 2, "LAYER"
      tag 70, 2
        tag 0, "LAYER"
        tag 2, "AISC_STEEL"
        tag 70, 0
        tag 62, 7
        tag 6, "CONTINUOUS"
        
        tag 0, "LAYER"
        tag 2, "AISC_LABELS"
        tag 70, 0
        tag 62, 3
        tag 6, "CONTINUOUS"
      tag 0, "ENDTAB"
      
      tag 0, "ENDSEC"
    end

    def write_entities_start
      tag 0, "SECTION"
      tag 2, "ENTITIES"
    end

    def close
      tag 0, "ENDSEC"
      tag 0, "EOF"
      @f.close
    end

    def add_circle(cx, cy, radius, layer="AISC_STEEL")
      tag 0, "CIRCLE"
      tag 8, layer
      tag 10, cx
      tag 20, cy
      tag 30, 0.0
      tag 40, radius
    end

    def add_polyline(points, cx, cy, layer="AISC_STEEL")
      return if points.empty?
      tag 0, "POLYLINE"
      tag 8, layer
      tag 66, 1
      tag 70, 1
      tag 10, 0.0
      tag 20, 0.0
      tag 30, 0.0
      
      points.each do |pt|
        tag 0, "VERTEX"
        tag 8, layer
        tag 10, pt[0] + cx
        tag 20, pt[1] + cy
        tag 30, 0.0
      end
      
      tag 0, "SEQEND"
    end

    def add_text(text, x, y, height, layer="AISC_LABELS")
      tag 0, "TEXT"
      tag 8, layer
      tag 10, x
      tag 20, y
      tag 30, 0.0
      tag 40, height
      tag 1, text
    end
  end

  # --- MATH HELPERS ---

  def self.get_val(row, map, key)
    k_raw = key.to_s
    i = map[k_raw]
    unless i
      found_key = map.keys.find { |mk| mk.to_s.upcase == k_raw.upcase }
      i = map[found_key] if found_key
    end
    unless i
      ku = k_raw.upcase
      if ku == 'HT' then i = map['Ht'] || map['H'] || map['h']
      elsif ku == 'OD' then i = map['OD'] || map['Od'] || map['D'] || map['d']
      elsif ku == 'ID' then i = map['ID_DEC'] || map['ID'] || map['Id']
      end
    end
    return "" if i.nil?
    row[i].to_s.strip.gsub('"', '')
  end

  def self.get_f(row, map, key)
    v = get_val(row, map, key)
    return 0.0 if v.empty?
    clean = v.gsub(/[^\d\.\/\-]/, '')
    return 0.0 if clean.empty?
    parse_dim(clean) || 0.0
  end

  def self.parse_dim(s)
    return nil if s.nil?
    str = s.to_s.strip
    return nil if str.empty? || str == "–" || str == "-"
    str = str.gsub('"','')
    if str.include?('-') && str =~ /^\d+-\d+\/\d+$/
      a,b = str.split('-',2)
      return a.to_f + parse_dim(b).to_f
    end
    if str.include?('/') && str =~ /^\d+\/\d+$/
      n,d = str.split('/',2)
      return n.to_f / d.to_f
    end
    Float(str) rescue nil
  end

  def self.thickness_nominal(row, map)
    tnom = get_f(row, map, "tnom")
    return tnom if tnom > 0.0
    t = get_f(row, map, "t")
    return t if t > 0.0
    tdes = get_f(row, map, "tdes")
    return tdes if tdes > 0.0
    0.0
  end

  def self.get_shape_slope(fam)
    case fam
    when "S", "C", "ST"
      return STD_SLOPE
    when "MC"
      return MC_SLOPE
    else
      return 0.0
    end
  end

  def self.infer_family(raw_type, label, row, map)
    rt = raw_type.to_s.strip.upcase
    lbl = label.to_s.strip.upcase
    clean_lbl = lbl.gsub(/\s+/, '')
    return 'MT' if rt == 'MT' || clean_lbl =~ /^MT\d/
    return 'WT' if rt == 'WT' || clean_lbl =~ /^WT\d/
    return 'ST' if rt == 'ST' || clean_lbl =~ /^ST\d/
    return '2L' if rt == '2L' || clean_lbl =~ /^2L/
    return 'L'  if rt == 'L'  || clean_lbl =~ /^L\d/
    if rt.include?('HSS') || lbl.start_with?('HSS') || rt == 'PIPE' || lbl.start_with?('PIPE')
      return 'PIPE' if rt == 'PIPE'
      x_count = clean_lbl.count('X')
      return 'HSS_Rect' if x_count >= 2
      b_val = get_f(row, map, "B"); h_val = get_f(row, map, "Ht")
      return 'HSS_Rect' if b_val > 0 && h_val > 0
      return 'HSS_Round' if lbl.start_with?('HSS')
      return 'PIPE'
    end
    return 'W' if rt == 'W' || clean_lbl =~ /^W\d/; return 'M' if rt == 'M' || clean_lbl =~ /^M\d/
    return 'HP' if rt == 'HP' || clean_lbl =~ /^HP\d/; return 'S' if rt == 'S' || clean_lbl =~ /^S\d/
    return 'C' if rt == 'C' || clean_lbl =~ /^C\d/; return 'MC' if rt == 'MC' || clean_lbl =~ /^MC\d/
    return "Unknown_#{rt}"
  end

  # --- GEOMETRY GENERATORS ---

  def self.arc_steps(r, span_rad)
    arc_len = (r * span_rad.abs)
    steps = (arc_len / MAX_CHORD).ceil
    steps = MIN_STEPS if steps < MIN_STEPS
    steps = MAX_STEPS if steps > MAX_STEPS
    steps
  end

  def self.add_fillet(pts, cx, cy, r, sa_rad, ea_rad)
    return if r <= 0
    span = (ea_rad - sa_rad)
    steps = arc_steps(r, span)
    (0..steps).each do |i|
      a = sa_rad + span * (i.to_f / steps.to_f)
      push_pt(pts, cx + r * Math.cos(a), cy + r * Math.sin(a))
    end
  end

  def self.solve_tangent_radius(k_y, slope, web_x, tip_x, tip_y)
    c_val = tip_y - slope * tip_x
    y_line_at_web = slope * web_x + c_val
    numer = y_line_at_web - k_y
    denom = Math.sqrt(slope*slope + 1.0) - slope
    return 0.0 if denom.abs < 1.0e-6
    return numer / denom
  end

  def self.rounded_rect_points(w, h, r)
    w = w.to_f; h = h.to_f; r = r.to_f
    x = w / 2.0; y = h / 2.0
    r = 0.0 if r < 1.0e-6
    r = [r, x, y].min
    if r <= 0.0
      return [[ x,  y], [-x,  y], [-x, -y], [ x, -y]]
    end
    pts = []
    push_pt(pts, x - r, y)
    add_fillet(pts, x-r, y-r, r, Math::PI/2, 0)
    push_pt(pts, x, -y + r)
    add_fillet(pts, x-r, -y+r, r, 0, -Math::PI/2)
    push_pt(pts, -x + r, -y)
    add_fillet(pts, -x+r, -y+r, r, 1.5*Math::PI, Math::PI)
    push_pt(pts, -x, y - r)
    add_fillet(pts, -x+r, y-r, r, Math::PI, Math::PI/2)
    pts
  end

  # -- SHAPE DEFINITIONS (v1.7.4 Logic) --

  def self.calc_i_parallel(row, map)
    d, bf, tf, tw = get_f(row, map, "d"), get_f(row, map, "bf"), get_f(row, map, "tf"), get_f(row, map, "tw")
    k = get_f(row, map, "kdes"); k = tf if k <= 0
    return [] if d <= 0 || bf <= 0 
    
    rad = [(k - tf), 0.0].max
    
    pts = []
    push_pt(pts, 0, d/2); push_pt(pts, bf/2, d/2); push_pt(pts, bf/2, d/2 - tf)
    
    if rad > 0
      add_fillet(pts, tw/2 + rad, d/2 - tf - rad, rad, Math::PI/2, Math::PI)
      push_pt(pts, tw/2, d/2 - tf - rad)
    else
      push_pt(pts, tw/2, d/2 - tf)
    end
    
    push_pt(pts, tw/2, -d/2 + k)
    
    if rad > 0 
      add_fillet(pts, tw/2 + rad, -d/2 + tf + rad, rad, Math::PI, 1.5*Math::PI) 
    else
      push_pt(pts, tw/2, -d/2 + tf)
    end
    
    push_pt(pts, bf/2, -d/2 + tf); push_pt(pts, bf/2, -d/2); push_pt(pts, 0, -d/2)
    
    right = pts
    left = right[1..-2].reverse.map { |p| [-p[0], p[1]] }
    right + left
  end

  def self.calc_i_tapered(row, map, slope, fam)
    d, bf, tf, tw = get_f(row, map, "d"), get_f(row, map, "bf"), get_f(row, map, "tf"), get_f(row, map, "tw")
    k = get_f(row, map, "kdes"); k = tf if k <= 0
    return [] if d <= 0 || bf <= 0 
    
    proj = (bf - tw) / 2.0
    t_toe = tf - (proj * slope / 2.0)
    t_web = tf + (proj * slope / 2.0)
    t_toe = [[t_toe, 0.05].max, tf].min
    t_web = [[t_web, 0.05].max, (tf + proj * slope)].min
    
    y_tan = d/2 - k
    rad = solve_tangent_radius(y_tan, slope, tw/2.0, bf/2.0, d/2 - t_toe) rescue 0
    rad = [[rad, (k - tf)].min, 0.0].max
    
    pts = []
    push_pt(pts, 0, d/2); push_pt(pts, bf/2, d/2); push_pt(pts, bf/2, d/2 - t_toe)
    
    if rad > 0
      cx = tw/2 + rad; alpha = Math.atan(slope)
      add_fillet(pts, cx, y_tan, rad, (Math::PI/2 + alpha), Math::PI)
      push_pt(pts, tw/2, y_tan) 
      push_pt(pts, tw/2, -d/2 + k)
      cy_bot = -d/2 + k
      add_fillet(pts, cx, cy_bot, rad, Math::PI, (1.5*Math::PI - alpha))
    else
      push_pt(pts, tw/2, d/2 - t_web)
      push_pt(pts, tw/2, -d/2 + t_web)
    end
    
    push_pt(pts, bf/2, -d/2 + t_toe); push_pt(pts, bf/2, -d/2); push_pt(pts, 0, -d/2)
    right = pts
    left = right[1..-2].reverse.map { |p| [-p[0], p[1]] }
    right + left
  end

  def self.calc_channel(row, map, slope, fam)
    d, bf, tf, tw = get_f(row, map, "d"), get_f(row, map, "bf"), get_f(row, map, "tf"), get_f(row, map, "tw")
    k = get_f(row, map, "kdes"); k = tf if k <= 0
    return [] if d <= 0 || bf <= 0 
    
    is_par = (slope.abs < 0.001)
    pts = []
    push_pt(pts, 0, d/2); push_pt(pts, bf, d/2)
    
    if is_par
      rad = [(k - tf), 0.0].max
      push_pt(pts, bf, d/2 - tf)
      if rad > 0
        add_fillet(pts, tw + rad, d/2 - tf - rad, rad, Math::PI/2, Math::PI)
        push_pt(pts, tw, d/2 - tf - rad)
      else
        push_pt(pts, tw, d/2 - tf)
      end
      push_pt(pts, tw, -d/2 + k)
      if rad > 0 
        add_fillet(pts, tw + rad, -d/2 + tf + rad, rad, Math::PI, 1.5*Math::PI)
      else
        push_pt(pts, tw, -d/2 + tf)
      end
      push_pt(pts, bf, -d/2 + tf)
    else
      proj = (bf - tw)
      t_toe = tf - (proj * slope / 2.0)
      t_web = tf + (proj * slope / 2.0)
      t_toe = [[t_toe, 0.05].max, tf].min
      t_web = [[t_web, 0.05].max, (tf + proj * slope)].min

      push_pt(pts, bf, d/2 - t_toe)
      y_tan = d/2 - k
      rad = solve_tangent_radius(y_tan, slope, tw, bf, d/2 - t_toe) rescue 0
      rad = [[rad, (k-tf)*0.75].min, 0.0].max 
      
      if rad > 0
        cx = tw + rad; alpha = Math.atan(slope)
        add_fillet(pts, cx, y_tan, rad, (Math::PI/2 + alpha), Math::PI)
        push_pt(pts, tw, y_tan)
        push_pt(pts, tw, -d/2 + k)
        y_bot_k = -d/2 + k
        add_fillet(pts, cx, y_bot_k, rad, Math::PI, (1.5*Math::PI - alpha))
      else
        push_pt(pts, tw, d/2 - t_web)
        push_pt(pts, tw, -d/2 + t_web)
      end
      push_pt(pts, bf, -d/2 + t_toe)
    end
    push_pt(pts, bf, -d/2); push_pt(pts, 0, -d/2)
    pts
  end

  def self.calc_angle(row, map)
    d = get_f(row, map, "d"); b = get_f(row, map, "b")
    t = get_f(row, map, "t")
    k = get_f(row, map, "kdes"); k = get_f(row, map, "kdet") if k <= 0
    return [] if d <= 0 || b <= 0 
    r = (k > t) ? (k - t) : (t * 0.5); r = 0.0 if r < 0.001
    
    pts = []
    push_pt(pts, b, 0); push_pt(pts, 0, 0); push_pt(pts, 0, d); push_pt(pts, t, d)
    if r > 0
      add_fillet(pts, t + r, t + r, r, Math::PI, 1.5 * Math::PI)
    else
      push_pt(pts, t, t)
    end
    push_pt(pts, b, t)
    pts
  end

  def self.calc_tee(row, map, slope, fam)
    d, bf, tf, tw = get_f(row, map, "d"), get_f(row, map, "bf"), get_f(row, map, "tf"), get_f(row, map, "tw")
    k = get_f(row, map, "kdes"); k = tf if k <= 0
    return [] if d <= 0 || bf <= 0 
    pts = []
    push_pt(pts, 0, d); push_pt(pts, bf/2, d)
    
    if fam == "ST"
      proj = (bf - tw) / 2.0
      t_toe = tf - (proj * slope / 2.0)
      t_web = tf + (proj * slope / 2.0)
      t_toe = [[t_toe, 0.05].max, tf].min
      t_web = [[t_web, 0.05].max, (tf + proj * slope)].min
      
      y_tan = d - k
      rad = solve_tangent_radius(y_tan, slope, tw/2.0, bf/2.0, d - t_toe) rescue 0
      rad = [[rad, (k-tf)*0.9].min, 0.0].max 
      
      push_pt(pts, bf/2, d - t_toe)
      if rad > 0
        cx = tw/2 + rad; alpha = Math.atan(slope)
        add_fillet(pts, cx, y_tan, rad, (Math::PI/2 + alpha), Math::PI)
        push_pt(pts, tw/2, y_tan) 
      else
        push_pt(pts, tw/2, d - t_web)
      end
    else
      rad = [(k - tf), 0.0].max
      push_pt(pts, bf/2, d - tf)
      if rad > 0 
        add_fillet(pts, tw/2 + rad, d - tf - rad, rad, Math::PI/2, Math::PI)
        push_pt(pts, tw/2, d - tf - rad)
      else
        push_pt(pts, tw/2, d - tf)
      end
    end
    push_pt(pts, tw/2, 0); push_pt(pts, 0, 0)
    right = pts
    left = right[1..-2].reverse.map { |p| [-p[0], p[1]] }
    right + left
  end

  def self.calc_hss_rect(row, map)
    b_out = get_f(row, map, "B"); h_out = get_f(row, map, "Ht")
    h_out = get_f(row, map, "H") if h_out <= 0
    t = thickness_nominal(row, map)
    return nil, nil if b_out <= 0 || h_out <= 0
    ro = 2.0 * t
    ro = [ro, [b_out, h_out].min/2.0 - 0.001].min
    outer = rounded_rect_points(b_out, h_out, ro)
    bi = b_out - 2.0*t; hi = h_out - 2.0*t
    return outer, nil if bi <= 0 || hi <= 0
    ri = [ro - t, 0].max
    inner = rounded_rect_points(bi, hi, ri).reverse
    return outer, inner
  end

  # --- MAIN RUN ---

  def self.run
    csv_path = UI.openpanel("Select AISC CSV", "", "CSV Files|*.csv||")
    return unless csv_path
    out_folder = UI.select_directory(title: "Select Output Folder for DXF")
    return unless out_folder

    table = CSV.read(csv_path, headers: true, encoding: 'bom|utf-8')
    headers = table.headers.map { |h| h.to_s.strip.gsub('"','') }
    map = {}
    headers.each_with_index { |h, i| map[h] ||= i }

    rows = []; table.each { |r| rows << r.fields }
    families = {}
    rows.each do |row|
      rt = get_val(row, map, "Type"); lbl = get_val(row, map, "AISC_Manual_Label")
      next if lbl.to_s.strip.empty?
      fam = infer_family(rt, lbl, row, map)
      families[fam] ||= []; families[fam] << { row: row, label: lbl }
    end

    families.keys.sort.each do |fam|
      unless SUPPORTED_FAMILIES.include?(fam)
        next
      end
      
      if fam == "MC" && MC_SLOPE.abs > 0.001
        puts "  [Warning] MC shapes generated with slope=#{MC_SLOPE}. (Assumption)"
      end

      dxf_path = File.join(out_folder, "AISC_#{fam}_2D_v1_9_5.dxf")
      puts "Generating #{dxf_path}..."
      dxf = DXFDoc.new(dxf_path)
      
      x = 0.0; y = 0.0; count = 0
      
      families[fam].each do |item|
        row = item[:row]; lbl = item[:label]
        
        begin
          case fam
          when "W", "M", "HP"
            pts = calc_i_parallel(row, map)
            normalize_sw!([pts])
            dxf.add_polyline(pts, x, y)
          when "S"
            pts = calc_i_tapered(row, map, get_shape_slope("S"), "S")
            normalize_sw!([pts])
            dxf.add_polyline(pts, x, y)
          when "C", "MC"
            slope = get_shape_slope(fam)
            pts = calc_channel(row, map, slope, fam)
            normalize_sw!([pts])
            dxf.add_polyline(pts, x, y)
          when "L"
            pts = calc_angle(row, map)
            normalize_sw!([pts])
            dxf.add_polyline(pts, x, y)
          when "WT", "MT"
            pts = calc_tee(row, map, 0, "WT")
            normalize_sw!([pts])
            dxf.add_polyline(pts, x, y)
          when "ST"
            pts = calc_tee(row, map, get_shape_slope("ST"), "ST")
            normalize_sw!([pts])
            dxf.add_polyline(pts, x, y)
          when "2L"
            sep = 0.0
            if lbl.count("X") == 3
              if lbl =~ /X([0-9\/\-\.]+)$/
                sep_str = $1
                sep = parse_dim(sep_str) || 0.0
              end
            end
            
            pts1 = calc_angle(row, map)
            next if pts1.empty?
            
            pts_left = pts1.map { |p| [-p[0], p[1]] } 
            pts_right = pts1.dup
            pts_left.map! { |p| [p[0] - sep/2.0, p[1]] }
            pts_right.map! { |p| [p[0] + sep/2.0, p[1]] }
            normalize_sw!([pts_left, pts_right])
            dxf.add_polyline(pts_left, x, y)
            dxf.add_polyline(pts_right, x, y) 
          when "HSS_Rect"
            out_pts, in_pts = calc_hss_rect(row, map)
            set = [out_pts]
            set << in_pts if in_pts
            normalize_sw!(set)
            dxf.add_polyline(out_pts, x, y)
            dxf.add_polyline(in_pts, x, y) if in_pts
          when "HSS_Round", "PIPE"
            od = get_f(row, map, "OD")
            if od <= 0
               m = lbl.match(/[^\d]*([\d\.]+)/)
               od = m[1].to_f if m
            end
            t = thickness_nominal(row, map)
            if od > 0
              dxf.add_circle(x + od/2.0, y + od/2.0, od/2.0)
              if t > 0
                id = od - 2.0*t
                dxf.add_circle(x + od/2.0, y + od/2.0, id/2.0) if id > 0
              end
            end
          end
          dxf.add_text(lbl, x, y - 5.0, TEXT_HEIGHT)
        rescue => e
          puts "  [ERROR] Shape '#{lbl}' (#{fam}): #{e.message}"
        end
        count += 1
        x += SPACING_X
        if count % ROW_LIMIT == 0; x = 0.0; y -= SPACING_Y; end
      end
      dxf.close
    end
    UI.messagebox("DXF Generation v1.9.5 (Syntax Audited) Complete!")
  end
end