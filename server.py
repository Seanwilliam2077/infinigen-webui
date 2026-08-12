# Infinigen Web UI — 零依赖本地服务器（纯 Python 标准库）
# 启动: uv run python server.py  → http://127.0.0.1:8765
#
# 设计约束:
# - 输出路径必须是绝对路径（Windows 上 Blender 会把相对路径解析到盘符根, 见 upstream bug）
# - 所有生成器名在服务端 CATALOG 白名单校验, 客户端输入不直接进命令行
# - 同时只跑一个渲染任务（独占 GPU）

import json
import os
import random
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
WEB_OUT = ROOT / "outputs"
INDEX = ROOT / "index.html"
PORT = int(os.environ.get("PORT", 8765))

CATALOG = {
    "material": {
        "label": "材质",
        "chain": lambda gid, base: [gid, base, "render_cycles"],
        "items": [
            {"id": "bricks_rand", "name": "砖墙", "emoji": "🧱"},
            {"id": "marble_rand", "name": "大理石", "emoji": "🪨"},
            {"id": "wood_grain_rand", "name": "木纹", "emoji": "🪵"},
            {"id": "wood_planks_rand", "name": "木板", "emoji": "🟫"},
            {"id": "fabric_patterned_rand", "name": "印花布料", "emoji": "🧵"},
            {"id": "carpet_rand", "name": "地毯绒布", "emoji": "🧶"},
            {"id": "scratched_metal_rand", "name": "划痕金属", "emoji": "⚙️"},
            {"id": "metal_brushed_linear_rand", "name": "拉丝金属", "emoji": "🔩"},
            {"id": "metal_hammered_rand", "name": "锤纹金属", "emoji": "🔨"},
            {"id": "granite_rand", "name": "花岗岩", "emoji": "⛰️"},
            {"id": "terrazzo_rand", "name": "水磨石", "emoji": "🎛️"},
            {"id": "tile_rand", "name": "瓷砖", "emoji": "🀄"},
            {"id": "ceramic_rand", "name": "陶瓷", "emoji": "🏺"},
            {"id": "concrete_rand", "name": "混凝土", "emoji": "🧱"},
            {"id": "paint_flaked_rand", "name": "剥落油漆", "emoji": "🎨"},
            {"id": "glass_colored_rand", "name": "彩色玻璃", "emoji": "🔮"},
            {"id": "plastic_opaque_rand", "name": "塑料", "emoji": "🧴"},
            {"id": "stone_smooth_rand", "name": "光面石材", "emoji": "🗿"},
        ],
        "bases": [
            {"id": "material_sphere", "name": "球体"},
            {"id": "material_torus_uv", "name": "圆环"},
            {"id": "material_monkey", "name": "猴头"},
            {"id": "material_cube", "name": "立方体"},
            {"id": "material_banana", "name": "香蕉"},
        ],
    },
    "object": {
        "label": "家具物件",
        "chain": lambda gid, base: [gid, "object_demo", "render_cycles"],
        "items": [
            {"id": "sofa_rand", "name": "沙发", "emoji": "🛋️"},
            {"id": "lamp_rand", "name": "落地灯", "emoji": "💡"},
            {"id": "desk_lamp_rand", "name": "台灯", "emoji": "🛎️"},
            {"id": "dining_table_rand", "name": "餐桌", "emoji": "🍽️"},
            {"id": "coffee_table_rand", "name": "咖啡桌", "emoji": "☕"},
            {"id": "side_table_rand", "name": "边桌", "emoji": "🪑"},
            {"id": "desk_rand", "name": "书桌", "emoji": "🖥️"},
            {"id": "bookcase_rand", "name": "书柜", "emoji": "📚"},
            {"id": "cabinet_rand", "name": "储物柜", "emoji": "🗄️"},
            {"id": "drawers_rand", "name": "抽屉柜", "emoji": "🗃️"},
            {"id": "vase_rand", "name": "花瓶", "emoji": "🏺"},
            {"id": "flower_rand", "name": "花", "emoji": "🌸"},
            {"id": "rug_rand", "name": "地毯", "emoji": "🟪"},
            {"id": "window_rand", "name": "窗户", "emoji": "🪟"},
            {"id": "curtain_rand", "name": "窗帘", "emoji": "🎪"},
            {"id": "wall_art_rand", "name": "挂画", "emoji": "🖼️"},
            {"id": "mirror_rand", "name": "镜子", "emoji": "🪞"},
            {"id": "triangle_shelf_rand", "name": "三角搁架", "emoji": "📐"},
        ],
    },
    "scene": {
        "label": "房间",
        "chain": lambda gid, base: [gid, "render_cycles"],
        "items": [
            {"id": "livingroom_rand", "name": "客厅", "emoji": "🏠"},
        ],
    },
}

QUALITY = {
    "fast": {"name": "快速预览", "res": (320, 320), "samples": 32,
             "room_res": (400, 300), "room_samples": 64},
    "std": {"name": "标准", "res": (512, 512), "samples": 256,
            "room_res": (640, 480), "room_samples": 384},
    "high": {"name": "高清", "res": (768, 768), "samples": 1024,
             "room_res": (960, 720), "room_samples": 1024},
}

