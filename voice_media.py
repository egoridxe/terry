"""Terry (Autonomy Voice Media) — offline Russian voice assistant in Windows tray.

Features:
- Wake word "Терри" ducks system audio by 50% for 5 seconds and opens a multi-command window.
- Each accepted command extends the active listening + lowered-volume window by +3 seconds.
- Volume commands ("тише", "громче", "громкость N") update the logical base volume and
  preview immediately at 50% ducked level, restoring smoothly to the NEW volume when done.
- Exact Windows Core Audio master volume (0-100%) and PC sleep timer with cancellation.
"""

from __future__ import annotations

import atexit
from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
import pystray
import sounddevice as sd
import vosk

import favorites_player as fp
from matcher import CommandMatcher
import media_keys as mk

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "commands.json"
MODEL_PATH = APP_DIR / "models" / "vosk-model-small-ru-0.22"
LOG_PATH = APP_DIR / "voice_media.log"
LAUNCHER_VBS = APP_DIR / "Start Voice Media.vbs"

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 1600  # 100 ms chunks at 16 kHz


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("voice_media")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


log = _setup_logging()


def _create_tray_icon(state: str = "listening") -> Image.Image:
    palette = {
        "listening": ((37, 99, 235, 255), (255, 255, 255, 255)),  # calm blue
        "armed": ((234, 179, 8, 255), (24, 24, 27, 255)),         # bright amber (active & ducked)
        "triggered": ((22, 163, 74, 255), (255, 255, 255, 255)),  # green flash
        "paused": ((100, 116, 139, 255), (226, 232, 240, 255)),   # gray
        "error": ((220, 38, 38, 255), (255, 255, 255, 255)),      # red
    }
    bg, fg = palette.get(state, palette["listening"])

    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, size - 6, size - 6), fill=bg)

    if state == "paused":
        d.rounded_rectangle((40, 36, 56, 92), radius=6, fill=fg)
        d.rounded_rectangle((72, 36, 88, 92), radius=6, fill=fg)
    elif state == "triggered":
        d.line([(36, 66), (56, 88), (94, 42)], fill=fg, width=12, joint="curve")
    else:
        d.rounded_rectangle((50, 28, 78, 72), radius=14, fill=fg)
        d.arc((36, 46, 92, 92), start=0, end=180, fill=fg, width=8)
        d.line((64, 92, 64, 104), fill=fg, width=8)
        d.line((48, 104, 80, 104), fill=fg, width=8)

    return img.resize((64, 64), Image.Resampling.LANCZOS)


def _list_input_devices() -> list[tuple[int, str]]:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    default_host = sd.default.hostapi
    seen_names: set[str] = set()
    result: list[tuple[int, str]] = []

    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] > 0 and dev["hostapi"] == default_host:
            name = str(dev["name"]).strip()
            if name and name not in seen_names:
                seen_names.add(name)
                result.append((idx, name))

    if not result:
        for idx, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                api_name = hostapis[dev["hostapi"]]["name"]
                label = f"{dev['name']} ({api_name})"
                if label not in seen_names:
                    seen_names.add(label)
                    result.append((idx, label))
    return result


