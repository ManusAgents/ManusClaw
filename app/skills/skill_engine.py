from __future__ import annotations

"""
SkillEngine - loads, manages, and injects skills into agent context.

Skills are Markdown files with YAML frontmatter:
  ---
  name: skill_name
  description: What this skill does
  version: 1.0.0
  tags: [github, devops]
  ---

  # Skill content here...

Skills are injected as USER messages (not system prompt) to preserve prompt caching.
After N+ tool calls in a session, the agent suggests creating a skill.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.logger import logger

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_DEFAULT_SKILLS_DIR = str(Path.home() / ".manusclaw" / "skills")
_SKILLS_DIR = Path(os.getenv("MANUSCLAW_SKILLS_DIR", _DEFAULT_SKILLS_DIR))
_BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin"


@dataclass
class Skill:
    name: str
    description: str
    version: str = "1.0.0"
    content: str = ""
    tags: list[str] = field(default_factory=list)
    required_config: list[str] = field(default_factory=list)
    platform: list[str] = field(default_factory=list)
    path: Optional[Path] = None
    enabled: bool = True

    def to_user_message(self) -> str:
        return (
            f"[SKILL LOADED: {self.name}]\n"
            f"Description: {self.description}\n\n"
            f"{self.content}\n"
            f"[END SKILL: {self.name}]"
        )

    def to_frontmatter(self) -> str:
        lines = [
            "---",
            f"name: {self.name}",
            f"description: {self.description}",
            f"version: {self.version}",
            f"tags: [{', '.join(self.tags)}]",
            f"required_config: [{', '.join(self.required_config)}]",
            f"platform: [{', '.join(self.platform)}]",
            "---",
        ]
        return "\n".join(lines)

    def to_file_content(self) -> str:
        return self.to_frontmatter() + "\n\n" + self.content


def _parse_skill_file(path: Path) -> Optional[Skill]:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None

    meta: dict = {}
    content = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm_raw = parts[1].strip()
            content = parts[2].strip()
            if _HAS_YAML:
                try:
                    meta = _yaml.safe_load(fm_raw) or {}
                except Exception:
                    meta = _simple_yaml_parse(fm_raw)
            else:
                meta = _simple_yaml_parse(fm_raw)

    name = meta.get("name", path.stem)
    tags_raw = meta.get("tags", [])
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.strip("[]").split(",") if t.strip()]

    return Skill(
        name=str(name),
        description=str(meta.get("description", "")),
        version=str(meta.get("version", "1.0.0")),
        content=content,
        tags=tags_raw if isinstance(tags_raw, list) else [],
        required_config=meta.get("required_config", []),
        platform=meta.get("platform", []),
        path=path,
        enabled=bool(meta.get("enabled", True)),
    )


def _simple_yaml_parse(text: str) -> dict:
    strip_chars = chr(34) + chr(39)
    result: dict = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip().strip(strip_chars)
    return result


class SkillEngine:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_builtin()
        self._load_user()
        self._loaded = True

    def _load_builtin(self) -> None:
        if not _BUILTIN_SKILLS_DIR.exists():
            return
        for p in _BUILTIN_SKILLS_DIR.rglob("*.md"):
            skill = _parse_skill_file(p)
            if skill and skill.enabled:
                self._skills[skill.name] = skill
        logger.debug(f"[SkillEngine] Loaded {len(self._skills)} built-in skills")

    def _load_user(self) -> None:
        if not _SKILLS_DIR.exists():
            return
        for p in _SKILLS_DIR.rglob("*.md"):
            skill = _parse_skill_file(p)
            if skill and skill.enabled:
                self._skills[skill.name] = skill
        logger.debug(f"[SkillEngine] Total skills: {len(self._skills)}")

    def list_skills(self) -> list[Skill]:
        self._ensure_loaded()
        return [s for s in self._skills.values() if s.enabled]

    def get(self, name: str) -> Optional[Skill]:
        self._ensure_loaded()
        return self._skills.get(name)

    def create(self, name: str, description: str, content: str,
               tags: Optional[list[str]] = None, version: str = "1.0.0") -> Skill:
        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill = Skill(
            name=name, description=description, version=version,
            content=content, tags=tags or [],
            path=_SKILLS_DIR / f"{name}.md",
        )
        if skill.path:
            skill.path.write_text(skill.to_file_content(), encoding="utf-8")
        self._skills[name] = skill
        logger.info(f"[SkillEngine] Created skill: {name}")
        return skill

    def patch(self, name: str, content: Optional[str] = None,
              description: Optional[str] = None, version: Optional[str] = None) -> Optional[Skill]:
        self._ensure_loaded()
        skill = self._skills.get(name)
        if not skill:
            return None
        if content is not None:
            skill.content = content
        if description is not None:
            skill.description = description
        if version is not None:
            skill.version = version
        if skill.path:
            skill.path.write_text(skill.to_file_content(), encoding="utf-8")
        logger.info(f"[SkillEngine] Patched skill: {name}")
        return skill

    def delete(self, name: str) -> bool:
        self._ensure_loaded()
        skill = self._skills.pop(name, None)
        if skill and skill.path and skill.path.exists():
            skill.path.unlink()
            logger.info(f"[SkillEngine] Deleted skill: {name}")
            return True
        return bool(skill)

    def get_relevant(self, goal: str, max_skills: int = 3) -> list[Skill]:
        self._ensure_loaded()
        goal_lower = goal.lower()
        words = set(re.findall(r"\w+", goal_lower))
        ranked: list[tuple[int, Skill]] = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            skill_text = (skill.description + " " + " ".join(skill.tags)).lower()
            skill_words = set(re.findall(r"\w+", skill_text))
            overlap = len(words & skill_words)
            if overlap > 0:
                ranked.append((overlap, skill))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in ranked[:max_skills]]

    def should_suggest_skill(self, tool_call_count: int) -> bool:
        return tool_call_count >= 5 and tool_call_count % 5 == 0

    def suggest_skill_message(self, session_summary: str = "") -> str:
        return (
            "SKILL SUGGESTION: You have completed 5+ tool calls in this session. "
            "Consider creating a skill to capture this workflow for future reuse.\n"
            "Use the skill_manager tool with action='create' to save this knowledge.\n"
            + (f"Session so far:\n{session_summary[:400]}" if session_summary else "")
        )


_engine: Optional[SkillEngine] = None


def get_skill_engine() -> SkillEngine:
    global _engine
    if _engine is None:
        _engine = SkillEngine()
    return _engine
