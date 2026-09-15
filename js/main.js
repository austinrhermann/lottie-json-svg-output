/* Panel logic.
 *
 * Two routes to the same emitters:
 *   1. Export queue — jsx/host.jsx samples each queued comp per frame and
 *      writes a baked intermediate; python turns that into an animated SVG
 *      and/or Lottie JSON. After Effects is the oracle on this path.
 *   2. Convert file / Watch folder — an existing Lottie JSON goes through the
 *      python-lottie evaluator instead (patched; see py/aefix.py and py/kf.py).
 *
 * Comps are baked one at a time on purpose: a bake drives comp.time, so two
 * running at once would fight over the same viewer.
 */
(function () {
  "use strict";

  var cs = new CSInterface();
  var fs = require("fs");
  var path = require("path");
  var os = require("os");
  var cp = require("child_process");

  var EXT = cs.getSystemPath(SystemPath.EXTENSION);
  var HOME = os.homedir();
  var SUPPORT = path.join(HOME, ".lottie2svg");
  var CONFIG = path.join(SUPPORT, "config.json");
  var WIN = process.platform === "win32";
  // a venv puts the interpreter in bin/ on unix and Scripts/ on windows
  var VENV_PY = WIN ? path.join(SUPPORT, "venv", "Scripts", "python.exe")
                    : path.join(SUPPORT, "venv", "bin", "python3");
  var L2S = path.join(EXT, "py", "l2s.py");
  var TOKENS = path.join(EXT, "py", "tokens.json");

  var el = function (id) { return document.getElementById(id); };

  var cfg = {
    outDir: "", trigger: "loop", precision: 2, simplify: 0.1, tokens: true,
    workArea: false, geometry: false, openAfter: false, keepBake: false,
    fmtSvg: true, fmtLottie: false, watchDir: ""
  };

  var comps = [];        // [{id, name, ...}]
  var checked = {};      // comp id -> true
  var watcher = null;
  var busy = false;

  // ----------------------------------------------------------------- config //
  function loadConfig() {
    try {
      var raw = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
      Object.keys(cfg).forEach(function (k) {
        if (raw[k] !== undefined) cfg[k] = raw[k];
      });
    } catch (e) { /* first run */ }
  }

  function saveConfig() {
    try {
      if (!fs.existsSync(SUPPORT)) fs.mkdirSync(SUPPORT, { recursive: true });
      fs.writeFileSync(CONFIG, JSON.stringify(cfg, null, 2), "utf8");
    } catch (e) { log("Could not save settings: " + e.message, "warn"); }
  }

  // -------------------------------------------------------------------- log //
  function log(msg, cls) {
    var box = el("log");
    var line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = msg;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
  }

  function status(msg) { el("status").textContent = msg || ""; }

  // --------------------------------------------------------------- tooltips //
  /* Any element carrying data-tip="Heading|Body" gets one. A single fixed node
   * does the work: the panel is overflow:hidden, so a tooltip parented to the
   * control it describes would be clipped at the panel edge. */
  var tipTimer = null;

  function showTip(node) {
    var raw = node.getAttribute("data-tip");
    if (!raw) return;
    var box = el("tip");
    var split = raw.indexOf("|");
    box.innerHTML = "";
    if (split > -1) {
      var head = document.createElement("b");
      head.textContent = raw.slice(0, split);
      box.appendChild(head);
      box.appendChild(document.createTextNode(raw.slice(split + 1)));
    } else {
      box.textContent = raw;
    }

    // measure first, then place: below the control, nudged back inside the panel
    box.style.left = "0px";
    box.style.top = "0px";
    box.classList.add("on");
    var r = node.getBoundingClientRect();
    var w = box.offsetWidth, h = box.offsetHeight;
    var left = Math.min(r.left, window.innerWidth - w - 6);
    var top = r.bottom + 6;
    if (top + h > window.innerHeight - 4) top = Math.max(4, r.top - h - 6);
    box.style.left = Math.max(4, left) + "px";
    box.style.top = top + "px";
  }

  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    el("tip").classList.remove("on");
  }

  function initTips() {
    document.addEventListener("mouseover", function (e) {
      var node = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (!node) return;
      hideTip();
      tipTimer = setTimeout(function () { showTip(node); }, 350);
    });
    document.addEventListener("mouseout", function (e) {
      var node = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (node) hideTip();
    });
    // a tooltip left hanging over a control you just clicked is only in the way
    document.addEventListener("mousedown", hideTip, true);
    document.addEventListener("scroll", hideTip, true);
    window.addEventListener("blur", hideTip);
  }

  // ------------------------------------------------------------ environment //
  function pythonPath() {
    return fs.existsSync(VENV_PY) ? VENV_PY : null;
  }

  function checkEnv() {
    if (pythonPath()) return true;
    log("Setup needed: no interpreter at " + VENV_PY, "bad");
    log("Run " + (WIN ? "install.ps1" : "install.sh") +
        " from the extension folder once, then reopen this panel.", "bad");
    return false;
  }

  // ------------------------------------------------------------ jsx bridge //
  function evalJSX(code) {
    return new Promise(function (resolve) {
      cs.evalScript(code, function (res) { resolve(res); });
    });
  }

  function parseJSX(res) {
    if (!res || res === "undefined" || res === "EvalScript error.") {
      return { ok: false, error: "ExtendScript did not respond (" + res + ")" };
    }
    try { return JSON.parse(res); }
    catch (e) { return { ok: false, error: "Bad response from AE: " + res }; }
  }

  // ------------------------------------------------------------ run python //
  function runPython(args, wantJson) {
    return new Promise(function (resolve) {
      var py = pythonPath();
      if (!py) return resolve({ ok: false, error: "python not installed; run install.sh" });
      cp.execFile(py, [L2S].concat(args), { maxBuffer: 64 * 1024 * 1024 },
        function (err, stdout, stderr) {
          if (!wantJson) return resolve(err ? { ok: false, error: (stderr || err.message).trim() }
                                            : { ok: true, text: stdout });
          if (err && !stdout) {
            return resolve({ ok: false, error: (stderr || err.message).trim() });
          }
          try { resolve({ ok: true, payload: JSON.parse(stdout) }); }
          catch (e) { resolve({ ok: false, error: (stderr || stdout || e.message).trim() }); }
        });
    });
  }

  function formats() {
    if (cfg.fmtSvg && cfg.fmtLottie) return "both";
    return cfg.fmtLottie ? "lottie" : "svg";
  }

  function pyArgs(input, outBase, fmt) {
    var a = [input, "-o", outBase, "--json",
             "--format", fmt || formats(),
             "--trigger", cfg.trigger,
             "--precision", String(cfg.precision),
             "--simplify", String(cfg.simplify || 0)];
    if (!cfg.tokens) a.push("--no-tokens");
    if (cfg.geometry) a.push("--geometry-only");
    if (fs.existsSync(TOKENS)) a.push("--tokens", TOKENS);
    return a;
  }

  function reportResults(payload) {
    var results = (payload && payload.results) || [];
    if (!results.length) { log("No result returned.", "bad"); return; }
    results.forEach(function (r) {
      if (!r.ok) { log("Failed (" + (r.format || "svg") + "): " + r.error, "bad"); return; }
      var kb = (r.bytes / 1024).toFixed(1);
      var gz = r.gzip_bytes ? (r.gzip_bytes / 1024).toFixed(1) + " KB gzipped, " : "";
      var st = r.stats || {};
      log("Wrote " + path.basename(r.output) + "  (" + kb + " KB, " + gz +
          (st.tracks || 0) + " tracks, " + (st.samples || 0) + " samples)", "ok");
      if (st.samples_dropped) {
        log("  dropped " + st.samples_dropped + " samples, worst error " +
            (st.simplify_err_px || 0) + " px");
      }
      if (st.fit_residual_px) {
        log("  transform fits verified to " + st.fit_residual_px + " px");
      }
      (r.warnings || []).forEach(function (w) { log("  " + w, "warn"); });
      if (cfg.openAfter) reveal(r.output);
    });
  }

  function reveal(p) {
    try {
      if (WIN) cp.execFile("explorer.exe", ["/select,", path.normalize(p)]);
      else cp.execFile("/usr/bin/open", ["-R", p]);
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------ comp queue //
  function refreshComps() {
    return evalJSX("L2S_listComps()").then(function (res) {
      var info = parseJSX(res);
      if (!info.ok) { renderQueue([]); return info; }
      comps = info.comps || [];
      var known = {};
      comps.forEach(function (c) { known[c.id] = true; });
      Object.keys(checked).forEach(function (id) {
        if (!known[id]) delete checked[id];        // comp closed or renamed away
      });
      if (!Object.keys(checked).length) {
        comps.forEach(function (c) { if (c.selected) checked[c.id] = true; });
      }
      if (!cfg.outDir && info.projectPath) {
        setOutDir(path.join(path.dirname(info.projectPath), "export"));
      }
      renderQueue(comps);
      return info;
    });
  }

  function renderQueue(list) {
    var box = el("queue");
    box.innerHTML = "";
    if (!list.length) {
      box.innerHTML = '<div class="empty">No compositions in this project.</div>';
      updateExportButton();
      return;
    }
    list.forEach(function (c) {
      var row = document.createElement("label");
      row.className = "item";
      row.setAttribute("data-tip", c.name + "|" + c.w + " \u00d7 " + c.h + " px \u00b7 " +
        c.frames + " frames @ " + c.fps + " fps \u00b7 " + c.layers +
        (c.layers === 1 ? " layer" : " layers") +
        ". Tick to add it to the export queue.");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!checked[c.id];
      cb.addEventListener("change", function () {
        if (cb.checked) checked[c.id] = true; else delete checked[c.id];
        updateExportButton();
      });
      var nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = c.name;
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = c.w + "\u00d7" + c.h + " \u00b7 " + c.frames + "f @ " + c.fps;
      row.appendChild(cb); row.appendChild(nm); row.appendChild(meta);
      box.appendChild(row);
    });
    updateExportButton();
  }

  function queued() {
    return comps.filter(function (c) { return checked[c.id]; });
  }

  function updateExportButton() {
    var n = queued().length;
    var btn = el("export");
    btn.textContent = n === 1 ? "Export 1 comp" : "Export " + n + " comps";
    btn.disabled = busy || !n || !pythonPath() || !(cfg.fmtSvg || cfg.fmtLottie);
  }

  function setOutDir(dir) {
    cfg.outDir = dir;
    el("outDir").textContent = dir || "(choose a folder)";
    el("outDir").title = dir;
    saveConfig();
  }

  // ---------------------------------------------------------------- export //
  function safeName(s) {
    return (s || "").replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "") || "icon";
  }

  function exportOne(comp) {
    var name = safeName(comp.name);
    var tmp = cfg.keepBake
      ? path.join(cfg.outDir, name + "-bake.json")
      : path.join(os.tmpdir(), "l2s-bake-" + comp.id + "-" + Date.now() + ".json");
    var call = "L2S_bake(" + JSON.stringify(tmp) + "," +
               (cfg.workArea ? "true" : "false") + "," + comp.id + ")";

    status("Sampling " + comp.name + " in After Effects\u2026");
    return evalJSX(call).then(function (res) {
      var baked = parseJSX(res);
      if (!baked.ok) { log(baked.error, "bad"); return; }
      log(baked.comp + " — " + baked.frames + " frames baked", "head");
      if (baked.affineWorst) {
        log("  layer transforms recovered to " + baked.affineWorst + " px");
      }
      (baked.warnings || []).forEach(function (w) { log("  " + w, "warn"); });

      status("Emitting " + comp.name + "\u2026");
      var out = path.join(cfg.outDir, name + ".svg");
      return runPython(pyArgs(tmp, out), true).then(function (r) {
        if (!r.ok) log(r.error, "bad");
        else reportResults(r.payload);
        if (cfg.keepBake) log("  bake kept at " + tmp);
        else { try { fs.unlinkSync(tmp); } catch (e) { /* ignore */ } }
      });
    });
  }

  function exportQueue() {
    if (busy || !checkEnv()) return;
    var list = queued();
    if (!list.length) return;
    if (!cfg.outDir) { log("Choose an output folder first.", "warn"); return; }
    if (!ensureDir(cfg.outDir)) return;

    busy = true;
    updateExportButton();
    var i = 0;
    (function next() {
      if (i >= list.length) {
        busy = false;
        status("");
        log("Done — " + list.length + (list.length === 1 ? " comp." : " comps."), "head");
        updateExportButton();
        return;
      }
      var c = list[i++];
      status("(" + i + "/" + list.length + ") " + c.name);
      exportOne(c).then(next, function (e) {
        log("Export failed on " + c.name + ": " + e.message, "bad");
        next();
      });
    })();
  }

  function ensureDir(dir) {
    try {
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
      return true;
    } catch (e) {
      log("Cannot create " + dir + ": " + e.message, "bad");
      return false;
    }
  }

  // ------------------------------------------------------- convert a file //
  function convertJson(file) {
    if (!checkEnv()) return;
    var dir = cfg.outDir || path.dirname(file);
    if (!ensureDir(dir)) return;
    var out = path.join(dir, path.basename(file).replace(/\.json$/i, "") + ".svg");
    status("Converting " + path.basename(file) + "\u2026");
    // an existing Lottie JSON can only go one way: to SVG
    runPython(pyArgs(file, out, "svg"), true).then(function (r) {
      status("");
      if (!r.ok) log(r.error, "bad");
      else reportResults(r.payload);
    });
  }

  // ------------------------------------------------------------- watching //
  function stableThenConvert(file, tries) {
    // Bodymovin and friends write progressively; wait for the size to settle.
    var last = -1, n = 0;
    var tick = setInterval(function () {
      var size;
      try { size = fs.statSync(file).size; }
      catch (e) { clearInterval(tick); return; }
      if (size === last && size > 0) {
        clearInterval(tick);
        convertJson(file);
        return;
      }
      last = size;
      if (++n > (tries || 40)) {
        clearInterval(tick);
        log("Gave up waiting for " + path.basename(file) + " to finish writing.", "warn");
      }
    }, 250);
  }

  function startWatch(dir) {
    stopWatch();
    var seen = {};
    try {
      watcher = fs.watch(dir, function (evt, name) {
        if (!name || !/\.json$/i.test(name)) return;
        if (/-bake\.json$/i.test(name)) return;      // our own intermediates
        var full = path.join(dir, name);
        var now = Date.now();
        if (seen[full] && now - seen[full] < 1500) return;
        seen[full] = now;
        log("Saw " + name, "head");
        stableThenConvert(full);
      });
    } catch (e) {
      log("Cannot watch " + dir + ": " + e.message, "bad");
      return;
    }
    cfg.watchDir = dir;
    saveConfig();
    el("watch").textContent = "Stop watching";
    el("watch").classList.add("watching");
    log("Watching " + dir + " — any Lottie JSON dropped there becomes an SVG.", "ok");
  }

  function stopWatch() {
    if (watcher) {
      try { watcher.close(); } catch (e) { /* ignore */ }
      watcher = null;
      log("Stopped watching.", "head");
    }
    el("watch").textContent = "Watch folder\u2026";
    el("watch").classList.remove("watching");
  }

  // -------------------------------------------------------------- dialogs //
  function pickFolder(title, initial) {
    var r = window.cep.fs.showOpenDialogEx(false, true, title, initial || HOME);
    return (r && r.data && r.data.length) ? r.data[0] : null;
  }

  function pickFile(title, initial) {
    var r = window.cep.fs.showOpenDialogEx(false, false, title, initial || HOME, ["json"]);
    return (r && r.data && r.data.length) ? r.data[0] : null;
  }

  // ----------------------------------------------------------------- wire //
  function bindOption(id, key, kind) {
    var node = el(id);
    if (kind === "check") {
      node.checked = !!cfg[key];
      node.addEventListener("change", function () {
        cfg[key] = node.checked;
        saveConfig();
        updateExportButton();
      });
    } else {
      node.value = cfg[key];
      node.addEventListener("change", function () {
        if (kind === "number") cfg[key] = parseInt(node.value, 10) || 0;
        else if (kind === "float") cfg[key] = parseFloat(node.value) || 0;
        else cfg[key] = node.value;
        saveConfig();
      });
    }
  }

  function init() {
    loadConfig();
    setOutDir(cfg.outDir);
    bindOption("trigger", "trigger");
    bindOption("precision", "precision", "number");
    bindOption("simplify", "simplify", "float");
    bindOption("tokens", "tokens", "check");
    bindOption("workArea", "workArea", "check");
    bindOption("geometry", "geometry", "check");
    bindOption("openAfter", "openAfter", "check");
    bindOption("keepBake", "keepBake", "check");
    bindOption("fmtSvg", "fmtSvg", "check");
    bindOption("fmtLottie", "fmtLottie", "check");

    el("export").addEventListener("click", exportQueue);
    el("refresh").addEventListener("click", function () { refreshComps(); });
    el("selAll").addEventListener("click", function () {
      comps.forEach(function (c) { checked[c.id] = true; });
      renderQueue(comps);
    });
    el("selNone").addEventListener("click", function () {
      checked = {};
      renderQueue(comps);
    });
    el("selAE").addEventListener("click", function () {
      checked = {};
      refreshComps();
    });
    el("pickOut").addEventListener("click", function () {
      var d = pickFolder("Where should the exports go?", cfg.outDir);
      if (d) setOutDir(d);
    });
    el("pickJson").addEventListener("click", function () {
      var f = pickFile("Choose a Lottie JSON", cfg.watchDir || cfg.outDir);
      if (f) convertJson(f);
    });
    el("watch").addEventListener("click", function () {
      if (watcher) { stopWatch(); return; }
      var d = pickFolder("Watch which folder for Lottie JSON?", cfg.watchDir);
      if (d) startWatch(d);
    });
    el("copyCss").addEventListener("click", function () {
      runPython(["--emit-css", "--tokens", TOKENS], false).then(function (r) {
        if (!r.ok || !r.text) { log("Could not read the CSS bindings.", "bad"); return; }
        var ta = document.createElement("textarea");
        ta.value = r.text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        log("Token CSS copied to the clipboard.", "ok");
      });
    });
    el("clear").addEventListener("click", function () { el("log").innerHTML = ""; });

    initTips();
    cs.addEventListener("com.adobe.csxs.events.ThemeColorChanged", applyHostTheme);
    applyHostTheme();
    // The comp list is refreshed on demand and whenever the panel regains
    // focus, rather than on a timer — polling the whole project every second
    // is real work in After Effects for no benefit.
    window.addEventListener("focus", function () { if (!busy) refreshComps(); });
    checkEnv();
    refreshComps();
  }

  /** Follow AE's own brightness so the panel does not fight the host UI. */
  function applyHostTheme() {
    try {
      var skin = cs.getHostEnvironment().appSkinInfo;
      var c = skin.panelBackgroundColor.color;
      var lum = (c.red + c.green + c.blue) / 3;
      var root = document.documentElement.style;
      root.setProperty("--bg", rgb(c));
      root.setProperty("--panel", shift(c, lum > 128 ? -10 : 14));
      root.setProperty("--line", shift(c, lum > 128 ? -22 : -12));
      root.setProperty("--edge", shift(c, lum > 128 ? -40 : 30));
      root.setProperty("--fg", lum > 128 ? "#1f1f1f" : "#d6d6d6");
      root.setProperty("--dim", lum > 128 ? "#6a6a6a" : "#8f8f8f");
    } catch (e) { /* keep the defaults */ }
  }

  function clamp(v) { return Math.max(0, Math.min(255, Math.round(v))); }
  function rgb(c) {
    return "rgb(" + clamp(c.red) + "," + clamp(c.green) + "," + clamp(c.blue) + ")";
  }
  function shift(c, d) {
    return "rgb(" + clamp(c.red + d) + "," + clamp(c.green + d) + "," + clamp(c.blue + d) + ")";
  }

  var started = false;
  function boot() { if (!started) { started = true; init(); } }
  document.addEventListener("DOMContentLoaded", boot);
  if (document.readyState !== "loading") boot();
})();
