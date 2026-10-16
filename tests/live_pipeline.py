"""Opt-in real writer -> reviewer -> writer round trip in an isolated project."""
from pathlib import Path
from tempfile import mkdtemp

from sovet.core import Project
from sovet.runtime import execute_next


def main():
    p = Project.init(Path(mkdtemp(prefix="sovet-pipeline-")))
    p.config["turn_timeout"] = 180
    p.save_config()
    print(f"Pipeline project: {p.root}", flush=True)
    p.enqueue("claude", "Smoke test: use artifact_read then artifact_write to create result.txt containing exactly "
              "SOVET_PIPELINE_OK followed by a newline. Then use ask_peer to ask Codex to read that file via "
              "artifact_read and respond REVIEW_OK if it matches. Do not do other work or launch any nested CLI. "
              "Finish your current turn after queuing the peer request.")
    for role in ("claude", "codex", "claude"):
        result = execute_next(p, role, lambda text: print(f"[{role}] {text}", flush=True))
        assert result and result["ok"], result
    assert (p.root / "result.txt").read_text() == "SOVET_PIPELINE_OK\n"
    assert not [j for j in p.jobs() if j["status"] in ("pending", "running")]
    assert any(e["actor"] == "codex" and "REVIEW_OK" in e["body"] for e in p.events())
    assert all(p.agent(role)["session"] for role in ("codex", "claude"))
    print("PASS: Claude wrote -> Codex reviewed -> same Claude session received the review", flush=True)


if __name__ == "__main__":
    main()
