from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..models import RunTrace, TargetAppProfile


class ArtifactStore:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        output = self.runtime_dir / run_id
        output.mkdir(parents=True, exist_ok=True)
        for name in ("screens", "layouts", "commands", "generated", "hypium", "reports"):
            (output / name).mkdir(exist_ok=True)
        return output

    def write_json(self, path: Path, data: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def save_trace(self, trace: RunTrace) -> Path:
        return self.write_json(self.run_dir(trace.run_id) / "trace.json", trace)

    def load_trace(self, run_id: str) -> RunTrace:
        path = self.run_dir(run_id) / "trace.json"
        return RunTrace.model_validate_json(path.read_text(encoding="utf-8-sig"))

    @staticmethod
    def load_profile(path: Path) -> TargetAppProfile:
        return TargetAppProfile.model_validate_json(path.read_text(encoding="utf-8-sig"))
