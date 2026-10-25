"""Prototype Agent - Flask app. Double-click the .exe (or run: python app.py)."""
import io, json, os, socket, sys, threading, webbrowser, zipfile, logging
import requests
from flask import Flask, jsonify, render_template, request, send_file

# A windowed .exe has no console, so give print() somewhere to go
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")

# Folder where the html/static files live (changes inside a PyInstaller .exe)
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=os.path.join(BASE, "templates"),
            static_folder=os.path.join(BASE, "static"))

# Settings are saved on your laptop, so you paste the key only once
CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PrototypeAgent")
CFG = os.path.join(CFG_DIR, "settings.json")
DEFAULTS = {"mode": "nvidia", "nvidia_key": "",
            "nvidia_model": "deepseek-ai/deepseek-v4.1-flash", "ollama_model": "qwen2.5:7b"}

def load():
    s = dict(DEFAULTS)
    try:
        with open(CFG, encoding="utf-8") as f: s.update(json.load(f))
    except Exception: pass
    return s

# ---------------- The four agent prompts ----------------
PROMPTS = {
"requirement": """You are a Requirement Agent for embedded systems. Convert the user's requirement into a structured spec.
In block_diagram write each link as "Part A -> Part B" and always use the exact same name for the same part.
Return ONLY valid JSON: {"project_name":"","summary":"","functional_requirements":[""],"constraints":[""],
"inputs":[{"name":"","type":"","description":""}],"outputs":[{"name":"","type":"","description":""}],
"block_diagram":["Sensor -> ESP32","ESP32 -> Display"]}""",
"hardware": """You are a Hardware Planning Agent. Using the requirement JSON and board, pick cheap, common parts. Use ONLY pins that exist on that board.
Return ONLY valid JSON: {"board":"","components":[{"name":"","part_number":"","reason":""}],
"bill_of_materials":[{"item":"","quantity":1,"approx_cost_inr":0}],
"pin_mapping":[{"component":"","component_pin":"","board_pin":""}],"power_notes":"","wiring_steps":[""]}""",
"firmware": """You are a Firmware Agent. Using the requirement and hardware JSON, write the algorithm and a SIMPLE beginner-friendly
firmware skeleton with a comment on every important line (Arduino C++ for ESP32/Arduino, MicroPython for Pico). Use the exact pins from pin_mapping.
Return ONLY valid JSON: {"language":"","algorithm_steps":[""],"libraries_needed":[""],"code":"full code, \\n for new lines"}""",
"test_plan": """You are a Test Agent. Using all previous JSON, write a verification plan doable with a multimeter, serial monitor, optional logic analyzer.
Return ONLY valid JSON: {"tests":[{"id":"T1","what_is_tested":"","method":"","tool":"","pass_criteria":""}],"power_on_checklist":[""]}""",
}

# ---------------- Talking to the AI ----------------
def call_ai(system, user, s):
    if s["mode"] == "ollama":
        try:
            r = requests.post("http://localhost:11434/api/chat", timeout=600, json={
                "model": s["ollama_model"], "stream": False, "format": "json",
                "options": {"num_ctx": 8192, "temperature": 0.2},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except requests.exceptions.ConnectionError:
            raise RuntimeError("Ollama is not running. Start the Ollama app, then press Retry.")
        if r.status_code != 200: raise RuntimeError("Ollama error: " + r.text[:300])
        return r.json()["message"]["content"]
    key = s["nvidia_key"] or os.environ.get("NVIDIA_API_KEY", "")
    if not key: raise RuntimeError("No NVIDIA key saved. Open Settings and paste your key.")
    try:
        r = requests.post("https://integrate.api.nvidia.com/v1/chat/completions", timeout=(15, 600), stream=True,
            headers={"Authorization": "Bearer " + key},
            json={"model": s["nvidia_model"], "temperature": 0.2, "max_tokens": 8000, "stream": True,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        if r.status_code in (401, 403): raise RuntimeError("NVIDIA key rejected (%d). Check the key in Settings." % r.status_code)
        if r.status_code in (404, 410): raise RuntimeError("Model not available (%d). Pick another model ID in Settings." % r.status_code)
        if r.status_code != 200: raise RuntimeError("NVIDIA error %d: %s" % (r.status_code, r.text[:300]))
        text = ""
        for line in r.iter_lines():          # the answer arrives piece by piece, which keeps the connection alive
            if not line.startswith(b"data:"): continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]": break
            try: delta = json.loads(chunk)["choices"][0]["delta"]
            except (ValueError, KeyError, IndexError): continue
            text += delta.get("content") or ""
        return text
    except requests.exceptions.RequestException as e:
        # show the real reason (timeout, no internet, SSL...) so it is easy to diagnose
        raise RuntimeError("The NVIDIA model is slow or the connection dropped. Press Retry, or pick a faster non-reasoning model in Settings. Details: " + type(e).__name__ + " - " + str(e)[:160])

def ask_json(stage, message, s):
    for _ in range(2):                                    # try twice
        text = call_ai(PROMPTS[stage], message, s)
        try: return json.loads(text[text.find("{"): text.rfind("}") + 1])
        except ValueError: pass
    raise RuntimeError("The AI did not return valid JSON twice. Press Retry or choose another model in Settings.")

# ---------------- Web pages / API ----------------
@app.route("/")
def home(): return render_template("index.html")

@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    s = load()
    if request.method == "POST":
        d = request.get_json()
        s["mode"] = d.get("mode", s["mode"])
        s["nvidia_model"] = d.get("nvidia_model", "").strip() or DEFAULTS["nvidia_model"]
        s["ollama_model"] = d.get("ollama_model", "").strip() or DEFAULTS["ollama_model"]
        if d.get("nvidia_key", "").strip(): s["nvidia_key"] = d["nvidia_key"].strip()
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f: json.dump(s, f, indent=2)
    # the key itself is never sent back to the browser
    return jsonify({"mode": s["mode"], "nvidia_model": s["nvidia_model"], "ollama_model": s["ollama_model"],
                    "has_key": bool(s["nvidia_key"] or os.environ.get("NVIDIA_API_KEY"))})

@app.route("/api/stage", methods=["POST"])
def stage():
    d = request.get_json()
    if d.get("stage") not in PROMPTS: return jsonify({"error": "Unknown stage"}), 400
    msg = "Previous artifacts:\n" + json.dumps(d.get("artifacts", {}), indent=2)
    if d.get("feedback"): msg += "\n\nThe student rejected your last answer. Fix this: " + d["feedback"]
    try: return jsonify({"result": ask_json(d["stage"], msg, load())})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
def download():
    d = request.get_json(); art = d.get("artifacts", {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in ("requirement", "hardware", "firmware", "test_plan"):
            if k in art: z.writestr(k + ".json", json.dumps(art[k], indent=2))
        fw = art.get("firmware", {})
        if fw.get("code"):
            z.writestr("firmware" + (".py" if "python" in fw.get("language", "").lower() else ".ino"), fw["code"])
        z.writestr("report.html", d.get("report_html", ""))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="prototype_output.zip")

@app.route("/api/quit", methods=["POST"])
def quit_app():
    threading.Timer(0.5, lambda: os._exit(0)).start()     # close the whole program
    return jsonify({"ok": True})

def free_port():
    for p in range(5000, 5050):
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", p)) != 0: return p   # nothing is using it
    return 5000

if __name__ == "__main__":
    port = free_port()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % port)).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
