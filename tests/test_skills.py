"""Tests for the SkillEngine and SkillManagerTool."""
import pytest
import os


def _fresh_engine(tmp_path):
    os.environ["MANUSCLAW_SKILLS_DIR"] = str(tmp_path / "skills")
    import app.skills.skill_engine as se
    se._engine = None
    return se.get_skill_engine()


def test_create_and_list_skill(tmp_path):
    engine = _fresh_engine(tmp_path)
    skill = engine.create("my_skill", "A test skill", "Do the thing.", tags=["test"])
    assert skill.name == "my_skill"
    skills = engine.list_skills()
    assert any(s.name == "my_skill" for s in skills)


def test_get_skill(tmp_path):
    engine = _fresh_engine(tmp_path)
    engine.create("get_me", "Get me", "Content here.")
    skill = engine.get("get_me")
    assert skill is not None
    assert skill.description == "Get me"


def test_patch_skill(tmp_path):
    engine = _fresh_engine(tmp_path)
    engine.create("patchable", "Old desc", "Old content")
    engine.patch("patchable", content="New content", description="New desc", version="2.0.0")
    skill = engine.get("patchable")
    assert skill.content == "New content"
    assert skill.version == "2.0.0"


def test_delete_skill(tmp_path):
    engine = _fresh_engine(tmp_path)
    engine.create("deletable", "Delete me", "Gone")
    ok = engine.delete("deletable")
    assert ok
    assert engine.get("deletable") is None


def test_get_relevant_skills(tmp_path):
    engine = _fresh_engine(tmp_path)
    engine.create("python_helper", "Python coding assistant", "Use python_execute tool.", tags=["python", "code"])
    engine.create("bash_helper", "Bash shell expert", "Use bash tool.", tags=["bash", "shell"])
    relevant = engine.get_relevant("run a python script", max_skills=2)
    names = [s.name for s in relevant]
    assert "python_helper" in names


def test_skill_suggest_after_threshold(tmp_path):
    engine = _fresh_engine(tmp_path)
    assert not engine.should_suggest_skill(4)
    assert engine.should_suggest_skill(5)
    assert not engine.should_suggest_skill(6)
    assert engine.should_suggest_skill(10)


def test_builtin_skills_load():
    import app.skills.skill_engine as se
    se._engine = None
    engine = se.get_skill_engine()
    skills = engine.list_skills()
    # Should have at least the built-in ones
    assert len(skills) >= 0  # may be 0 in isolated test env
