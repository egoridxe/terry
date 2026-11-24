"""Comprehensive verification for Terry Voice Media (ducking math, 5s/3s multi-turn, numbers, Core Audio, Vosk)."""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import tempfile
import wave

import sounddevice as sd
import vosk

from matcher import CommandMatcher
import media_keys as mk
from ru_numbers import parse_duration_seconds, parse_number

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "commands.json"
MODEL_PATH = APP_DIR / "models" / "vosk-model-small-ru-0.22"


def test_ru_numbers() -> None:
    assert parse_number("громкость сорок пять") == 45
    assert parse_number("поставь на ноль") == 0
    assert parse_number("громкость сто процентов") == 100
    assert parse_number("громкость на максимум", allow_volume_aliases=True) == 100
    assert parse_number("громкость на половину", allow_volume_aliases=True) == 50
    assert parse_number("измени громкость на 72") == 72

    assert parse_duration_seconds("выключи комп через тридцать минут") == (1800, "30 мин")
    assert parse_duration_seconds("выключи комп через полчаса") == (1800, "30 мин")
    assert parse_duration_seconds("выключи компьютер через полтора часа") == (5400, "1 ч 30 мин")
    assert parse_duration_seconds("выключи комп через час двадцать") == (4800, "1 ч 20 мин")
    assert parse_duration_seconds("выключи комп через два часа пятнадцать минут") == (8100, "2 ч 15 мин")
    assert parse_duration_seconds("выключи комп через минуту") == (60, "1 мин")
    assert parse_duration_seconds("выключи комп через час") == (3600, "1 ч")
    assert parse_duration_seconds("выключи комп через сорок пять") == (2700, "45 мин")
    assert parse_duration_seconds("выключи комп") is None
    print("[OK] ru_numbers passed")


def test_multi_turn_and_matcher() -> None:
    matcher = CommandMatcher.from_file(CONFIG_PATH)
    t = 100.0

    # Without "Терри" -> ignored
    assert matcher.match("тише", is_partial=False, now=t) is None
    assert matcher.match("дальше", is_partial=False, now=t + 1.0) is None

    # Scenario 1: Single-breath "Терри тише" -> follow-up "тише" -> follow-up "дальше" -> expires after 3s
    t = 200.0
    w = matcher.match("терри", is_partial=True, now=t)
    assert w and w.action == "wake" and w.is_partial and w.session_seconds == 5.0
    assert matcher.is_armed(now=t + 4.9)
    assert not matcher.is_armed(now=t + 5.1)

    # First command completes at t + 0.8s -> extends window by +3.0s (until t + 3.8s)
    c1 = matcher.match("терри тише", is_partial=False, now=t + 0.8)
    assert c1 and c1.action == "volume_down" and c1.session_seconds == 3.0
    assert abs(matcher.armed_until - (t + 3.8)) < 1e-6

    # Second command WITHOUT "Терри" at t + 2.5s -> accepted! Extends by +3.0s (until t + 5.5s)
    c2 = matcher.match("тише", is_partial=False, now=t + 2.5)
    assert c2 and c2.action == "volume_down" and c2.session_seconds == 3.0
    assert abs(matcher.armed_until - (t + 5.5)) < 1e-6

    # Third command WITHOUT "Терри" at t + 4.5s -> accepted! Extends by +3.0s (until t + 7.5s)
    c3 = matcher.match("дальше", is_partial=False, now=t + 4.5)
    assert c3 and c3.action == "next" and c3.session_seconds == 3.0
    assert abs(matcher.armed_until - (t + 7.5)) < 1e-6

    # After 3.0s silence (t + 7.6s), session expired -> bare "тише" MUST be ignored again!
    assert not matcher.is_armed(now=t + 7.6)
    assert matcher.match("тише", is_partial=False, now=t + 7.6) is None

    # Scenario 2: Parameterized commands wait for final & respect exact numbers
    t = 300.0
    assert matcher.match("терри", is_partial=True, now=t).action == "wake"
    assert matcher.match("терри громкость сорок", is_partial=True, now=t + 0.3) is None
    v = matcher.match("терри громкость сорок пять", is_partial=False, now=t + 0.8)
    assert v and v.action == "volume_set" and v.value == 45 and v.session_seconds == 3.0

    # Follow-up sleep timer without repeating "Терри"
    s = matcher.match("выключи комп через полтора часа", is_partial=False, now=t + 2.2)
    assert s and s.action == "shutdown_timer" and s.value == 5400 and s.session_seconds == 3.0

    # Follow-up cancel without repeating "Терри"
    cc = matcher.match("отмена выключения", is_partial=True, now=t + 3.5)
    assert cc and cc.action == "shutdown_cancel" and cc.session_seconds == 3.0

    # Shutdown without duration is ignored
    t = 400.0
    matcher.disarm()
    assert matcher.match("терри выключи комп", is_partial=False, now=t) is None

    print("[OK] Multi-turn 5s wake + 3s follow-up & matcher tests passed")


