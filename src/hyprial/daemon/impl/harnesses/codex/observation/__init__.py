"""Producer-local Codex quiet observation, before lossy progress fan-out."""
import asyncio
import math
import time

from hyprial.daemon.impl.harnesses.streaming.protocol import ProgressObservation
from hyprial.daemon.impl.harnesses.codex.process import _codex_progress_observation, _notification_turn_id


class CodexTurnObservationMixin:
    def read_run_activity(self):
        # One immutable tuple assignment publishes the condition. A new native
        # turn must never inherit the old native turn's quiet watch.
        current = getattr(self, "_run_activity", None)
        if current is None or current[0] != self._active_turn_id:
            return None
        threshold = self._turn_idle_timeout_seconds
        return (*current[1:], max(1, math.ceil(threshold * 1000)) if math.isfinite(threshold) else None)

    async def _turn_events(self, turn_id):
        """Yield existing progress/terminal values, retaining diagnostic facts."""
        threshold = self._turn_idle_timeout_seconds
        last_activity = asyncio.get_running_loop().time()
        next_stall_report = last_activity + threshold
        stalled = False
        self._run_activity = (turn_id, False, None)
        while True:
            now = asyncio.get_running_loop().time()
            if now >= next_stall_report:
                stalled = True
                self._run_activity = (turn_id, True, self._run_activity[2])
                next_stall_report = now + threshold
                quiet = now - last_activity
                yield ProgressObservation(
                    phase="turn-stalled",
                    summary=(f"Codex turn quiet for {quiet:.0f}s (no correlated "
                             f"app-server activity; sensitivity {threshold:g}s)"),
                    detail={"quietSeconds": round(quiet, 3), "thresholdSeconds": threshold,
                            "detector": "producer-quiet-watch"},
                )
            try:
                message = await asyncio.wait_for(self._notifications.get(), timeout=0.5)
            except TimeoutError:
                # Polling level-state is not activity. Keep the existing watch.
                try:
                    polled = await self._read_turn(turn_id)
                except ConnectionError as error:
                    if "was not found" not in str(error):
                        raise
                else:
                    if polled.get("status") not in {None, "inProgress"}:
                        yield ProgressObservation(
                            phase="turn-end",
                            summary=("Codex turn completed" if polled.get("status") == "completed"
                                     else f"Codex turn ended ({polled.get('status')})"),
                            terminal=True,
                        )
                        yield polled
                        return
                continue
            if isinstance(message, BaseException):
                raise message
            params = message.get("params")
            if isinstance(params, dict) and _notification_turn_id(params) == turn_id:
                # Publish before yielding progress. A dropped route-C event
                # cannot hide the producer's observed activity or quiet flag.
                self._run_activity = (turn_id, False, time.time_ns() // 1_000_000)
                if stalled:
                    stalled = False
                    quiet = asyncio.get_running_loop().time() - last_activity
                    yield ProgressObservation(
                        phase="turn-resumed",
                        summary=f"Codex turn active again after {quiet:.0f}s quiet",
                        detail={"quietSeconds": round(quiet, 3), "thresholdSeconds": threshold,
                                "detector": "producer-quiet-watch"},
                    )
                last_activity = asyncio.get_running_loop().time()
                next_stall_report = last_activity + threshold
            if message.get("method") != "turn/completed":
                observation = _codex_progress_observation(message, thread_id=self._require_thread_id(), turn_id=turn_id)
                if observation is not None:
                    yield observation
                continue
            turn = params.get("turn") if isinstance(params, dict) else None
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                observation = _codex_progress_observation(message, thread_id=self._require_thread_id(), turn_id=turn_id)
                if observation is not None:
                    yield observation
                yield turn
                return