_job_lock = threading.Lock()
_job = None  # 当前/最近一次任务的状态字典


def _valid_ids(kind):
    return {it["id"] for it in CATALOG[kind]["items"]}


def _run_job(job, cmd, out_dir):
    job["phase"] = "启动 Blender 引擎…"
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        job["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job["log"].append(line)
            del job["log"][:-80]
            m = re.search(r"Executing (\w+)", line)
            if m:
                gen = m.group(1)
                job["phase"] = "渲染中…" if gen.startswith("render") else f"生成 {gen} …"
            m = re.search(r"Sample (\d+)/(\d+)", line)
            if m:
                job["phase"] = f"渲染中… 采样 {m.group(1)}/{m.group(2)}"
        code = proc.wait(timeout=1800)
        pngs = sorted(out_dir.rglob("*.png"))
        if code == 0 and pngs:
            rel = pngs[0].relative_to(WEB_OUT).as_posix()
            job["image"] = f"/img/{rel}"
            job["state"] = "done"
            meta = {k: job[k] for k in
                    ("name", "emoji", "kind", "seed", "cmd", "started")}
            meta["image"] = job["image"]
            meta["elapsed"] = round(time.time() - job["started"], 1)
            (out_dir / "webui_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        else:
            job["state"] = "error"
            job["error"] = "\n".join(job["log"][-12:]) or f"exit code {code}"
    except Exception as e:  # noqa: BLE001 — 任何失败都要反馈到前端
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        job["phase"] = ""


def start_job(kind, gid, base, quality, seed):
    global _job
    with _job_lock:
        if _job and _job["state"] == "running":
            return None, "已有任务在生成中，请稍候"
        if kind not in CATALOG or gid not in _valid_ids(kind):
            return None, f"未知生成器: {gid}"
        cat = CATALOG[kind]
        bases = {b["id"] for b in cat.get("bases", [])}
        if bases and base not in bases:
            return None, f"未知底模: {base}"
        q = QUALITY.get(quality) or QUALITY["std"]
        res, samples = (q["room_res"], q["room_samples"]) if kind == "scene" \
            else (q["res"], q["samples"])

        out_dir = WEB_OUT / f"{int(time.time())}_{gid}_{seed}"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["uv", "run", "infinigen", *cat["chain"](gid, base),
               "--seed", str(seed), "--output", str(out_dir),
               "-r", str(res[0]), str(res[1]), "-s", str(samples)]
        item = next(it for it in cat["items"] if it["id"] == gid)
        _job = {
            "state": "running", "phase": "排队中…", "log": [],
            "name": item["name"], "emoji": item["emoji"], "kind": kind,
            "seed": seed, "cmd": " ".join(cmd), "started": time.time(),
            "image": None, "error": None,
        }
        threading.Thread(target=_run_job, args=(_job, cmd, out_dir),
                         daemon=True).start()
        return _job, None


def gallery():
    entries = []
    if WEB_OUT.exists():
        for meta_file in WEB_OUT.glob("*/webui_meta.json"):
            try:
                entries.append(json.loads(meta_file.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    entries.sort(key=lambda e: e.get("started", 0), reverse=True)
    return entries[:60]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静点
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/catalog":
            cat = {k: {"label": v["label"], "items": v["items"],
                       "bases": v.get("bases", [])}
                   for k, v in CATALOG.items()}
            self._send(200, {"catalog": cat, "quality": [
                {"id": k, "name": v["name"]} for k, v in QUALITY.items()]})
        elif path == "/api/status":
            if _job is None:
                self._send(200, {"state": "idle"})
            else:
                j = {k: _job[k] for k in ("state", "phase", "name", "emoji",
                                          "kind", "seed", "cmd", "image", "error")}
                j["elapsed"] = round(time.time() - _job["started"], 1)
                j["log"] = _job["log"][-6:]
                self._send(200, j)
        elif path == "/api/gallery":
            self._send(200, {"items": gallery()})
        elif path.startswith("/img/"):
            target = (WEB_OUT / path[5:]).resolve()
            if target.is_file() and target.suffix == ".png" \
                    and target.is_relative_to(WEB_OUT.resolve()):
                self._send(200, target.read_bytes(), "image/png")
            else:
                self._send(404, {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/generate":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            seed = int(req.get("seed", random.randrange(2**31)))
            job, err = start_job(
                str(req.get("kind", "")), str(req.get("generator", "")),
                str(req.get("base", "material_sphere")),
                str(req.get("quality", "std")), seed)
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": f"请求格式错误: {e}"})
            return
        if err:
            self._send(409, {"error": err})
        else:
            self._send(200, {"ok": True, "seed": job["seed"]})


if __name__ == "__main__":
    WEB_OUT.mkdir(parents=True, exist_ok=True)
    print(f"Infinigen Web UI → http://127.0.0.1:{PORT}  (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
