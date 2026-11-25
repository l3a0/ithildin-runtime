"""What ci.yml's `aws` job must keep, which the job cannot show for itself, for #162.

The job runs only on a push to `main`. It stays green with `require-checksums` removed,
or misspelled, since GitHub only warns about an input an action does not name. It also
stays green with both that and `version` removed. This reads the workflow alone, so it
cannot see whether the pinned commit honours an input, and setup-uv before 10.4.0
ignores `require-checksums`.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_the_aws_jobs_setup_uv_step_is_pinned_and_takes_only_these_inputs() -> None:
    job = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["aws"]
    (step,) = [step for step in job["steps"] if "setup-uv" in step.get("uses", "")]
    # A name changes nothing. `if`, `env` or `continue-on-error` could skip the check.
    assert set(step) - {"name", "id"} == {"uses", "with"}
    assert re.fullmatch(r"astral-sh/setup-uv@[0-9a-f]{40}", step["uses"])
    # Every input is named, so none that supplies another checksum or list, such as
    # `checksum` or `manifest-file`, can join them here.
    assert step["with"] == {
        "version": "latest-known",
        "require-checksums": True,
        "enable-cache": False,
    }
