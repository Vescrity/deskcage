"""Real tmpbind overlay: reads pass through, writes stay in the sandbox."""

from __future__ import annotations

import sys
import time

import harness

from deskcage import sandbox
from deskcage.instance import Instance
from deskcage.profile import Profile


def log_of(instance: Instance, pid: int) -> str:
    info = instance.process(pid)
    assert info is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return open(info.log, encoding="utf-8", errors="replace").read()
        except OSError:
            time.sleep(0.02)
    return ""


def main() -> int:
    if not sandbox.overlay_supported():
        print("tmpbind: skipped (overlayfs is not available to user namespaces)")
        return 0

    with harness.TestEnv() as env:
        source = env.dir / "data"
        source.mkdir()
        (source / "lower.txt").write_text("lower\n", encoding="utf-8")

        profile = Profile(
            id="tb",
            bus="none",
            home="tmp",
            keep_alive=False,
            tmpbind=[str(source)],
        )
        instance = Instance("tb-1", profile, env.address)
        instance.start()
        try:
            info = instance.spawn(
                [
                    "sh",
                    "-c",
                    f"cat {source}/lower.txt; "
                    f"echo written > {source}/upper.txt; "
                    f"test -f {source}/upper.txt && echo writable",
                ]
            )
            harness.wait_until(lambda: instance.process(info.pid).status is not None)
            process = instance.process(info.pid)
            assert process is not None and process.status == 0, process
            text = log_of(instance, info.pid)
            assert "lower" in text and "writable" in text, text
            assert (source / "lower.txt").read_text(encoding="utf-8") == "lower\n"
            assert not (source / "upper.txt").exists()
        finally:
            instance.stop()
    print("tmpbind: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