def test_duck_session_math() -> None:
    """Verify ducking logic with mocked Core Audio getter/setter (doesn't touch live volume)."""
    state = {"vol": 60}
    orig_get = mk.get_master_volume
    orig_set = mk.set_master_volume
    orig_ramp = mk.ramp_master_volume

    def fake_get() -> int:
        return state["vol"]

    def fake_set(p: int, *, unmute_if_positive: bool = True) -> int:
        state["vol"] = max(0, min(100, int(p)))
        return state["vol"]

    def fake_ramp(p: int, *, duration: float = 0.0, steps: int = 1, unmute_if_positive: bool = False) -> int:
        return fake_set(p, unmute_if_positive=unmute_if_positive)

    mk.get_master_volume = fake_get
    mk.set_master_volume = fake_set
    mk.ramp_master_volume = fake_ramp
    try:
        ds = mk.VolumeDuckSession(factor=0.5)
        # 1. Start at 60% -> ducks to 30%
        assert ds.start_duck() == 60
        assert state["vol"] == 30
        assert ds.base_volume == 60

        # 2. First "тише" (-10%) -> base becomes 50%, live preview drops to 25%
        assert ds.adjust_base(-10) == 50
        assert ds.base_volume == 50
        assert state["vol"] == 25

        # 3. Second "тише" (-10%) -> base becomes 40%, live preview drops to 20%
        assert ds.adjust_base(-10) == 40
        assert ds.base_volume == 40
        assert state["vol"] == 20

        # 4. End duck after 3s -> restores to NEW logical base 40% (NOT old 60%!)
        assert ds.end_duck() == 40
        assert state["vol"] == 40
        assert not ds.active

        # 5. Exact volume while ducked: start at 40% -> duck 20% -> set 80% (live 40%) -> end -> 80%
        assert ds.start_duck() == 40
        assert state["vol"] == 20
        assert ds.set_base(80) == 80
        assert state["vol"] == 40
        assert ds.end_duck() == 80
        assert state["vol"] == 80
    finally:
        mk.get_master_volume = orig_get
        mk.set_master_volume = orig_set
        mk.ramp_master_volume = orig_ramp

    print("[OK] VolumeDuckSession math verified (60% -> duck 30% -> -10%/-10% -> restores cleanly to 40%)")


def test_live_core_audio_and_vosk() -> None:
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(mk.INPUT) == expected
    vol = mk.get_master_volume()
    assert mk.set_master_volume(vol, unmute_if_positive=False) == vol
    print(f"[OK] Live Core Audio master volume intact: {vol}%")

    vosk.SetLogLevel(-1)
    model = vosk.Model(str(MODEL_PATH))
    matcher = CommandMatcher.from_file(CONFIG_PATH)

    # Simulate multi-turn real TTS: "Терри, тише" then "тише" (within 3s)
    t = 50.0
    for spoken, exp_act in [("Терри, тише", "volume_down"), ("тише", "volume_down"), ("дальше", "next")]:
        wav = Path(tempfile.gettempdir()) / "terry_mt.wav"
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.SelectVoice('Microsoft Irina Desktop'); "
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$s.SetOutputToWaveFile('{wav}', $f); "
            f"$s.Speak('{spoken}'); "
            "$s.SetOutputToNull()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        rec = vosk.KaldiRecognizer(model, 16000)
        with wave.open(str(wav), "rb") as wf:
            rec.AcceptWaveform(wf.readframes(wf.getnframes()))
        heard = json.loads(rec.FinalResult()).get("text", "")
        wav.unlink(missing_ok=True)
        t += 1.5
        res = matcher.match(heard, is_partial=False, now=t)
        print(f"  [E2E multi-turn] {spoken:14s} -> {heard!r:14s} -> {res.action if res else None} (extend={res.session_seconds if res else 0}s)")
        assert res and res.action == exp_act

    print(f"[OK] Default input microphone: {sd.query_devices(kind='input')['name']}")


def main() -> None:
    test_ru_numbers()
    test_multi_turn_and_matcher()
    test_duck_session_math()
    test_live_core_audio_and_vosk()
    print("\nALL TERRY DUCKING & MULTI-TURN TESTS PASSED.")


if __name__ == "__main__":
    main()
