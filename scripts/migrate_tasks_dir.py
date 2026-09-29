"""Migrate an ldb-tasks checkout to the Task / Design Phase / Evaluation Phase layout.

Usage: python migrate_tasks_dir.py <tasks-root>

Per task directory:
  problem.yaml -> task.yaml  (keys: examples -> problems,
                              existing_library_examples -> existing_library_problems)
  phase_1/     -> design/
  phase_2/     -> evaluation/
Uses `git mv` so history follows the files.
"""

import re
import subprocess
import sys
from pathlib import Path

KEYS = {
    "examples": "problems",
    "existing_library_examples": "existing_library_problems",
}
MOVES = {"problem.yaml": "task.yaml", "phase_1": "design", "phase_2": "evaluation"}


def main(root: Path) -> None:
    """Migrate every task directory directly under `root`."""
    for spec in sorted(root.glob("*/problem.yaml")):
        task_dir = spec.parent
        text = spec.read_text(encoding="utf-8")
        for old, new in KEYS.items():
            text = re.sub(rf"(?m)^{old}:", f"{new}:", text)
        spec.write_text(text, encoding="utf-8")
        for old, new in MOVES.items():
            if (task_dir / old).exists():
                subprocess.run(["git", "-C", str(task_dir), "mv", old, new], check=True)
        print(f"migrated {task_dir.name}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