class VoiceMediaApp:
    def __init__(self) -> None:
        vosk.SetLogLevel(-1)
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"Vosk model not found at {MODEL_PATH}")

        self.config = self._load_config()
        self._config_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
        self.matcher = CommandMatcher(self.config)
        self.model = vosk.Model(str(MODEL_PATH))

        self.paused = False
        self.stop_event = threading.Event()
        self.restart_stream_event = threading.Event()
        self.selected_device_name: str | None = self.config.get("device_name")
        self.last_status = "Жду «Терри»…"
        self.shutdown_target_time: str | None = None
        self.shutdown_target_ts: float | None = None

        self._session_lock = threading.RLock()
        self._player_lock = threading.Lock()
        self.duck_session = mk.VolumeDuckSession(factor=float(self.config.get("duck_factor", 0.5)))
        self._session_active = False
        self._session_deadline: float = 0.0
        self._speech_active = False
        self._flash_timer: threading.Timer | None = None

        atexit.register(self._safe_restore_audio)

        self._icons = {
            "listening": _create_tray_icon("listening"),
            "armed": _create_tray_icon("armed"),
            "triggered": _create_tray_icon("triggered"),
            "paused": _create_tray_icon("paused"),
            "error": _create_tray_icon("error"),
        }
        self.tray: pystray.Icon | None = None
        self._worker_thread: threading.Thread | None = None

    def _safe_restore_audio(self) -> None:
        with self._session_lock:
            self._session_active = False
            self._session_deadline = 0.0
            self.matcher.disarm()
            self.duck_session.end_duck()

    def _load_config(self) -> dict[str, Any]:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def _save_config(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._config_mtime = CONFIG_PATH.stat().st_mtime

    def _maybe_reload_config(self) -> None:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime != self._config_mtime:
                self._config_mtime = mtime
                self.config = self._load_config()
                self.matcher.update_config(self.config)
                log.info("Reloaded commands.json")
        except Exception as exc:
            log.warning("Failed to reload commands.json: %s", exc)

    def _build_tooltip(self) -> str:
        base = f"Терри — {self.last_status}"
        if self.shutdown_target_time and self.shutdown_target_ts and time.time() < self.shutdown_target_ts:
            base += f" | Сон в {self.shutdown_target_time}"
        return base[:120]

    def _set_tray_state(self, state: str, tooltip: str | None = None) -> None:
        if self.tray is None:
            return
        self.tray.icon = self._icons.get(state, self._icons["listening"])
        self.tray.title = (tooltip or self._build_tooltip())[:120]

    def _notify(self, title: str, message: str) -> None:
        if self.tray is None:
            return
        try:
            self.tray.notify(message, title)
        except Exception as exc:
            log.debug("Tray notify skipped: %s", exc)

    def _activate_session(self, duration: float, *, status: str | None = None) -> None:
        """Start or extend the active listening + 50% ducked volume window."""
        with self._session_lock:
            now = time.monotonic()
            self._session_active = True
            self._session_deadline = now + max(0.5, float(duration))
            if bool(self.config.get("duck_enabled", True)) and not self.duck_session.active:
                factor = float(self.config.get("duck_factor", 0.5))
                base = self.duck_session.start_duck(factor=factor)
                log.info("Ducking volume (base=%s%% -> factor=%.2f, window=%.1fs)", base, factor, duration)
            if status is not None:
                self.last_status = status
            self._set_tray_state("armed")
        if self.tray is not None:
            self.tray.update_menu()

    def _end_session(self) -> None:
        """Close active listening window and smoothly restore volume to base_volume."""
        with self._session_lock:
            if not self._session_active and not self.duck_session.active:
                return
            self._session_active = False
            self._session_deadline = 0.0
            self.matcher.disarm()
            restored = self.duck_session.end_duck()
            if restored is not None:
                log.info("Restored volume to %s%% after listening window ended", restored)
            if not self.paused:
                self._set_tray_state("listening")
        if self.tray is not None:
            self.tray.update_menu()

    def _check_session_expiry(self) -> None:
        with self._session_lock:
            if not self._session_active:
                return
            now = time.monotonic()
            # Don't cut off the user mid-word if they started speaking just before expiry (up to 1.5s grace)
            grace = 1.5 if self._speech_active else 0.0
            if now < self._session_deadline + grace:
                return
        self._end_session()

    def _flash_triggered(self, label: str, *, keep_armed: bool = False) -> None:
        now_str = time.strftime("%H:%M:%S")
        self.last_status = f"{label} ({now_str})"
        self._set_tray_state("triggered")
        if self._flash_timer is not None:
            self._flash_timer.cancel()

        def _restore_icon() -> None:
            if self.stop_event.is_set():
                return
            if self.paused:
                self._set_tray_state("paused")
            elif keep_armed and self._session_active:
                self._set_tray_state("armed")
            else:
                self._set_tray_state("listening")

        self._flash_timer = threading.Timer(0.65, _restore_icon)
        self._flash_timer.daemon = True
        self._flash_timer.start()
        if self.tray is not None:
            self.tray.update_menu()

    def _resolve_device_index(self) -> int | None:
        if not self.selected_device_name:
            return None
        for idx, name in _list_input_devices():
            if name == self.selected_device_name:
                return idx
        return None

    def _handle_recognized(self, text: str, *, is_partial: bool, rec: vosk.KaldiRecognizer) -> None:
        if not text:
            return
        match = self.matcher.match(text, is_partial=is_partial)
        if not match:
            if not is_partial:
                log.info("Heard (ignored): %r", text)
            return

        if match.action == "wake":
            log.info("WAKE heard=%r partial=%s window=%.1fs", match.heard, match.is_partial, match.session_seconds)
            self._activate_session(match.session_seconds, status="Терри слушает…")
            if not match.is_partial:
                rec.Reset()
            return

        if match.action in {"play_favorites_sc", "play_favorites_spotify"}:
            service = "spotify" if match.action == "play_favorites_spotify" else "soundcloud"
            log.info(
                "TRIGGER action=%s service=%s phrase=%r heard=%r",
                match.action,
                service,
                match.phrase,
                match.heard,
            )
            rec.Reset()
            self._launch_favorites_async(service)
            return

        # Make sure ducking is engaged before modifying volume if the command came straight from final
        use_duck = bool(self.config.get("duck_enabled", True)) and match.session_seconds > 0
        if use_duck:
            self._activate_session(match.session_seconds)

        prev_double_tap = bool(self.config.get("prev_double_tap", True))
        volume_step = int(self.config.get("volume_step_percent", 10))
        try:
            with self._session_lock:
                label, meta = mk.send_media_action(
                    match.action,
                    value=match.value,
                    prev_double_tap=prev_double_tap,
                    volume_step_percent=volume_step,
                    duck_session=self.duck_session if use_duck else None,
                )
        except Exception as exc:
            log.exception("Failed to execute action %s: %s", match.action, exc)
            self._flash_triggered(f"Ошибка: {match.action}", keep_armed=self._session_active)
            return

        if match.session_seconds > 0:
            self._activate_session(match.session_seconds)

        if match.action == "shutdown_timer":
            self.shutdown_target_time = str(meta.get("target_time", ""))
            self.shutdown_target_ts = float(meta.get("target_timestamp", 0.0))
            self._notify(
                "Терри — Таймер сна",
                f"ПК выключится через {meta['duration_label']} (в {self.shutdown_target_time}).",
            )
        elif match.action == "shutdown_cancel":
            self.shutdown_target_time = None
            self.shutdown_target_ts = None
            self._notify("Терри — Таймер сна", label)

        log.info(
            "TRIGGER action=%s value=%s label=%r phrase=%r heard=%r partial=%s extend=%.1fs",
            match.action,
            match.value,
            label,
            match.phrase,
            match.heard,
            match.is_partial,
            match.session_seconds,
        )
        rec.Reset()
        self._flash_triggered(label, keep_armed=(match.session_seconds > 0))

    def _audio_loop(self) -> None:
        while not self.stop_event.is_set():
            self.restart_stream_event.clear()
            audio_q: queue.Queue[bytes] = queue.Queue(maxsize=40)
            rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
            rec.SetWords(False)

            device_idx = self._resolve_device_index()
            device_label = self.selected_device_name or "Микрофон по умолчанию"
            log.info("Opening audio input stream: %s (idx=%s)", device_label, device_idx)

            def _callback(indata: bytes, frames: int, time_info: Any, status: Any) -> None:
                if status:
                    log.debug("Audio status: %s", status)
                if self.paused or self.stop_event.is_set():
                    return
                try:
                    audio_q.put_nowait(bytes(indata))
                except queue.Full:
                    pass

            pre_buffer: deque[bytes] = deque(maxlen=4)
            speech_active = False
            silence_chunks = 0
            last_config_check = time.monotonic()

            try:
                with sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=BLOCK_SAMPLES,
                    device=device_idx,
                    dtype="int16",
                    channels=1,
                    callback=_callback,
                ):
                    if not self.paused:
                        self._set_tray_state("listening")

                    while not self.stop_event.is_set() and not self.restart_stream_event.is_set():
                        self._check_session_expiry()

                        now = time.monotonic()
                        if now - last_config_check > 1.5:
                            self._maybe_reload_config()
                            last_config_check = now

                        try:
                            chunk = audio_q.get(timeout=0.15)
                        except queue.Empty:
                            continue

                        if self.paused:
                            pre_buffer.clear()
                            speech_active = False
                            self._speech_active = False
                            silence_chunks = 0
                            continue

                        vad_enabled = bool(self.config.get("vad_enabled", True))
                        vad_threshold = float(self.config.get("vad_threshold", 150))

                        if vad_enabled:
                            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

                            if rms >= vad_threshold:
                                if not speech_active:
                                    speech_active = True
                                    self._speech_active = True
                                    while pre_buffer:
                                        rec.AcceptWaveform(pre_buffer.popleft())
                                silence_chunks = 0
                                chunks_to_feed = [chunk]
                            elif speech_active:
                                silence_chunks += 1
                                chunks_to_feed = [chunk]
                                if silence_chunks >= 6:  # ~0.6s pause -> crisp multi-turn finalization
                                    final_res = json.loads(rec.FinalResult())
                                    self._handle_recognized(final_res.get("text", ""), is_partial=False, rec=rec)
                                    speech_active = False
                                    self._speech_active = False
                                    silence_chunks = 0
                                    pre_buffer.clear()
                                    continue
                            else:
                                pre_buffer.append(chunk)
                                continue
                        else:
                            chunks_to_feed = [chunk]

                        for pcm in chunks_to_feed:
                            if rec.AcceptWaveform(pcm):
                                res = json.loads(rec.Result())
                                self._handle_recognized(res.get("text", ""), is_partial=False, rec=rec)
                            else:
                                partial = json.loads(rec.PartialResult()).get("partial", "")
                                if partial:
                                    self._handle_recognized(partial, is_partial=True, rec=rec)

            except Exception as exc:
                log.exception("Audio stream error: %s", exc)
                self._end_session()
                self.last_status = f"Ошибка микрофона: {exc}"
                self._set_tray_state("error", "Терри — Ошибка микрофона")
                self.stop_event.wait(3.0)

    # ------------------------------------------------------------------
    # Tray menu actions
    # ------------------------------------------------------------------
    def _toggle_pause(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.paused = not self.paused
        self._end_session()
        if self.paused:
            self.last_status = "На паузе"
            self._set_tray_state("paused")
            log.info("Listening paused")
        else:
            self.last_status = "Жду «Терри»…"
            self._set_tray_state("listening")
            log.info("Listening resumed")
        if self.tray is not None:
            self.tray.update_menu()

    def _toggle_wake_word(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        new_val = not bool(self.config.get("require_wake_word", True))
        self.config["require_wake_word"] = new_val
        self.matcher.update_config(self.config)
        self._save_config()
        log.info("require_wake_word=%s", new_val)
        if self.tray is not None:
            self.tray.update_menu()

    def _toggle_ducking(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        new_val = not bool(self.config.get("duck_enabled", True))
        self.config["duck_enabled"] = new_val
        if not new_val:
            self._end_session()
        self._save_config()
        log.info("duck_enabled=%s", new_val)
        if self.tray is not None:
            self.tray.update_menu()

    def _toggle_prev_double_tap(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        new_val = not bool(self.config.get("prev_double_tap", True))
        self.config["prev_double_tap"] = new_val
        self._save_config()
        log.info("prev_double_tap=%s", new_val)
        if self.tray is not None:
            self.tray.update_menu()

    def _toggle_autostart(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        enabled = not mk.is_autostart_enabled()
        mk.set_autostart(enabled, LAUNCHER_VBS)
        log.info("Autostart=%s", enabled)
        if self.tray is not None:
            self.tray.update_menu()

    def _launch_favorites_async(self, service: str) -> None:
        if not self._player_lock.acquire(blocking=False):
            log.info("Favorites launcher already busy; skipping duplicate request for %s", service)
            return

        title_hint = "Spotify" if service == "spotify" else "SoundCloud"
        self._flash_triggered(f"Открываю любимые ({title_hint})…", keep_armed=False)

        def _worker() -> None:
            try:
                # Restore ducked volume before playback starts so music plays at full volume
                self._end_session()
                label, meta = fp.play_favorites(service, config=self.config)
                self._flash_triggered(label, keep_armed=False)
                if meta.get("mode") == "browser":
                    self._notify("Терри — Музыка", f"Приложение не найдено, открыто в браузере: {label}")
            except Exception as exc:
                log.exception("Failed to launch favorites for %s: %s", service, exc)
                self._flash_triggered(f"Ошибка запуска {title_hint}", keep_armed=False)
            finally:
                self._player_lock.release()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _play_sc_menu(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._launch_favorites_async("soundcloud")

    def _play_spotify_menu(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._launch_favorites_async("spotify")

    def _cancel_shutdown_menu(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        label, _meta = mk.send_media_action("shutdown_cancel")
        self.shutdown_target_time = None
        self.shutdown_target_ts = None
        self._flash_triggered(label, keep_armed=self._session_active)
        self._notify("Терри — Таймер сна", label)

    def _select_device(self, name: str | None):
        def _handler(_icon: pystray.Icon, _item: pystray.MenuItem) -> None:
            self.selected_device_name = name
            self.config["device_name"] = name
            self._save_config()
            self.restart_stream_event.set()
            log.info("Switched microphone to: %s", name or "Default")
            if self.tray is not None:
                self.tray.update_menu()

        return _handler

    def _open_config(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        os.startfile(str(CONFIG_PATH))  # type: ignore[attr-defined]

    def _open_log(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        if not LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8")
        os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]

    def _quit(self, icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        log.info("Shutting down Terry Voice Media")
        self.stop_event.set()
        self._end_session()
        icon.stop()

    def _shutdown_status_label(self) -> str:
        if self.shutdown_target_time and self.shutdown_target_ts and time.time() < self.shutdown_target_ts:
            rem_min = max(1, round((self.shutdown_target_ts - time.time()) / 60))
            return f"Таймер сна: выключение в {self.shutdown_target_time} (~{rem_min} мин)"
        return "Таймер сна: не активен"

    def _build_mic_submenu(self) -> pystray.Menu:
        items = [
            pystray.MenuItem(
                "По умолчанию (системный)",
                self._select_device(None),
                checked=lambda _item: self.selected_device_name is None,
                radio=True,
            )
        ]
        for _idx, name in _list_input_devices():
            items.append(
                pystray.MenuItem(
                    name,
                    self._select_device(name),
                    checked=(lambda _item, n=name: self.selected_device_name == n),
                    radio=True,
                )
            )
        return pystray.Menu(*items)

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda _item: f"Статус: {self.last_status}", None, enabled=False),
            pystray.MenuItem(lambda _item: self._shutdown_status_label(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _item: "Продолжить прослушивание" if self.paused else "Пауза прослушивания",
                self._toggle_pause,
                default=True,
            ),
            pystray.MenuItem("Включить любимые (SoundCloud)", self._play_sc_menu),
            pystray.MenuItem("Включить любимые (Spotify)", self._play_spotify_menu),
            pystray.MenuItem("Отменить выключение ПК", self._cancel_shutdown_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Микрофон", self._build_mic_submenu()),
            pystray.MenuItem(
                "Только с именем «Терри»",
                self._toggle_wake_word,
                checked=lambda _item: bool(self.config.get("require_wake_word", True)),
            ),
            pystray.MenuItem(
                "Приглушать звук на 50% при «Терри»",
                self._toggle_ducking,
                checked=lambda _item: bool(self.config.get("duck_enabled", True)),
            ),
            pystray.MenuItem(
                "Двойное нажатие для «Назад»",
                self._toggle_prev_double_tap,
                checked=lambda _item: bool(self.config.get("prev_double_tap", True)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Настроить команды (commands.json)", self._open_config),
            pystray.MenuItem("Открыть лог распознавания", self._open_log),
            pystray.MenuItem(
                "Автозапуск с Windows",
                self._toggle_autostart,
                checked=lambda _item: mk.is_autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._quit),
        )

    def run(self) -> None:
        self._worker_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._worker_thread.start()

        self.tray = pystray.Icon(
            "autonomy_voice_media",
            self._icons["listening"],
            "Терри — Жду команду",
            menu=self._build_menu(),
        )
        log.info("Terry (Autonomy Voice Media) started")
        try:
            self.tray.run()
        finally:
            self._safe_restore_audio()


def main() -> None:
    mutex = mk.acquire_single_instance_mutex()
    if mutex is None:
        log.info("Another instance is already running; exiting.")
        return
    try:
        app = VoiceMediaApp()
        app.run()
    except Exception:
        log.exception("Fatal error in Voice Media")
        raise


if __name__ == "__main__":
    main()
