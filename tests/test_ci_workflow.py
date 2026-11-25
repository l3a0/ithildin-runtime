"""What ci.yml's `aws` job must keep, which the job cannot show for itself, for #162.

The job runs only on a push to `main`, and it stays green with the two inputs below
removed or misspelled, because GitHub only warns about an input an action does not name.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_the_aws_job_runs_only_a_uv_the_pinned_commit_holds_a_checksum_for() -> None:
    job = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["aws"]
    (step,) = [step for step in job["steps"] if "setup-uv" in step.get("uses", "")]
    assert set(step) == {"uses", "with"}
    assert re.fullmatch(r"astral-sh/setup-uv@[0-9a-f]{40}", step["uses"])
    # Every input is named, so none that supplies another checksum or list, such as
    # `checksum` or `manifest-file`, can join them.
    assert step["with"] == {
        "version": "latest-known",
        "require-checksums": True,
        "enable-cache": False,
    }
