"""EmbeddingGemma 2 in a llama-server child process, started when there's work and stopped when there isn't, so the
model's memory (and the discrete GPU's power) is only spent while it's used."""

import atexit
import base64
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from . import config


MEDIA_MARKER = "<__media__>"


class LlamaError(RuntimeError):
    pass


def list_devices() -> dict[str, str]:
    """Vulkan devices llama.cpp sees, by name: {"Vulkan0": "AMD Radeon Graphics (RADV RENOIR)", ...}."""
    try:
        out = subprocess.run([str(config.LLAMA_SERVER), "--list-devices"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(re.findall(r"^\s*(Vulkan\d+):\s*(.+?)(?:\s*\(\d+ MiB.*)?$", out, re.M))


def vulkan_types() -> dict[str, str]:
    """What the Vulkan driver says each GPU is, by name: {"AMD Radeon RX 5500M (RADV NAVI14)": "discrete", ...}.
    Asked through libvulkan directly (vulkaninfo isn't always installed); empty if that fails."""
    import ctypes

    try:
        vk = ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return {}

    class InstanceInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_int), ("pNext", ctypes.c_void_p), ("flags", ctypes.c_uint32),
                    ("pApplicationInfo", ctypes.c_void_p), ("enabledLayerCount", ctypes.c_uint32),
                    ("ppEnabledLayerNames", ctypes.c_void_p), ("enabledExtensionCount", ctypes.c_uint32),
                    ("ppEnabledExtensionNames", ctypes.c_void_p)]

    class Properties(ctypes.Structure):   # the start of VkPhysicalDeviceProperties, padded past the rest
        _fields_ = [("apiVersion", ctypes.c_uint32), ("driverVersion", ctypes.c_uint32),
                    ("vendorID", ctypes.c_uint32), ("deviceID", ctypes.c_uint32), ("deviceType", ctypes.c_int),
                    ("deviceName", ctypes.c_char * 256), ("rest", ctypes.c_byte * 4096)]

    instance = ctypes.c_void_p()
    info = InstanceInfo(sType=1)   # VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO
    if vk.vkCreateInstance(ctypes.byref(info), None, ctypes.byref(instance)) != 0:
        return {}
    try:
        count = ctypes.c_uint32()
        vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None)
        handles = (ctypes.c_void_p * count.value)()
        vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), handles)
        names = {1: "integrated", 2: "discrete", 3: "virtual", 4: "cpu"}
        out = {}
        for handle in handles[:count.value]:
            props = Properties()
            vk.vkGetPhysicalDeviceProperties(ctypes.c_void_p(handle), ctypes.byref(props))
            out[props.deviceName.decode(errors="replace")] = names.get(props.deviceType, "other")
        return out
    finally:
        vk.vkDestroyInstance(instance, None)


def classify(devices: dict[str, str]) -> dict[str, str]:
    """"discrete" or "integrated" for each llama.cpp device. The driver's word for it when there is one; otherwise
    names: APUs say "Graphics" or their codename, discrete cards say RX/NAVI/GeForce/RTX/Arc A…/B…."""
    types = vulkan_types()
    out = {}
    for device, name in devices.items():
        # llama.cpp names a device by the driver's name plus, sometimes, the driver in brackets.
        known = next((kind for vk_name, kind in types.items() if name.startswith(vk_name) or vk_name.startswith(name)),
                     None)
        if known in ("discrete", "integrated"):
            out[device] = known
        elif re.search(r"\bRX\b|NAVI|NVIDIA|GeForce|RTX|Quadro|\bArc\(\w+\) [AB]\d", name, re.I):
            out[device] = "discrete"
        elif re.search(r"RENOIR|CEZANNE|REMBRANDT|PHOENIX|STRIX|Graphics\b|\bUHD\b|Iris", name, re.I):
            out[device] = "integrated"
        else:
            out[device] = "discrete"
    return out


