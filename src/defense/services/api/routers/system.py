"""System Monitor telemetry and diagnostics endpoints.

Surfaces host hardware metrics (CPU per-core, GPU NVIDIA/CUDA, RAM, Storage Disks & I/O,
Docker containers live load, Network), datastores health and connection statistics (PostgreSQL, Redis, MinIO, ClickHouse),
Ollama local model inference server & loaded models, pipeline stream depths & DLQs,
Agent Intelligence Registry, and AI model / LLM runtime telemetry.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
import psutil
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from defense.libs import sentiment_models, streams
from defense.libs.common.config import app_env, get_settings
from defense.libs.llm.client import VALID_ROLES, LLMClient
from defense.services.agents.registry import AGENT_REGISTRY
from defense.services.api.deps import engine, get_current_user, get_db, get_redis

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/system", tags=["system"])

_PROCESS_START_TIME = time.time()
_PROCESS = psutil.Process()


def _format_bytes(size_bytes: int | float) -> str:
    """Format bytes into a human-readable string (KB, MB, GB, TB)."""
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    val = float(size_bytes)
    while val >= 1024.0 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}" if i > 0 else f"{int(val)} B"


def _format_duration(seconds: int | float) -> str:
    """Format duration into days, hours, minutes, seconds."""
    secs = int(max(0, seconds))
    days = secs // 86400
    secs %= 86400
    hours = secs // 3600
    secs %= 3600
    minutes = secs // 60
    seconds_rem = secs % 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds_rem}s")
    return " ".join(parts)


def _get_cpu_stats() -> dict[str, Any]:
    """Collect detailed CPU load, per-core utilization, and frequency."""
    cpu_percent = psutil.cpu_percent(interval=None)
    per_cpu = psutil.cpu_percent(interval=None, percpu=True)
    cpu_logical = psutil.cpu_count(logical=True) or 1
    cpu_physical = psutil.cpu_count(logical=False) or cpu_logical
    load_avg = [round(x, 2) for x in psutil.getloadavg()] if hasattr(psutil, "getloadavg") else [0.0, 0.0, 0.0]
    normalized_load_pct = round((load_avg[0] / cpu_logical) * 100.0, 1) if cpu_logical > 0 else 0.0

    freq_info = {}
    try:
        cfreq = psutil.cpu_freq()
        if cfreq:
            freq_info = {
                "current_mhz": round(cfreq.current, 1),
                "min_mhz": round(cfreq.min, 1) if cfreq.min else None,
                "max_mhz": round(cfreq.max, 1) if cfreq.max else None,
            }
    except Exception:
        pass

    ctx_switches = 0
    interrupts = 0
    try:
        cstats = psutil.cpu_stats()
        ctx_switches = cstats.ctx_switches
        interrupts = cstats.interrupts
    except Exception:
        pass

    return {
        "percent": cpu_percent,
        "per_cpu_percent": per_cpu,
        "cores_logical": cpu_logical,
        "cores_physical": cpu_physical,
        "load_avg": load_avg,
        "normalized_load_percent": normalized_load_pct,
        "frequency": freq_info,
        "context_switches": ctx_switches,
        "interrupts": interrupts,
    }


def _get_igpu_stats() -> list[dict[str, Any]]:
    """Collect Integrated GPU (iGPU) telemetry from Linux DRM sysfs."""
    import glob
    igpus: list[dict[str, Any]] = []

    for card_path in glob.glob("/sys/class/drm/card*"):
        card_name = os.path.basename(card_path)
        if "-" in card_name:
            continue
        dev_path = os.path.join(card_path, "device")
        if not os.path.isdir(dev_path):
            continue

        vendor = ""
        vendor_file = os.path.join(dev_path, "vendor")
        if os.path.exists(vendor_file):
            try:
                with open(vendor_file, "r") as f:
                    vendor = f.read().strip().lower()
            except Exception:
                pass

        # Skip NVIDIA discrete GPUs in this helper since nvidia-smi handles dGPU
        if vendor == "0x10de":
            continue

        driver = ""
        driver_link = os.path.join(dev_path, "driver")
        if os.path.islink(driver_link):
            try:
                driver = os.path.basename(os.readlink(driver_link))
            except Exception:
                pass

        slot = ""
        uevent_file = os.path.join(dev_path, "uevent")
        if os.path.exists(uevent_file):
            try:
                with open(uevent_file, "r") as f:
                    for line in f:
                        if line.startswith("PCI_SLOT_NAME="):
                            slot = line.split("=", 1)[1].strip()
            except Exception:
                pass

        name = "Integrated Graphics (iGPU)"
        if vendor == "0x8086" or "i915" in driver or "xe" in driver:
            name = "Intel UHD Graphics / Iris Xe"
        elif vendor == "0x1002" or "amdgpu" in driver or "radeon" in driver:
            name = "AMD Radeon Graphics"

        if slot and shutil.which("lspci"):
            try:
                lspci_out = subprocess.run(["lspci", "-s", slot], capture_output=True, text=True, timeout=1).stdout
                if ":" in lspci_out:
                    clean_name = lspci_out.split(":", 2)[-1].strip()
                    if "controller:" in clean_name.lower():
                        clean_name = clean_name.split("controller:", 1)[-1].strip()
                    name = clean_name
            except Exception:
                pass

        freq_info = {}
        for freq_key, fname in [
            ("cur_mhz", "gt_cur_freq_mhz"),
            ("act_mhz", "gt_act_freq_mhz"),
            ("min_mhz", "gt_min_freq_mhz"),
            ("max_mhz", "gt_max_freq_mhz"),
            ("boost_mhz", "gt_boost_freq_mhz"),
            ("rp0_mhz", "gt_RP0_freq_mhz"),
            ("rp1_mhz", "gt_RP1_freq_mhz"),
            ("rpn_mhz", "gt_RPn_freq_mhz"),
        ]:
            ffile = os.path.join(card_path, fname)
            if os.path.exists(ffile):
                try:
                    with open(ffile, "r") as f:
                        freq_info[freq_key] = int(f.read().strip())
                except Exception:
                    pass

        engines = []
        engine_dir = os.path.join(card_path, "engine")
        if os.path.isdir(engine_dir):
            try:
                engines = sorted(os.listdir(engine_dir))
            except Exception:
                pass

        pwr_status = "active"
        pwr_file = os.path.join(dev_path, "power", "runtime_status")
        if os.path.exists(pwr_file):
            try:
                with open(pwr_file, "r") as f:
                    pwr_status = f.read().strip()
            except Exception:
                pass

        cur_f = freq_info.get("cur_mhz") or freq_info.get("act_mhz")
        min_f = freq_info.get("min_mhz") or freq_info.get("rpn_mhz") or 350
        max_f = freq_info.get("max_mhz") or freq_info.get("rp0_mhz") or 1450
        util_pct = 0.0
        if cur_f and max_f and max_f > min_f:
            util_pct = round(max(0.0, min(100.0, ((cur_f - min_f) / (max_f - min_f)) * 100.0)), 1)

        igpus.append({
            "card": card_name,
            "name": name,
            "vendor": "Intel" if vendor == "0x8086" else ("AMD" if vendor == "0x1002" else "Integrated"),
            "vendor_id": vendor,
            "driver": driver,
            "pci_slot": slot,
            "power_status": pwr_status,
            "utilization_percent": util_pct,
            "frequency": freq_info,
            "engines": engines,
        })

    return igpus


def _get_gpu_stats() -> dict[str, Any]:
    """Collect NVIDIA discrete GPU and Intel/AMD integrated GPU hardware metrics."""
    dgpus: list[dict[str, Any]] = []
    driver_version = None

    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,driver_version,temperature.gpu,utilization.gpu,utilization.memory,memory.total,memory.free,memory.used,power.draw,fan.speed",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0:
                for line in res.stdout.strip().splitlines():
                    if not line.strip():
                        continue
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 9:
                        driver_version = parts[2]
                        def to_float(val: str, d: float = 0.0) -> float:
                            try:
                                return float(val)
                            except Exception:
                                return d

                        def to_int(val: str, d: int = 0) -> int:
                            try:
                                return int(float(val))
                            except Exception:
                                return d

                        total_mb = to_float(parts[6])
                        free_mb = to_float(parts[7])
                        used_mb = to_float(parts[8])
                        mem_pct = round((used_mb / total_mb * 100.0), 1) if total_mb > 0 else 0.0

                        dgpus.append({
                            "index": to_int(parts[0]),
                            "name": parts[1],
                            "type": "discrete",
                            "driver_version": parts[2],
                            "temperature_c": to_float(parts[3]),
                            "utilization_gpu_percent": to_float(parts[4]),
                            "utilization_mem_percent": to_float(parts[5]),
                            "memory_total_mb": total_mb,
                            "memory_total_human": f"{total_mb/1024.0:.1f} GB" if total_mb >= 1024 else f"{total_mb:.0f} MB",
                            "memory_used_mb": used_mb,
                            "memory_used_human": f"{used_mb/1024.0:.1f} GB" if used_mb >= 1024 else f"{used_mb:.0f} MB",
                            "memory_free_mb": free_mb,
                            "memory_free_human": f"{free_mb/1024.0:.1f} GB" if free_mb >= 1024 else f"{free_mb:.0f} MB",
                            "memory_percent": mem_pct,
                            "power_draw_w": to_float(parts[9]) if len(parts) > 9 else None,
                            "fan_speed": parts[10] if len(parts) > 10 and parts[10] != "[N/A]" else "N/A",
                        })
        except Exception as exc:
            log.debug("nvidia_smi_query_failed", error=str(exc))

    igpus = _get_igpu_stats()

    torch_cuda = False
    try:
        import torch
        torch_cuda = torch.cuda.is_available()
    except Exception:
        pass

    all_devices = list(dgpus)
    for ig in igpus:
        all_devices.append({
            "index": len(all_devices),
            "name": ig["name"],
            "type": "integrated",
            "driver_version": ig["driver"],
            "temperature_c": None,
            "utilization_gpu_percent": ig["utilization_percent"],
            "utilization_mem_percent": 0.0,
            "memory_total_human": "Shared RAM",
            "memory_used_human": "Dynamic",
            "memory_free_human": "—",
            "memory_percent": 0.0,
            "power_draw_w": None,
            "fan_speed": "N/A",
            "frequency_mhz": ig["frequency"].get("cur_mhz"),
            "power_status": ig["power_status"],
            "engines": ig["engines"],
        })

    return {
        "available": len(all_devices) > 0 or torch_cuda,
        "device_count": len(all_devices),
        "driver_version": driver_version,
        "torch_cuda_available": torch_cuda,
        "discrete": dgpus,
        "integrated": igpus,
        "devices": all_devices,
    }


def _get_memory_stats() -> dict[str, Any]:
    """Collect RAM, Cached, Buffers, and Swap memory telemetry."""
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "total_bytes": vm.total,
        "total_human": _format_bytes(vm.total),
        "used_bytes": vm.used,
        "used_human": _format_bytes(vm.used),
        "available_bytes": vm.available,
        "available_human": _format_bytes(vm.available),
        "free_bytes": getattr(vm, "free", 0),
        "free_human": _format_bytes(getattr(vm, "free", 0)),
        "cached_human": _format_bytes(getattr(vm, "cached", 0)),
        "buffers_human": _format_bytes(getattr(vm, "buffers", 0)),
        "percent": vm.percent,
        "swap_total_human": _format_bytes(swap.total),
        "swap_used_human": _format_bytes(swap.used),
        "swap_free_human": _format_bytes(swap.free),
        "swap_percent": swap.percent,
    }


def _get_storage_stats() -> dict[str, Any]:
    """Collect Disk space across mount points and Disk I/O throughput."""
    mounts = []
    checked_paths = set()

    for path in ["/", os.getcwd()]:
        if path in checked_paths:
            continue
        checked_paths.add(path)
        try:
            du = psutil.disk_usage(path)
            mounts.append({
                "path": path,
                "total_bytes": du.total,
                "total_human": _format_bytes(du.total),
                "used_bytes": du.used,
                "used_human": _format_bytes(du.used),
                "free_bytes": du.free,
                "free_human": _format_bytes(du.free),
                "percent": du.percent,
            })
        except Exception:
            pass

    primary = mounts[0] if mounts else {
        "path": "/",
        "total_human": "0 B",
        "used_human": "0 B",
        "free_human": "0 B",
        "percent": 0.0,
    }

    io_stats = {}
    try:
        dio = psutil.disk_io_counters()
        if dio:
            io_stats = {
                "read_bytes_human": _format_bytes(dio.read_bytes),
                "write_bytes_human": _format_bytes(dio.write_bytes),
                "read_count": dio.read_count,
                "write_count": dio.write_count,
                "busy_time_ms": dio.busy_time,
            }
    except Exception:
        pass

    return {
        "primary": primary,
        "mounts": mounts,
        "io": io_stats,
    }


def _get_network_stats() -> dict[str, Any]:
    """Collect host network I/O traffic."""
    try:
        nio = psutil.net_io_counters()
        return {
            "bytes_sent_human": _format_bytes(nio.bytes_sent),
            "bytes_recv_human": _format_bytes(nio.bytes_recv),
            "packets_sent": nio.packets_sent,
            "packets_recv": nio.packets_recv,
            "errors_total": nio.errin + nio.errout,
            "drops_total": nio.dropin + nio.dropout,
        }
    except Exception:
        return {
            "bytes_sent_human": "0 B",
            "bytes_recv_human": "0 B",
            "packets_sent": 0,
            "packets_recv": 0,
            "errors_total": 0,
            "drops_total": 0,
        }


def _get_fans_stats() -> dict[str, Any]:
    """Collect hardware cooling fan speeds (RPM, labels)."""
    fans_list = []
    try:
        if hasattr(psutil, "sensors_fans"):
            fans_dict = psutil.sensors_fans()
            for chip, sfan_list in fans_dict.items():
                for s in sfan_list:
                    fans_list.append({
                        "label": s.label or f"{chip}_fan",
                        "current_rpm": s.current,
                        "chip": chip,
                    })
    except Exception:
        pass

    if not fans_list:
        for p in glob.glob("/sys/class/hwmon/hwmon*/fan*_input"):
            try:
                with open(p) as f:
                    rpm = int(f.read().strip())
                    label = os.path.basename(p).replace("_input", "")
                    fans_list.append({
                        "label": label,
                        "current_rpm": rpm,
                        "chip": "hwmon",
                    })
            except Exception:
                pass

    return {
        "available": len(fans_list) > 0,
        "count": len(fans_list),
        "fans": fans_list,
        "primary_rpm": fans_list[0]["current_rpm"] if fans_list else None,
    }


def _get_docker_stats_sync() -> dict[str, Any]:
    """Collect Docker daemon state and live container CPU/memory load."""
    if not shutil.which("docker"):
        return {"available": False, "reason": "docker command not installed"}

    try:
        ps_res = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if ps_res.returncode != 0:
            return {"available": False, "reason": ps_res.stderr.strip() or "docker daemon not reachable"}

        containers_raw = [json.loads(line) for line in ps_res.stdout.strip().splitlines() if line.strip()]

        stats_map: dict[str, dict[str, Any]] = {}
        try:
            st_res = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if st_res.returncode == 0:
                for line in st_res.stdout.strip().splitlines():
                    if line.strip():
                        s = json.loads(line)
                        name_key = s.get("Name", "").strip("/")
                        id_key = s.get("ID", "").strip()
                        if name_key:
                            stats_map[name_key] = s
                        if id_key:
                            stats_map[id_key] = s
        except Exception:
            pass

        containers = []
        for c in containers_raw:
            name = c.get("Names", "").strip("/")
            cid = c.get("ID", "")
            st = stats_map.get(name) or stats_map.get(cid) or {}

            cpu_raw = st.get("CPUPerc", "0.0%").replace("%", "").strip()
            try:
                cpu_p = float(cpu_raw)
            except Exception:
                cpu_p = 0.0

            mem_raw = st.get("MemPerc", "0.0%").replace("%", "").strip()
            try:
                mem_p = float(mem_raw)
            except Exception:
                mem_p = 0.0

            containers.append({
                "id": cid[:12],
                "name": name,
                "image": c.get("Image", ""),
                "status": c.get("Status", ""),
                "state": c.get("State", ""),
                "health": c.get("HealthStatus", "none"),
                "ports": c.get("Ports", ""),
                "running_for": c.get("RunningFor", ""),
                "cpu_percent": cpu_p,
                "memory_percent": mem_p,
                "memory_usage": st.get("MemUsage", "—"),
                "net_io": st.get("NetIO", "—"),
                "block_io": st.get("BlockIO", "—"),
                "pids": st.get("PIDs", "—"),
            })

        running_cnt = sum(1 for c in containers if c["state"] == "running")
        paused_cnt = sum(1 for c in containers if c["state"] == "paused")
        stopped_cnt = sum(1 for c in containers if c["state"] in ("exited", "dead"))

        return {
            "available": True,
            "total_containers": len(containers),
            "running_containers": running_cnt,
            "paused_containers": paused_cnt,
            "stopped_containers": stopped_cnt,
            "containers": containers,
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


async def _get_docker_stats() -> dict[str, Any]:
    """Asynchronously collect Docker container metrics in a thread pool."""
    return await asyncio.to_thread(_get_docker_stats_sync)


async def _get_ollama_stats() -> dict[str, Any]:
    """Collect Ollama local inference server telemetry and downloaded model catalog."""
    ollama_url = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            ver_res = await client.get(f"{ollama_url}/api/version")
            ping_ms = round((time.perf_counter() - t0) * 1000, 2)
            version = ver_res.json().get("version", "unknown") if ver_res.status_code == 200 else None

            tags_res = await client.get(f"{ollama_url}/api/tags")
            models = []
            if tags_res.status_code == 200:
                raw_models = tags_res.json().get("models", [])
                for m in raw_models:
                    sz = m.get("size", 0)
                    sz_h = f"{sz / (1024**3):.1f} GB" if sz >= 1024**3 else f"{sz / (1024**2):.0f} MB"
                    details = m.get("details", {})
                    models.append({
                        "name": m.get("name"),
                        "model": m.get("model"),
                        "size_bytes": sz,
                        "size_human": sz_h,
                        "parameter_size": details.get("parameter_size", "—"),
                        "quantization_level": details.get("quantization_level", "—"),
                        "family": details.get("family", "—"),
                        "context_length": details.get("context_length", None),
                        "capabilities": m.get("capabilities", []),
                        "modified_at": m.get("modified_at"),
                    })

            ps_res = await client.get(f"{ollama_url}/api/ps")
            active_models = []
            if ps_res.status_code == 200:
                for am in ps_res.json().get("models", []):
                    vram_sz = am.get("size_vram", 0)
                    active_models.append({
                        "name": am.get("name"),
                        "size_vram_human": f"{vram_sz / (1024**3):.1f} GB" if vram_sz >= 1024**3 else f"{vram_sz / (1024**2):.0f} MB",
                        "expires_at": am.get("expires_at"),
                    })

            return {
                "status": "online",
                "ping_ms": ping_ms,
                "version": version,
                "endpoint": ollama_url,
                "models_count": len(models),
                "models": models,
                "active_models": active_models,
            }
    except Exception as exc:
        return {
            "status": "offline",
            "ping_ms": None,
            "version": None,
            "endpoint": ollama_url,
            "models_count": 0,
            "models": [],
            "active_models": [],
            "error": str(exc),
        }


async def _get_minio_stats() -> dict[str, Any]:
    """Collect MinIO S3 object storage service status."""
    minio_ports = [9002, 9001, 9000]
    for port in minio_ports:
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                res = await client.get(f"http://127.0.0.1:{port}/minio/health/live")
                ping_ms = round((time.perf_counter() - t0) * 1000, 2)
                if res.status_code == 200:
                    return {
                        "status": "connected",
                        "ping_ms": ping_ms,
                        "endpoint": f"http://127.0.0.1:{port}",
                        "bucket": "defense",
                    }
        except Exception:
            pass

    return {
        "status": "unreachable",
        "ping_ms": None,
        "endpoint": "http://127.0.0.1:9000",
        "bucket": "defense",
    }


def _get_agents_stats() -> dict[str, Any]:
    """Collect Agent Intelligence Registry definitions and tool assignments."""
    agents_list = []
    for name, agent in AGENT_REGISTRY.items():
        agents_list.append({
            "name": name,
            "description": agent.description,
            "llm_role": agent.llm_role,
            "max_tool_calls": agent.max_tool_calls,
            "tools_count": len(agent.tools),
            "tools": agent.tools,
        })
    return {
        "total_agents": len(agents_list),
        "agents": agents_list,
    }


def _get_host_stats() -> dict[str, Any]:
    """Collect host OS and hardware resource metrics."""
    cpu_stats = _get_cpu_stats()
    mem_stats = _get_memory_stats()
    storage_stats = _get_storage_stats()
    gpu_stats = _get_gpu_stats()
    net_stats = _get_network_stats()
    fan_stats = _get_fans_stats()

    proc_cpu = 0.0
    proc_mem_rss = 0
    proc_mem_vms = 0
    proc_threads = 1
    proc_fds = 0
    proc_status = "running"
    try:
        proc_cpu = round(_PROCESS.cpu_percent(interval=None), 1)
        mem_info = _PROCESS.memory_info()
        proc_mem_rss = mem_info.rss
        proc_mem_vms = mem_info.vms
        proc_threads = _PROCESS.num_threads()
        if hasattr(_PROCESS, "num_fds"):
            proc_fds = _PROCESS.num_fds()
        proc_status = _PROCESS.status()
    except Exception as exc:
        log.debug("process_metrics_partial", error=str(exc))

    uptime_sec = time.time() - _PROCESS_START_TIME

    total_proc_count = 0
    try:
        total_proc_count = len(psutil.pids())
    except Exception:
        pass

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "uptime_seconds": round(uptime_sec, 1),
        "uptime_human": _format_duration(uptime_sec),
        "boot_time": datetime.datetime.fromtimestamp(psutil.boot_time(), tz=datetime.timezone.utc).isoformat(),
        "process_count": total_proc_count,
        "cpu": cpu_stats,
        "memory": mem_stats,
        "disk": storage_stats["primary"],
        "storage": storage_stats,
        "gpu": gpu_stats,
        "network": net_stats,
        "fans": fan_stats,
        "process": {
            "pid": os.getpid(),
            "cpu_percent": proc_cpu,
            "memory_rss_bytes": proc_mem_rss,
            "memory_rss_human": _format_bytes(proc_mem_rss),
            "memory_vms_bytes": proc_mem_vms,
            "memory_vms_human": _format_bytes(proc_mem_vms),
            "threads_count": proc_threads,
            "fds_count": proc_fds,
            "status": proc_status,
        },
    }


async def _get_postgres_stats(db: AsyncSession) -> dict[str, Any]:
    """Collect PostgreSQL database telemetry safely with automatic transaction recovery."""
    pg_stats: dict[str, Any] = {
        "status": "unreachable",
        "ping_ms": None,
        "version": None,
        "database": None,
        "db_size": None,
        "active_connections": 0,
        "pool": {
            "size": engine.pool.size() if hasattr(engine, "pool") else 10,
            "checked_in": engine.pool.checkedin() if hasattr(engine, "pool") else 0,
            "checked_out": engine.pool.checkedout() if hasattr(engine, "pool") else 0,
            "overflow": engine.pool.overflow() if hasattr(engine, "pool") else 0,
        },
        "tables": {},
        "error": None,
    }

    t0 = time.perf_counter()
    try:
        # If the session has an uncommitted or aborted transaction, roll it back first
        if db is not None:
            try:
                await db.rollback()
            except Exception:
                pass
            session_to_use = db
        else:
            session_to_use = None

        if session_to_use is not None:
            ver_row = (await session_to_use.execute(text("SELECT version()"))).scalar()
            dbname = (await session_to_use.execute(text("SELECT current_database()"))).scalar()
            dbsize = (await session_to_use.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))"))).scalar()
            conn_count = (await session_to_use.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"))).scalar()
            tbl_res = await session_to_use.execute(text("SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC"))
        else:
            async with engine.connect() as conn:
                ver_row = (await conn.execute(text("SELECT version()"))).scalar()
                dbname = (await conn.execute(text("SELECT current_database()"))).scalar()
                dbsize = (await conn.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))"))).scalar()
                conn_count = (await conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"))).scalar()
                tbl_res = await conn.execute(text("SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC"))

        ping_ms = round((time.perf_counter() - t0) * 1000, 2)
        pg_stats["ping_ms"] = ping_ms
        pg_stats["version"] = ver_row
        pg_stats["status"] = "connected"
        pg_stats["database"] = dbname
        pg_stats["db_size"] = dbsize
        pg_stats["active_connections"] = int(conn_count or 0)
        pg_stats["tables"] = {r[0]: int(r[1] or 0) for r in tbl_res.fetchall()}

    except Exception as exc:
        pg_stats["status"] = "unreachable"
        pg_stats["error"] = str(exc)
        log.warning("system_monitor_postgres_failed", error=str(exc))

    return pg_stats


def _as_str(v: Any) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def _get_redis_stats(redis: aioredis.Redis) -> dict[str, Any]:
    """Collect Redis server telemetry and stream status."""
    r_stats: dict[str, Any] = {
        "status": "unreachable",
        "ping_ms": None,
        "version": None,
        "mode": "standalone",
        "uptime_seconds": 0,
        "uptime_human": "0s",
        "connected_clients": 0,
        "used_memory_human": "0 B",
        "peak_memory_human": "0 B",
        "mem_fragmentation_ratio": 1.0,
        "total_commands_processed": 0,
        "ops_per_sec": 0,
        "total_keys": 0,
        "streams": [],
        "error": None,
    }

    t0 = time.perf_counter()
    try:
        pong = await redis.ping()
        ping_ms = round((time.perf_counter() - t0) * 1000, 2)
        if pong:
            r_stats["status"] = "connected"
            r_stats["ping_ms"] = ping_ms

        info = await redis.info()
        r_stats["version"] = info.get("redis_version", "unknown")
        r_stats["mode"] = info.get("redis_mode", "standalone")
        uptime = int(info.get("uptime_in_seconds", 0))
        r_stats["uptime_seconds"] = uptime
        r_stats["uptime_human"] = _format_duration(uptime)
        r_stats["connected_clients"] = int(info.get("connected_clients", 0))
        r_stats["used_memory_human"] = info.get("used_memory_human", "0 B")
        r_stats["peak_memory_human"] = info.get("used_memory_peak_human", "0 B")
        r_stats["mem_fragmentation_ratio"] = float(info.get("mem_fragmentation_ratio", 1.0))
        r_stats["total_commands_processed"] = int(info.get("total_commands_processed", 0))
        r_stats["ops_per_sec"] = int(info.get("instantaneous_ops_per_sec", 0))

        try:
            r_stats["total_keys"] = int(await redis.dbsize())
        except Exception:
            pass

        stream_diagnostics = []
        for key, spec in streams.ALL.items():
            s_name = spec.name
            s_group = spec.group
            length = 0
            pending = 0
            lag = 0
            dlq_len = 0

            try:
                length = int(await redis.xlen(s_name))
            except Exception:
                pass

            try:
                for g in await redis.xinfo_groups(s_name):
                    gname = _as_str(g.get("name", g.get(b"name", "")))
                    if gname == s_group:
                        pending = int(g.get("pending", g.get(b"pending", 0)) or 0)
                        glag = g.get("lag", g.get(b"lag", None))
                        if glag is not None:
                            lag = int(glag)
            except Exception:
                pass

            try:
                dlq_len = int(await redis.xlen(f"{s_name}:dlq"))
            except Exception:
                pass

            stream_diagnostics.append({
                "key": key,
                "stream": s_name,
                "group": s_group,
                "length": length,
                "pending": pending,
                "lag": lag,
                "dlq_length": dlq_len,
            })

        r_stats["streams"] = stream_diagnostics

    except Exception as exc:
        r_stats["status"] = "unreachable"
        r_stats["error"] = str(exc)
        log.warning("system_monitor_redis_failed", error=str(exc))

    return r_stats


async def _get_pipeline_stats(redis: aioredis.Redis, db: AsyncSession, tenant_id: str = "default") -> dict[str, Any]:
    """Collect pipeline stage and processing volume telemetry."""
    from defense.services.api.routers.pipeline import collect_stats
    try:
        return await collect_stats(redis, db, tenant_id=tenant_id)
    except Exception as exc:
        log.warning("system_monitor_pipeline_stats_failed", error=str(exc))
        return {
            "stages": [],
            "completed": 0,
            "total_processed": 0,
            "llm_routed": 0,
            "dlq_total": 0,
            "in_flight_total": 0,
            "backlog_total": 0,
        }


async def _get_ai_runtime_stats(redis: aioredis.Redis, db: AsyncSession) -> dict[str, Any]:
    """Collect LLM and NLP model runtime configuration & usage telemetry."""
    config = get_settings()

    override_val = None
    try:
        raw = await redis.get("config:llm_backend")
        if raw:
            override_val = _as_str(raw)
    except Exception:
        pass
    active_backend = override_val or config.llm_backend

    llm_client = LLMClient()
    circuit_state = getattr(llm_client, "_circuit_breaker", None)
    circuit_status = circuit_state.state if circuit_state else "closed"

    models_map = {}
    for role in sorted(VALID_ROLES):
        try:
            models_map[role] = llm_client.default_model(role, active_backend)
        except Exception:
            models_map[role] = "unknown"

    nlp_override = None
    try:
        raw_nlp = await redis.get("config:sentiment_model")
        if raw_nlp:
            nlp_override = _as_str(raw_nlp)
    except Exception:
        pass

    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    try:
        total_tokens = int(await redis.get("usage:tokens:total") or 0)
        prompt_tokens = int(await redis.get("usage:tokens:prompt") or 0)
        completion_tokens = int(await redis.get("usage:tokens:completion") or 0)
    except Exception:
        pass

    estimated_cost = 0.0
    baseline_market_cost = (total_tokens / 1000.0) * 0.005
    if active_backend == "groq":
        estimated_cost = (total_tokens / 1000.0) * 0.0001
    elif active_backend == "local":
        estimated_cost = 0.0

    savings_usd = max(0.0, baseline_market_cost - estimated_cost)
    savings_pct = round((savings_usd / baseline_market_cost * 100.0), 1) if baseline_market_cost > 0 else 0.0

    return {
        "llm_backend": active_backend,
        "llm_backend_source": "override" if override_val else "default",
        "default_backend": config.llm_backend,
        "circuit_breaker": circuit_status,
        "models": models_map,
        "nlp": {
            "mode": "forced" if nlp_override else "auto",
            "sentiment_model": nlp_override or "auto (language-routed)",
            "default_key": sentiment_models.DEFAULT_KEY,
            "options": list(sentiment_models.valid_keys()),
        },
        "usage": {
            "total_tokens": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_usd": round(estimated_cost, 4),
            "baseline_market_cost_usd": round(baseline_market_cost, 4),
            "savings_usd": round(savings_usd, 4),
            "savings_pct": savings_pct,
        },
    }


def _determine_overall_health(host: dict, pg: dict, redis: dict, pipe: dict, docker: dict, ollama: dict) -> str:
    """Compute overall system health: healthy, degraded, or unhealthy."""
    if pg.get("status") != "connected" or redis.get("status") != "connected":
        return "unhealthy"

    cpu_p = host.get("cpu", {}).get("percent", 0)
    mem_p = host.get("memory", {}).get("percent", 0)
    disk_p = host.get("disk", {}).get("percent", 0)
    dlq_count = pipe.get("dlq_total", 0)

    if cpu_p > 90 or mem_p > 92 or disk_p > 95 or dlq_count > 50 or ollama.get("status") == "offline":
        return "degraded"

    return "healthy"


async def collect_full_system_snapshot(
    request: Request | None,
    db: AsyncSession,
    redis: aioredis.Redis,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Assemble complete system monitor telemetry."""
    host = _get_host_stats()
    docker = await _get_docker_stats()
    ollama = await _get_ollama_stats()
    minio = await _get_minio_stats()
    agents = _get_agents_stats()
    pg = await _get_postgres_stats(db)
    r = await _get_redis_stats(redis)
    pipe = await _get_pipeline_stats(redis, db, tenant_id=tenant_id)
    ai = await _get_ai_runtime_stats(redis, db)

    overall_status = _determine_overall_health(host, pg, r, pipe, docker, ollama)
    config = get_settings()

    app_routes_count = 0
    if request and request.app and hasattr(request.app, "routes"):
        app_routes_count = len(request.app.routes)

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": overall_status,
        "host": host,
        "gpu": host["gpu"],
        "fans": host["fans"],
        "network": host["network"],
        "docker": docker,
        "ollama": ollama,
        "minio": minio,
        "agents": agents,
        "datastores": {
            "postgres": pg,
            "redis": r,
            "minio": minio,
        },
        "pipeline": pipe,
        "ai_runtime": ai,
        "api_service": {
            "title": "Selective Intelligence API",
            "version": "1.0.0",
            "environment": app_env(),
            "rate_limit_enabled": config.rate_limit_enabled,
            "rate_limit_per_min": config.rate_limit_per_min,
            "routes_count": app_routes_count,
        },
    }


