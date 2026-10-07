from pathlib import Path
from typing import List

from pydantic import BaseModel, Field


class SkillFrontmatter(BaseModel):
    skill_id: str
    agent_types: List[str] = Field(default_factory=list)
    trigger_keywords: List[str] = Field(default_factory=list)
    priority: int = Field(default=0, ge=0, le=100)
    content: str


def _parse_scalar(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip('"\'') for item in value[1:-1].split(",") if item.strip()]
    if value.isdigit():
        return int(value)
    return value.strip('"\'')


class SkillLoader:
    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.skills = self._load_all()

    def load(self, path: str) -> SkillFrontmatter:
        text = Path(path).read_text(encoding="utf-8")
        metadata = {}
        body = text
        if text.startswith("---"):
            _, frontmatter, body = text.split("---", 2)
            for line in frontmatter.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata[key.strip()] = _parse_scalar(value)
        metadata["content"] = body.strip()
        return SkillFrontmatter.model_validate(metadata)

    def _load_all(self):
        if not self.directory.exists():
            return []
        return [self.load(str(path)) for path in sorted(self.directory.glob("*.md"))]

    def match(self, agent_type: str, user_message: str):
        message = user_message.lower()
        matches = []
        for skill in self.skills:
            allowed = "*" in skill.agent_types or agent_type in skill.agent_types
            triggered = not skill.trigger_keywords or any(word.lower() in message for word in skill.trigger_keywords)
            if allowed and triggered:
                matches.append(skill)
        return sorted(matches, key=lambda skill: skill.priority, reverse=True)

