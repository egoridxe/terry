"""Smart Russian voice command matcher with 'Terry' wake word, multi-turn follow-up, numbers & sleep timer."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any

from ru_numbers import parse_duration_seconds, parse_number

_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    cleaned = text.lower().replace("ё", "е")
    cleaned = _NON_WORD_RE.sub(" ", cleaned)
    return _SPACE_RE.sub(" ", cleaned).strip()


@dataclass(frozen=True)
class MatchResult:
    action: str
    phrase: str
    heard: str
    is_partial: bool
    value: int | None = None
    extra: str = ""
    session_seconds: float = 0.0


class CommandMatcher:
    def __init__(self, config: dict[str, Any]) -> None:
        self.update_config(config)
        self._last_trigger_at: float = 0.0
        self._armed_until: float = 0.0

    @classmethod
    def from_file(cls, path: Path) -> "CommandMatcher":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data)

    def update_config(self, config: dict[str, Any]) -> None:
        self.config = config
        self.cooldown_seconds = float(config.get("cooldown_seconds", 0.45))
        self.require_wake_word = bool(config.get("require_wake_word", True))
        self.wake_timeout_seconds = float(config.get("wake_timeout_seconds", 5.0))
        self.followup_timeout_seconds = float(config.get("followup_timeout_seconds", 3.0))
        self.standalone_max_words = max(1, int(config.get("standalone_max_words", 2)))
        self.wake_words = {
            normalize_text(w) for w in config.get("wake_words", ["терри"]) if normalize_text(w)
        }

        self._shutdown_triggers = sorted(
            {normalize_text(p) for p in config.get("shutdown_trigger_phrases", []) if normalize_text(p)},
            key=len,
            reverse=True,
        )
        self._shutdown_cancels = sorted(
            {normalize_text(p) for p in config.get("shutdown_cancel_phrases", []) if normalize_text(p)},
            key=len,
            reverse=True,
        )
        self._volume_keywords = {
            normalize_text(w) for w in config.get("volume_set_keywords", ["громкость", "звук"]) if normalize_text(w)
        }

        fav_cfg = config.get("favorites_config", {})
        self._fav_actions = {
            normalize_text(w) for w in fav_cfg.get("action_words", []) if normalize_text(w)
        }
        self._fav_markers = sorted(
            {normalize_text(p) for p in fav_cfg.get("favorite_markers", []) if normalize_text(p)},
            key=len,
            reverse=True,
        )
        self._spotify_markers = sorted(
            {normalize_text(p) for p in fav_cfg.get("spotify_markers", []) if normalize_text(p)},
            key=len,
            reverse=True,
        )
        self._soundcloud_markers = sorted(
            {normalize_text(p) for p in fav_cfg.get("soundcloud_markers", []) if normalize_text(p)},
            key=len,
            reverse=True,
        )

        self._phrases: list[tuple[str, str]] = []
        self._standalone: list[tuple[str, str]] = []
        commands = config.get("commands", {})
        for action, spec in commands.items():
            for p in spec.get("phrases", []):
                np = normalize_text(str(p))
                if np:
                    self._phrases.append((np, action))
            for s in spec.get("standalone", []):
                ns = normalize_text(str(s))
                if ns:
                    self._standalone.append((ns, action))

        self._phrases.sort(key=lambda item: len(item[0]), reverse=True)
        self._standalone.sort(key=lambda item: len(item[0]), reverse=True)

    @property
    def armed_until(self) -> float:
        return self._armed_until

    def is_armed(self, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        return ts < self._armed_until

    def disarm(self) -> None:
        self._armed_until = 0.0

    @staticmethod
    def _contains_phrase(padded_text: str, phrase: str) -> bool:
        return f" {phrase} " in padded_text

    def _extract_after_wake(self, words: list[str]) -> tuple[bool, list[str]]:
        """Find wake word in tokens; return (found, words_after_wake)."""
        for idx, tok in enumerate(words):
            if tok in self.wake_words:
                return True, words[idx + 1 :]
        return False, words

    def _finish(
        self,
        ts: float,
        action: str,
        phrase: str,
        heard: str,
        is_partial: bool,
        *,
        value: int | None = None,
        extra: str = "",
        keep_session: bool = True,
    ) -> MatchResult:
        self._last_trigger_at = ts
        if keep_session and self.followup_timeout_seconds > 0:
            self._armed_until = ts + self.followup_timeout_seconds
            sess_secs = self.followup_timeout_seconds
        else:
            self._armed_until = 0.0
            sess_secs = 0.0
        return MatchResult(
            action=action,
            phrase=phrase,
            heard=heard,
            is_partial=is_partial,
            value=value,
            extra=extra,
            session_seconds=sess_secs,
        )

    def match(self, raw_text: str, *, is_partial: bool, now: float | None = None) -> MatchResult | None:
        norm = normalize_text(raw_text)
        if not norm:
            return None

        ts = time.monotonic() if now is None else now
        words = norm.split()
        has_wake, tail_words = self._extract_after_wake(words)
        armed_now = ts < self._armed_until

        # Instant wake trigger on first hearing "Терри" (even mid-utterance partial!)
        # so audio ducks immediately for 5 seconds without waiting for silence.
        if has_wake and not armed_now and is_partial:
            self._armed_until = ts + self.wake_timeout_seconds
            return MatchResult(
                action="wake",
                phrase="терри",
                heard=norm,
                is_partial=True,
                session_seconds=self.wake_timeout_seconds,
            )

        if ts - self._last_trigger_at < self.cooldown_seconds:
            return None

        if self.require_wake_word:
            if not has_wake and not armed_now:
                return None

            # Final standalone "Терри" refreshes the full 5-second listening window
            if has_wake and not tail_words:
                if not is_partial:
                    self._armed_until = ts + self.wake_timeout_seconds
                    return MatchResult(
                        action="wake",
                        phrase="терри",
                        heard=norm,
                        is_partial=False,
                        session_seconds=self.wake_timeout_seconds,
                    )
                return None

            cmd_words = tail_words if has_wake else words
        else:
            if has_wake and not tail_words and not is_partial:
                self._armed_until = ts + self.wake_timeout_seconds
                return MatchResult(
                    action="wake",
                    phrase="терри",
                    heard=norm,
                    is_partial=False,
                    session_seconds=self.wake_timeout_seconds,
                )
            cmd_words = tail_words if (has_wake and tail_words) else words

        if not cmd_words:
            return None

        cmd_text = " ".join(cmd_words)
        padded = f" {cmd_text} "
        keep_session = has_wake or armed_now

        # ------------------------------------------------------------------
        # 1. Shutdown cancel (highest safety priority — instant even on partial)
        # ------------------------------------------------------------------
        for phrase in self._shutdown_cancels:
            if self._contains_phrase(padded, phrase):
                return self._finish(
                    ts, "shutdown_cancel", phrase, norm, is_partial, keep_session=keep_session
                )

        # ------------------------------------------------------------------
        # 2. Shutdown sleep timer ("выключи комп через ... минут/часов")
        # ------------------------------------------------------------------
        matched_shutdown_trigger: str | None = None
        for phrase in self._shutdown_triggers:
            if self._contains_phrase(padded, phrase):
                matched_shutdown_trigger = phrase
                break

        if matched_shutdown_trigger is not None:
            if is_partial:
                return None
            parsed = parse_duration_seconds(cmd_text)
            if parsed is not None:
                secs, dur_label = parsed
                return self._finish(
                    ts,
                    "shutdown_timer",
                    matched_shutdown_trigger,
                    norm,
                    False,
                    value=secs,
                    extra=dur_label,
                    keep_session=keep_session,
                )
            return None

        # ------------------------------------------------------------------
        # 2b. Liked Tracks / Favorites (SoundCloud priority; Spotify if named)
        #     Wait for final utterance so trailing "в спотифай" isn't missed.
        # ------------------------------------------------------------------
        fav_hit = next((m for m in self._fav_markers if self._contains_phrase(padded, m)), None)
        sp_hit = next((m for m in self._spotify_markers if self._contains_phrase(padded, m)), None)
        sc_hit = next((m for m in self._soundcloud_markers if self._contains_phrase(padded, m)), None)
        has_action_verb = any(w in self._fav_actions for w in cmd_words)

        if fav_hit is not None or (has_action_verb and (sp_hit is not None or sc_hit is not None)):
            if is_partial:
                return None
            if sp_hit is not None:
                return self._finish(
                    ts,
                    "play_favorites_spotify",
                    f"{fav_hit or 'спотифай'} ({sp_hit})",
                    norm,
                    False,
                    keep_session=keep_session,
                )
            return self._finish(
                ts,
                "play_favorites_sc",
                f"{fav_hit or 'ск'}" + (f" ({sc_hit})" if sc_hit else ""),
                norm,
                False,
                keep_session=keep_session,
            )

        # ------------------------------------------------------------------
        # 3. Relative volume ("громче [на N]", "тише [на N]")
        # ------------------------------------------------------------------
        for action in ("volume_up", "volume_down"):
            spec = self.config.get("commands", {}).get(action, {})
            phrases = [normalize_text(p) for p in spec.get("phrases", []) if normalize_text(p)]
            standalone = [normalize_text(s) for s in spec.get("standalone", []) if normalize_text(s)]
            hit_phrase = next((p for p in phrases if self._contains_phrase(padded, p)), None)
            hit_single = next((s for s in standalone if self._contains_phrase(padded, s)), None)
            if hit_phrase or (hit_single and (keep_session or len(cmd_words) <= self.standalone_max_words + 2)):
                if is_partial:
                    return None
                delta = parse_number(cmd_text, allow_volume_aliases=False)
                if delta is not None:
                    delta = max(1, min(100, delta))
                return self._finish(
                    ts,
                    action,
                    hit_phrase or hit_single or action,
                    norm,
                    False,
                    value=delta,
                    keep_session=keep_session,
                )

        # ------------------------------------------------------------------
        # 4. Exact volume ("громкость 45", "измени громкость на 60", "звук на максимум")
        # ------------------------------------------------------------------
        if any(w in self._volume_keywords for w in cmd_words):
            mute_phrases = [
                normalize_text(p)
                for p in self.config.get("commands", {}).get("mute", {}).get("phrases", [])
            ]
            if not any(self._contains_phrase(padded, mp) for mp in mute_phrases):
                vol = parse_number(cmd_text, allow_volume_aliases=True)
                if vol is not None:
                    if is_partial:
                        return None
                    clamped = max(0, min(100, vol))
                    return self._finish(
                        ts,
                        "volume_set",
                        f"громкость {clamped}",
                        norm,
                        False,
                        value=clamped,
                        keep_session=keep_session,
                    )

        # ------------------------------------------------------------------
        # 5. Multi-word non-parameterized media commands (instant, even on partials)
        # ------------------------------------------------------------------
        for phrase, action in self._phrases:
            if action in {"volume_up", "volume_down"}:
                continue
            if self._contains_phrase(padded, phrase):
                return self._finish(
                    ts, action, phrase, norm, is_partial, keep_session=keep_session
                )

        # ------------------------------------------------------------------
        # 6. Short standalone commands ("стоп", "продолжи", "дальше", "некст", "назад")
        # ------------------------------------------------------------------
        max_len = (self.standalone_max_words + 1) if keep_session else self.standalone_max_words
        if not is_partial and len(cmd_words) <= max_len:
            for word, action in self._standalone:
                if action in {"volume_up", "volume_down"}:
                    continue
                if self._contains_phrase(padded, word):
                    return self._finish(
                        ts, action, word, norm, False, keep_session=keep_session
                    )

        return None
