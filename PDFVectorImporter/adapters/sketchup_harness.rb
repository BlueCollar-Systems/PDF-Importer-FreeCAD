# frozen_string_literal: false
# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# SketchUp QA harness for PDF Vector Importer.
#
# Runs inside SketchUp (for example via -RubyStartup) and:
# 1) reads payload from ENV["BC_PDF_QA_PAYLOAD"]
# 2) loads extension entry points
# 3) runs pipeline import with selected mode/pages
# 4) writes JSON result to payload["result_json"]

require "json"
require "time"

module BCPDFQA
  module SketchUpHarness
    module_function

    def run
      payload_path = ENV["BC_PDF_QA_PAYLOAD"].to_s
      raise "BC_PDF_QA_PAYLOAD not set" if payload_path.strip.empty?
      payload = JSON.parse(File.read(payload_path))
      result_path = payload["result_json"].to_s
      raise "result_json missing in payload" if result_path.strip.empty?
      progress_path = payload["progress_json"].to_s

      start_time = Time.now
      progress_lock = Mutex.new
      progress_state = {
        "test_id" => payload["test_id"],
        "platform" => "SU",
        "status" => "RUNNING",
        "current_stage" => "boot",
        "started_at" => start_time.utc.iso8601,
        "last_heartbeat_at" => start_time.utc.iso8601,
        "heartbeat_count" => 0,
        "stage_history" => [],
        "ruby_pid" => Process.pid
      }
      progress_tick = lambda do |stage, details = nil|
        now = Time.now.utc.iso8601
        progress_lock.synchronize do
          progress_state["current_stage"] = stage
          progress_state["last_heartbeat_at"] = now
          progress_state["heartbeat_count"] = progress_state["heartbeat_count"].to_i + 1
          entry = {"at" => now, "stage" => stage}
          entry["details"] = details if details
          history = progress_state["stage_history"]
          history << entry
          history.shift while history.length > 80
          write_progress_snapshot(progress_path, progress_state)
        end
      end

      progress_tick.call("payload_loaded", {"payload_path" => payload_path})
      result = {
        "test_id" => payload["test_id"],
        "platform" => "SU",
        "status" => "ERROR",
        "message" => "",
        "started_at" => start_time.utc.iso8601
      }
      heartbeat_thread = nil

      begin
        loader = require_extension(payload)
        progress_tick.call("extension_loaded", {"loader" => loader})
        importer = BlueCollarSystems::PDFVectorImporter

        model = Sketchup.active_model
        raise "No active SketchUp model" unless model
        progress_tick.call("model_ready")

        before = model_entity_counts(model)
        before_layers = safe_layer_count(model)
        progress_tick.call("model_counts_before_done", {
          "entities" => before,
          "layers_before" => before_layers
        })

        opts = build_opts(payload, importer)
        progress_tick.call("options_built")
        progress_tick.call("pipeline_started", {
          "input_pdf" => payload["input_pdf"],
          "mode" => payload["mode"],
          "page_range" => payload["page_range"]
        })
        heartbeat_thread = Thread.new do
          loop do
            sleep 2
            progress_tick.call("pipeline_running")
          end
        end
        stats = importer.run_pipeline(model, payload["input_pdf"], opts)
        if heartbeat_thread
          heartbeat_thread.kill
          heartbeat_thread.join(0.2) rescue nil
          heartbeat_thread = nil
        end
        raise "run_pipeline returned nil" if stats.nil?
        progress_tick.call("pipeline_completed")

        after = model_entity_counts(model)
        after_layers = safe_layer_count(model)
        progress_tick.call("model_counts_after_done", {
          "entities" => after,
          "layers_after" => after_layers
        })

        result["status"] = "PASS"
        result["message"] = "Import completed."
        result["loader"] = loader
        result["input_pdf"] = payload["input_pdf"]
        result["mode"] = payload["mode"]
        result["page_range"] = payload["page_range"]
        result["pipeline_stats"] = stats
        result["model_counts_before"] = before
        result["model_counts_after"] = after
        result["model_delta"] = hash_delta(after, before)
        result["layers_before"] = before_layers
        result["layers_after"] = after_layers
        result["layers_delta"] = after_layers - before_layers
        progress_lock.synchronize { progress_state["status"] = "PASS" }
        progress_tick.call("pipeline_succeeded")
      rescue => e
        if heartbeat_thread
          heartbeat_thread.kill
          heartbeat_thread.join(0.2) rescue nil
          heartbeat_thread = nil
        end
        result["status"] = "FAIL"
        result["message"] = "#{e.class}: #{e.message}"
        result["backtrace"] = (e.backtrace || [])[0, 25]
        progress_lock.synchronize { progress_state["status"] = "FAIL" }
        progress_tick.call("pipeline_failed", {"error" => result["message"]})
      ensure
        if heartbeat_thread
          heartbeat_thread.kill
          heartbeat_thread.join(0.2) rescue nil
          heartbeat_thread = nil
        end
        finish = Time.now
        result["finished_at"] = finish.utc.iso8601
        result["runtime_seconds"] = (finish - start_time).round(3)
        progress_lock.synchronize do
          progress_state["status"] = result["status"]
          progress_state["finished_at"] = result["finished_at"]
          progress_state["runtime_seconds"] = result["runtime_seconds"]
        end
        progress_tick.call("writing_result", {"result_status" => result["status"]})
        safe_result = utf8_safe(result)
        json_str = nil
        begin
          json_str = JSON.pretty_generate(safe_result)
        rescue StandardError
          json_str = JSON.generate({"status" => "ERROR", "message" => "JSON encoding failed"})
        end
        File.open(result_path, "w") { |f| f.write(json_str) }
        progress_tick.call("result_written", {"result_path" => result_path})
      end
    end

    def utf8_safe(obj)
      case obj
      when String
        begin
          s = obj.dup
          s.force_encoding("UTF-8")
          unless s.valid_encoding?
            s = obj.encode("UTF-8", "binary", invalid: :replace, undef: :replace, replace: "?")
          end
          s
        rescue StandardError
          obj.to_s.force_encoding("UTF-8")
        end
      when Array
        obj.map { |v| utf8_safe(v) }
      when Hash
        out = {}
        obj.each { |k, v| out[utf8_safe(k)] = utf8_safe(v) }
        out
      else
        obj
      end
    end

    def write_progress_snapshot(progress_path, progress_state)
      return if progress_path.nil?
      path = progress_path.to_s
      return if path.strip.empty?
      safe_progress = utf8_safe(progress_state)
      json_str = JSON.pretty_generate(safe_progress)
      File.open(path, "w") { |f| f.write(json_str) }
    rescue StandardError
      nil
    end

    def build_opts(payload, importer)
      modes = importer::ImportDialog::MODES
      mode_name = payload["mode"].to_s.strip
      mode_name = "Auto" if mode_name.empty?
      mode_name = mode_name[0].upcase + mode_name[1..-1].to_s.downcase
      mode = modes[mode_name] || modes["Auto"] || {}

      raw = {}
      mode.each { |k, v| raw[k.to_sym] = v }
      raw[:pages] = payload["page_range"].to_s.strip.empty? ? "1" : payload["page_range"].to_s
      raw[:scale] = "1.0" if raw[:scale].to_s.strip.empty?

      importer::ImportDialog.send(:build_opts, raw)
    end

    def require_extension(payload)
      candidates = []
      ext_root = resolve_path(payload["extension_root"])
      plugins_dir = resolve_path(payload["plugins_dir"])

      if ext_root && !ext_root.empty?
        candidates << File.join(ext_root, "main")
        candidates << File.join(ext_root, "main.rb")
        candidates << File.join(File.dirname(ext_root), "bc_pdf_vector_importer.rb")
      end
      if plugins_dir && !plugins_dir.empty?
        candidates << File.join(plugins_dir, "bc_pdf_vector_importer", "main")
        candidates << File.join(plugins_dir, "bc_pdf_vector_importer", "main.rb")
        candidates << File.join(plugins_dir, "bc_pdf_vector_importer.rb")
      end

      candidates.each do |path|
        loaded = false
        begin
          require path
          loaded = true
        rescue LoadError
          loaded = false
        rescue StandardError
          loaded = false
        end
        return path if loaded
      end

      if defined?(BlueCollarSystems::PDFVectorImporter) &&
         BlueCollarSystems::PDFVectorImporter.respond_to?(:run_pipeline)
        return "already_loaded"
      end

      raise "Could not load PDF Vector Importer from extension_root/plugins_dir."
    end

    def resolve_path(value)
      return nil if value.nil?
      s = value.to_s
      return nil if s.strip.empty?
      s = s.gsub(/%([^%]+)%/) { |m| ENV[$1] || m }
      File.expand_path(s)
    rescue
      value.to_s
    end

    def safe_layer_count(model)
      model.layers.length
    rescue
      0
    end

    def model_entity_counts(model)
      acc = {
        "edges" => 0,
        "faces" => 0,
        "groups" => 0,
        "component_instances" => 0,
        "texts" => 0,
        "images" => 0
      }
      count_entities(model.entities, acc, {})
      acc
    end

    def count_entities(entities, acc, seen_defs)
      entities.each do |e|
        if e.is_a?(Sketchup::Edge)
          acc["edges"] += 1
        elsif e.is_a?(Sketchup::Face)
          acc["faces"] += 1
        elsif defined?(Sketchup::Text) && e.is_a?(Sketchup::Text)
          acc["texts"] += 1
        elsif defined?(Sketchup::Image) && e.is_a?(Sketchup::Image)
          acc["images"] += 1
        elsif e.is_a?(Sketchup::Group)
          acc["groups"] += 1
          count_entities(e.entities, acc, seen_defs)
        elsif e.is_a?(Sketchup::ComponentInstance)
          acc["component_instances"] += 1
          d = e.definition
          next if d.nil?
          did = d.object_id
          next if seen_defs[did]
          seen_defs[did] = true
          count_entities(d.entities, acc, seen_defs)
        end
      end
    end

    def hash_delta(after, before)
      out = {}
      after.each do |k, v|
        out[k] = v.to_i - before[k].to_i
      end
      out
    end
  end
end

BCPDFQA::SketchUpHarness.run
