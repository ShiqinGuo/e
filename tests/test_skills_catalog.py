import pytest

from agent_client.application.skills import SkillCatalog


@pytest.mark.asyncio
async def test_skill_identity_hash_and_resource_scope(tmp_path):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
        (root / "SKILL.md").write_text(
            "---\nname: same\ndescription: testing skills\n---\nBody", encoding="utf-8"
        )
    catalog = SkillCatalog(roots)
    await catalog.scan()
    assert len(catalog.search("same")) == 2
    first = await catalog.load("r0/SKILL.md")
    assert not first.already_loaded
    assert (await catalog.load("r0/SKILL.md")).already_loaded
    with pytest.raises(ValueError, match="escapes"):
        await catalog.resource("r0/SKILL.md", "../outside.txt")