def pick_device(preference: str) -> str | None:
    """llama.cpp --device for "dgpu", "igpu", "cpu" or "auto" (the discrete GPU when there is one, else the
    integrated one). None: the CPU, also when there's no Vulkan GPU at all."""
    if preference == "cpu":
        return None
    kinds = classify(list_devices())
    discrete = sorted(d for d, k in kinds.items() if k == "discrete")
    integrated = sorted(d for d, k in kinds.items() if k == "integrated")
    if preference == "igpu":
        return next(iter(integrated + discrete), None)
    return next(iter(discrete + integrated), None)


def physical_cores() -> int:
    """Cores, not hardware threads: the second thread of a core adds little to matrix maths."""
    cores = set()
    for topology in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology"):
        try:
            cores.add(((topology / "physical_package_id").read_text().strip(), (topology / "core_id").read_text().strip()))
        except OSError:
            pass
    return len(cores) or max(1, (os.cpu_count() or 2) // 2)


def threads_for(setting, device: str | None) -> int:
    """[power] threads: a number, or "auto": 3 next to a GPU (it does the work), every core on the CPU alone (the
    indexer runs at idle priority, so the desktop still comes first)."""
    if isinstance(setting, int) and setting > 0:
        return setting
    return 3 if device else physical_cores()


_servers: set["LlamaServer"] = set()


@atexit.register
def _stop_all() -> None:
    """A llama-server outlives a killed parent otherwise (holding ~1 GB); sift's commands turn SIGTERM into an exit."""
    for server in list(_servers):
        server.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self, media: bool, device: str | None, threads: int, name: str):
        self.media = media
        self.device = device
        self.threads = threads
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.last_used = 0.0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        if not config.LLAMA_SERVER.exists():
            raise LlamaError(f"llama-server isn't built: run scripts/build-llama.sh")
        self.port = _free_port()
        args = [str(config.LLAMA_SERVER), "-m", str(config.TEXT_MODEL), "--embeddings", "--pooling", "mean",
                "--host", "127.0.0.1", "--port", str(self.port), "-c", "2048", "-b", "2048", "-ub", "2048",
                "-np", "1", "--threads", str(self.threads), "--threads-batch", str(self.threads), "--no-webui"]
        if self.media:
            # Pictures arrive already sized for their budget (extract.prepare_image): the range keeps mtmd from
            # resizing them again.
            args += ["--mmproj", str(config.MEDIA_MODEL), "--image-min-tokens", "70", "--image-max-tokens", "560"]
        if self.device:
            args += ["--device", self.device, "-ngl", "999"]
            if self.media:
                args += ["--mmproj-offload"]
        else:
            args += ["--device", "none", "-ngl", "0"] + (["--no-mmproj-offload"] if self.media else [])
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = open(config.STATE_DIR / f"llama-{self.name}.log", "wb")
        # The server makes up a random media marker unless one is pinned.
        env = dict(os.environ, LLAMA_MEDIA_MARKER=MEDIA_MARKER)
        self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        _servers.add(self)
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise LlamaError(f"llama-server exited ({self.proc.returncode}); see {log.name}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        self.last_used = time.time()
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.25)
        self.stop()
        raise LlamaError("llama-server didn't start in 3 minutes")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        _servers.discard(self)

    def stop_if_idle(self, seconds: float) -> None:
        if self.running and time.time() - self.last_used > seconds:
            self.stop()

    def _embed(self, content) -> list[np.ndarray]:
        self.start()
        body = json.dumps({"content": content, "embd_normalize": 2}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/embeddings", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=600) as r:
                result = json.load(r)
        except urllib.error.HTTPError as e:
            raise LlamaError(f"llama-server: {e.read().decode(errors='replace')[:300]}") from None
        finally:
            self.last_used = time.time()
        result = sorted(result, key=lambda item: item["index"])
        vectors = []
        for item in result:
            vector = np.asarray(item["embedding"], dtype=np.float32)
            if vector.ndim == 2:
                vector = vector[0]
            vector /= max(np.linalg.norm(vector), 1e-12)
            vectors.append(vector)
        return vectors

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        return self._embed(texts) if texts else []

    def embed_image(self, png: bytes) -> np.ndarray:
        if not self.media:
            raise LlamaError("this server has no vision encoder")
        b64 = base64.b64encode(png).decode()
        return self._embed([{"prompt_string": MEDIA_MARKER, "multimodal_data": [b64]}])[0]
