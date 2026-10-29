#!/usr/bin/env python3
"""Persistenter Runner fuer gebuendelte Beitrags-Exporte.

Flask legt Jobs und einen unveraenderlichen Eingabe-Snapshot unter
WORKSPACES_DIR/.platform/render_jobs an. Dieser Prozess haelt einen globalen
Datei-Lock und verarbeitet die wartenden Jobs in Erstellungsreihenfolge. Damit
bleiben Status und Queue ueber mehrere Server-Prozesse hinweg konsistent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_WERKZEUGE = str(Path(__file__).resolve().parents[1] / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
from dateisperre import sperrdatei, sperren  # noqa: E402
from slide_bereinigung import ERLAUBTE_HOSTS, snapshot_bereinigen  # noqa: E402


JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
FINAL_STATES = {"done", "error"}
HEARTBEAT_INTERVAL_S = 2.0
SLIDE_TIMEOUT_S = 5 * 60
JOB_TIMEOUT_S = 20 * 60
MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
RETAIN_PER_POST = 2
RETAIN_PER_WORKSPACE = 20
RETAIN_WORKSPACE_BYTES = 5 * 1024 * 1024 * 1024
ALLOWED_NETWORK_HOSTS = ERLAUBTE_HOSTS
VIDEO_OUTPUT_SCALE = 2
VIDEO_CAPTURE_SCALE = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def update_status(job_dir: Path, **changes) -> dict:
    path = job_dir / "status.json"
    status = read_json(path)
    status.update(changes)
    status["updated_at"] = _utc_now()
    status["heartbeat_epoch"] = time.time()
    atomic_write_json(path, status)
    return status


def _directory_bytes(root: Path) -> int:
    total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = [
            name for name in dirnames if not (directory_path / name).is_symlink()
        ]
        for name in filenames:
            path = directory_path / name
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


def _cleanup_retained_jobs(job_dir: Path, spec: dict) -> None:
    queue_root = job_dir.parent
    workspace = str(spec.get("workspace") or "")
    kind = str(spec.get("kind") or "")
    post_slug = str(spec.get("post_slug") or "")
    if not workspace or kind not in ("drafts", "archive") or not post_slug:
        return
    finals = []
    for entry in queue_root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or not JOB_ID_RE.fullmatch(entry.name):
            continue
        status = read_json(entry / "status.json")
        if (
            status.get("type") == "post_export"
            and status.get("workspace") == workspace
            and status.get("state") in FINAL_STATES
        ):
            finals.append((entry, status))
    finals.sort(
        key=lambda item: (str(item[1].get("created_at") or ""), item[0].name),
        reverse=True,
    )
    keep_workspace = set()
    retained_bytes = 0
    for entry, status in finals:
        try:
            size = max(0, int(status.get("job_size") or 0))
        except (TypeError, ValueError):
            size = 0
        size = size or _directory_bytes(entry)
        if (
            len(keep_workspace) < RETAIN_PER_WORKSPACE
            and (retained_bytes + size <= RETAIN_WORKSPACE_BYTES or not keep_workspace)
        ):
            keep_workspace.add(entry.name)
            retained_bytes += size
    same_post = [
        (entry, status) for entry, status in finals
        if status.get("kind") == kind and status.get("post_slug") == post_slug
    ]
    keep_post = {entry.name for entry, _ in same_post[:RETAIN_PER_POST]}
    for entry, status in finals:
        if entry == job_dir:
            continue
        outside_workspace_limit = entry.name not in keep_workspace
        outside_post_limit = (
            status.get("kind") == kind
            and status.get("post_slug") == post_slug
            and entry.name not in keep_post
        )
        if not outside_workspace_limit and not outside_post_limit:
            continue
        try:
            with sperrdatei(entry / "download.lock") as download_lock:
                sperren(download_lock, warten=False)
                # Unter Windows laesst sich die offene Sperrdatei nicht
                # loeschen, sie faellt erst nach dem Schliessen weg.
                for teil in entry.iterdir():
                    if teil.name == "download.lock":
                        continue
                    if teil.is_dir() and not teil.is_symlink():
                        shutil.rmtree(teil)
                    else:
                        teil.unlink()
            shutil.rmtree(entry)
        except (BlockingIOError, OSError):
            continue


def _safe_output_stem(name: str, index: int) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_") or "slide"
    return f"{index:02d}_{cleaned[:80]}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_snapshot_html(path: Path) -> None:
    """Entfernt aktive Inhalte (engine-shared/tools/slide_bereinigung.py)."""
    snapshot_bereinigen(path, ALLOWED_NETWORK_HOSTS)


def _kill_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # Windows kennt keine Prozessgruppen per Signal: taskkill beendet den
        # Renderer samt Browser und ffmpeg (/T) ohne Rueckfrage (/F).
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def eigene_prozessgruppe() -> dict:
    """Popen-Argumente, damit der Kindprozess samt Nachkommen beendbar bleibt."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _run_renderer(
    job_dir: Path, command: list[str], current: str, timeout_s: float = SLIDE_TIMEOUT_S
) -> tuple[int, str]:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **eigene_prozessgruppe(),
    )
    # Beide Pipes waehrend der Laufzeit leerlaufen lassen. Wir warten hier in
    # einer Heartbeat-Schleife und nicht in communicate(); ohne mitlesende
    # Threads wuerde der Renderer bei gesprächigem Output (~64 KB Pipe-Puffer)
    # blockieren, bis das Timeout zuschlaegt.
    chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def mitlesen(stream, key: str) -> None:
        try:
            for line in stream:
                chunks[key].append(line)
                # Puffer klein halten: nur das Ende der Ausgabe ist relevant.
                if len(chunks[key]) > 400:
                    del chunks[key][:200]
        except (OSError, ValueError):
            pass

    readers = [
        threading.Thread(target=mitlesen, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=mitlesen, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    try:
        while proc.poll() is None:
            if time.monotonic() - started > timeout_s:
                _kill_process_group(proc)
                return 124, f"Das Rendern von {current} hat zu lange gedauert."
            update_status(job_dir, state="running", current=current)
            time.sleep(HEARTBEAT_INTERVAL_S)
        for reader in readers:
            reader.join(timeout=10)
        stderr = "".join(chunks["stderr"])
        stdout = "".join(chunks["stdout"])
        detail = (stderr or stdout or "").strip()[-1500:]
        return proc.returncode, detail
    finally:
        _kill_process_group(proc)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _job_sources(spec: dict) -> list[dict]:
    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Der Job enthält keine Slide-Quellen.")
    clean = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Die Quellenliste ist ungültig.")
        name = str(source.get("name") or "")
        if not name.endswith(".html") or "/" in name or "\\" in name:
            raise ValueError("Ein Quelldateiname ist ungültig.")
        input_name = str(source.get("input_name") or name)
        if not input_name.endswith(".html") or "/" in input_name or "\\" in input_name:
            raise ValueError("Ein Eingabedateiname ist ungültig.")
        still_name = str(source.get("still_name") or name)
        if not still_name.endswith(".html") or "/" in still_name or "\\" in still_name:
            raise ValueError("Ein statischer Quelldateiname ist ungültig.")
        sha256 = str(source.get("sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Die Prüfsumme einer Quelle ist ungültig.")
        still_sha256 = str(source.get("still_sha256") or sha256)
        if not re.fullmatch(r"[a-f0-9]{64}", still_sha256):
            raise ValueError("Die Prüfsumme einer statischen Quelle ist ungültig.")
        clean.append({
            "name": name,
            "input_name": input_name,
            "still_name": still_name,
            "width": 1080,
            "height": 1920 if int(source.get("height") or 1350) == 1920 else 1350,
            "duration_seconds": max(
                0.5, min(15.0, float(source.get("duration_seconds") or 8.0))
            ),
            "sha256": sha256,
            "still_sha256": still_sha256,
        })
    return clean


def process_job(job_dir: Path) -> None:
    spec = read_json(job_dir / "job.json")
    job_id = str(spec.get("job_id") or "")
    if not JOB_ID_RE.fullmatch(job_id) or job_dir.name != job_id:
        raise ValueError("Die Job-ID ist ungültig.")
    sources = _job_sources(spec)
    renderer = Path(str(spec.get("renderer") or "")).resolve()
    if not renderer.is_file():
        raise FileNotFoundError("Der gemeinsame Renderer ist nicht verfügbar.")

    input_dir = (job_dir / "input").resolve()
    output_dir = (job_dir / "output").resolve()
    if input_dir.parent != job_dir.resolve() or not input_dir.is_dir():
        raise ValueError("Der Eingabe-Snapshot fehlt.")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()
    zip_path = job_dir / "export.zip"
    zip_path.unlink(missing_ok=True)

    update_status(
        job_dir,
        state="running",
        total=len(sources),
        completed=0,
        percent=0,
        current="",
        error="",
        started_at=_utc_now(),
    )

    # Markensound des Workspace: beim Anlegen in den Job-Ordner kopiert, damit
    # ein spaeteres Ersetzen in den Einstellungen laufende Jobs nicht aendert.
    # Dazu optional ein eigener CTA-Sound fuer die animierte Abschluss-Slide.
    audio = spec.get("audio") if isinstance(spec.get("audio"), dict) else {}

    def _audio_datei(feld: str, basis: str):
        name = str(audio.get(feld) or "")
        if not re.fullmatch(basis + r"\.(mp3|m4a|aac|wav)", name):
            return None
        candidate = job_dir / name
        return candidate if candidate.is_file() and not candidate.is_symlink() else None

    audio_haupt = _audio_datei("datei", "markensound")
    audio_cta = _audio_datei("cta_datei", "cta-sound")
    try:
        audio_volume = max(0.05, min(1.0, float(audio.get("lautstaerke", 0.8))))
    except (TypeError, ValueError):
        audio_volume = 0.8

    outputs = []
    job_started = time.monotonic()
    for index, source in enumerate(sources, start=1):
        if time.monotonic() - job_started > JOB_TIMEOUT_S:
            raise TimeoutError("Der gesamte Renderauftrag hat zu lange gedauert.")
        name = source["name"]
        input_name = source["input_name"]
        still_name = source["still_name"]
        duration = source["duration_seconds"]
        source_path = (input_dir / input_name).resolve()
        still_source_path = (input_dir / still_name).resolve()
        try:
            source_path.relative_to(input_dir)
            still_source_path.relative_to(input_dir)
        except ValueError as exc:
            raise ValueError("Eine Quelle liegt außerhalb des Snapshots.") from exc
        if not source_path.is_file() or source_path.is_symlink():
            raise FileNotFoundError(f"Die Quelle {name} fehlt im Snapshot.")
        if not still_source_path.is_file() or still_source_path.is_symlink():
            raise FileNotFoundError(f"Die statische Quelle {name} fehlt im Snapshot.")
        if _sha256_file(source_path) != source["sha256"]:
            raise ValueError(f"Die Quelle {name} wurde nach dem Start verändert.")
        if _sha256_file(still_source_path) != source["still_sha256"]:
            raise ValueError(f"Die statische Quelle {name} wurde nach dem Start verändert.")
        prepared_path = input_dir / f".render_{index:02d}_{source_path.name}"
        shutil.copy2(source_path, prepared_path)
        _sanitize_snapshot_html(prepared_path)
        if still_source_path == source_path:
            prepared_still_path = prepared_path
        else:
            prepared_still_path = input_dir / f".render_{index:02d}_still_{still_source_path.name}"
            shutil.copy2(still_source_path, prepared_still_path)
            _sanitize_snapshot_html(prepared_still_path)

        stem = _safe_output_stem(name, index)
        mp4_path = output_dir / f"{stem}.mp4"
        png_path = output_dir / f"{stem}.png"
        size = f"{source['width']}x{source['height']}"
        command = [
            sys.executable,
            str(renderer),
            str(prepared_path),
            "--animate", str(duration),
            "--size", size,
            "--scale", str(VIDEO_OUTPUT_SCALE),
            "--capture-scale", str(VIDEO_CAPTURE_SCALE),
            "--output", str(mp4_path),
            "--end-png", str(png_path),
            "--end-png-source", str(prepared_still_path),
            "--asset-root", str(input_dir),
        ]
        for host in ALLOWED_NETWORK_HOSTS:
            command.extend(("--allow-network-host", host))
        # Markenfarben: der Job rendert aus einer Kopie, render.py findet die
        # brand.yaml des Workspace deshalb nicht von selbst.
        # Ablage: <WORKSPACES_DIR>/.platform/render_jobs/<job>, der Workspace daneben.
        brand = job_dir.resolve().parents[2] / str(spec.get("workspace") or "") / "brand.yaml"
        if spec.get("workspace") and brand.is_file():
            command.extend(("--brand", str(brand)))
        audio_path = audio_cta if audio_cta is not None and Path(name).stem.lower().endswith("_cta") else audio_haupt
        if audio_path is not None:
            command.extend(("--audio", str(audio_path), "--audio-volume", f"{audio_volume:.2f}"))
            if audio_path == audio_cta:
                command.append("--audio-once")
        try:
            remaining = JOB_TIMEOUT_S - (time.monotonic() - job_started)
            if remaining <= 0:
                raise TimeoutError("Der gesamte Renderauftrag hat zu lange gedauert.")
            code, detail = _run_renderer(
                job_dir, command, name, min(SLIDE_TIMEOUT_S, remaining)
            )
        finally:
            prepared_path.unlink(missing_ok=True)
            if prepared_still_path != prepared_path:
                prepared_still_path.unlink(missing_ok=True)
        if time.monotonic() - job_started > JOB_TIMEOUT_S:
            raise TimeoutError("Der gesamte Renderauftrag hat zu lange gedauert.")
        if code != 0 or not mp4_path.is_file() or not png_path.is_file():
            raise RuntimeError(detail or f"Das Rendern von {name} ist fehlgeschlagen.")
        if mp4_path.is_symlink() or png_path.is_symlink():
            raise RuntimeError("Der Renderer hat einen ungültigen Ausgabepfad erzeugt.")
        outputs.extend((mp4_path.name, png_path.name))
        output_bytes = sum((output_dir / output).stat().st_size for output in outputs)
        if output_bytes > MAX_OUTPUT_BYTES:
            raise RuntimeError("Der Export ist zu groß.")
        completed = index
        update_status(
            job_dir,
            state="running",
            completed=completed,
            percent=round(completed / len(sources) * 100),
            current=name,
        )

    zip_tmp = job_dir / ".export.zip.tmp"
    zip_tmp.unlink(missing_ok=True)
    output_bytes = sum((output_dir / name).stat().st_size for name in outputs)
    if shutil.disk_usage(job_dir).free < output_bytes + 256 * 1024 * 1024:
        raise RuntimeError("Für das ZIP ist nicht genug Speicher frei.")
    with zipfile.ZipFile(zip_tmp, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in outputs:
            path = output_dir / name
            if not path.is_file() or path.is_symlink() or path.parent != output_dir:
                raise RuntimeError("Eine Exportdatei ist ungültig.")
            archive.write(path, arcname=name)
    os.replace(zip_tmp, zip_path)
    zip_sha256 = _sha256_file(zip_path)
    job_size = _directory_bytes(job_dir)
    update_status(
        job_dir,
        state="done",
        completed=len(sources),
        percent=100,
        current="",
        outputs=outputs,
        zip_size=zip_path.stat().st_size,
        zip_sha256=zip_sha256,
        job_size=job_size,
        finished_at=_utc_now(),
    )
    _cleanup_retained_jobs(job_dir, spec)


def _queued_jobs(queue_root: Path) -> list[Path]:
    jobs = []
    for entry in queue_root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or not JOB_ID_RE.fullmatch(entry.name):
            continue
        status = read_json(entry / "status.json")
        if status.get("type") == "post_export" and status.get("state") in ("queued", "running"):
            jobs.append(entry)
    return sorted(
        jobs,
        key=lambda path: (
            str(read_json(path / "status.json").get("created_at") or ""),
            path.name,
        ),
    )


def drain_queue(queue_root: Path) -> None:
    queue_root = queue_root.resolve()
    queue_root.mkdir(parents=True, exist_ok=True)
    lock_path = queue_root.parent / "render_worker.lock"
    with sperrdatei(lock_path) as lock:
        try:
            sperren(lock, warten=False)
        except BlockingIOError:
            return
        empty_checks = 0
        while True:
            jobs = _queued_jobs(queue_root)
            if not jobs:
                empty_checks += 1
                if empty_checks >= 2:
                    return
                time.sleep(0.5)
                continue
            empty_checks = 0
            job_dir = jobs[0]
            try:
                process_job(job_dir)
            except Exception as exc:  # noqa: BLE001
                shutil.rmtree(job_dir / "output", ignore_errors=True)
                (job_dir / ".export.zip.tmp").unlink(missing_ok=True)
                for prepared in (job_dir / "input").glob(".render_*"):
                    prepared.unlink(missing_ok=True)
                update_status(
                    job_dir,
                    state="error",
                    error=str(exc)[-1000:] or "Der Export konnte nicht erstellt werden.",
                    finished_at=_utc_now(),
                )
                _cleanup_retained_jobs(job_dir, read_json(job_dir / "job.json"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("queue_root", type=Path)
    args = parser.parse_args()
    drain_queue(args.queue_root)


if __name__ == "__main__":
    main()