@router.get("/stats", summary="Comprehensive backend system telemetry snapshot")
async def get_system_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Returns detailed real-time host, datastores, queue, and model metrics."""
    tenant_id = current_user.get("tenant_id", "default")
    return await collect_full_system_snapshot(request, db, redis, tenant_id=tenant_id)


@router.get("/health", summary="Quick health and component status check")
async def get_system_quick_health(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    """Lightweight health check returning status and core component latencies."""
    t_start = time.perf_counter()
    pg_ok = False
    redis_ok = False
    pg_latency = 0.0
    redis_latency = 0.0

    try:
        t0 = time.perf_counter()
        await db.execute(text("SELECT 1"))
        pg_latency = round((time.perf_counter() - t0) * 1000, 2)
        pg_ok = True
    except Exception:
        pass

    try:
        t0 = time.perf_counter()
        await redis.ping()
        redis_latency = round((time.perf_counter() - t0) * 1000, 2)
        redis_ok = True
    except Exception:
        pass

    status_str = "healthy" if (pg_ok and redis_ok) else "degraded" if (pg_ok or redis_ok) else "unhealthy"
    return {
        "status": status_str,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "postgres": {"status": "ok" if pg_ok else "down", "latency_ms": pg_latency},
        "redis": {"status": "ok" if redis_ok else "down", "latency_ms": redis_latency},
        "duration_ms": round((time.perf_counter() - t_start) * 1000, 2),
    }


async def _system_sse(
    request: Request,
    redis: aioredis.Redis,
    db: AsyncSession,
    tenant_id: str = "default",
    poll_seconds: float = 1.0,
    max_seconds: float = 600.0,
) -> AsyncGenerator[str, None]:
    """Poll system state and stream as SSE events."""
    yield "event: connected\ndata: {}\n\n"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_seconds
    try:
        while loop.time() < deadline:
            stats = await collect_full_system_snapshot(request, db, redis, tenant_id=tenant_id)
            yield f"event: stats\ndata: {json.dumps(stats, default=str)}\n\n"
            await asyncio.sleep(poll_seconds)
        yield "event: timeout\ndata: {}\n\n"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("system_sse_error", error=str(exc))
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"


@router.get(
    "/stream",
    summary="Stream live system monitor telemetry via SSE",
    response_class=StreamingResponse,
)
async def system_stream(
    request: Request,
    redis: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events: real-time system metrics frame pushed periodically."""
    tenant_id = current_user.get("tenant_id", "default")
    return StreamingResponse(
        _system_sse(request, redis, db, tenant_id=tenant_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
