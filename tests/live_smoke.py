"""Opt-in live CLI smoke test. Uses your CLI logins; never part of unit tests."""
import concurrent.futures
import tempfile
from pathlib import Path

from sovet.core import Project
from sovet.runtime import NativeRun


def main():
    root = Path(tempfile.mkdtemp(prefix="sovet-live-"))
    p = Project.init(root, brief="Smoke marker: SOVET_SHARED_OK")
    p.config["turn_timeout"] = 180
    p.save_config()
    print(f"Live test project: {root}", flush=True)

    def check(role):
        for turn in range(2):
            text = ("Use the sovet memory_read MCP tool once, then reply only with the Smoke marker "
                    "from shared brief. Do not edit files or delegate.") if turn == 0 else (
                    "Reply only with the same Smoke marker as your previous answer. Do not call any tools or delegate.")
            p.enqueue(role, text, read_only=True)
            job = p.claim(role)
            result = NativeRun().run(p, role, job, lambda text: print(f"[{role}] {text}", flush=True))
            p.finish(job, result)
            assert "SOVET_SHARED_OK" in result, result
            if turn == 0:
                log = (p.team / "logs" / f"{role}-{job['id']}.jsonl").read_text()
                assert "memory_read" in log and '"is_error":true' not in log.replace(" ", ""), log[-5000:]
            sid = p.agent(role)["session"]
            if turn == 0:
                initial = sid
            assert sid and sid == initial, (initial, sid)
        return f"{role}: two real turns, MCP shared memory, native session {initial}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(check, ("codex", "claude")):
            print(result, flush=True)


if __name__ == "__main__":
    main()
