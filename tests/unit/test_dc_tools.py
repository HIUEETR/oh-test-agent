"""DC 工具注册与 build_tools 测试。"""

from __future__ import annotations

from harmony_test_agent.dc.models import TIER_TOOLS, TOOL_TIER, DcToolName, DcToolTier, tools_up_to
from harmony_test_agent.dc.tools import _TOOL_REGISTRY, build_tools


class TestTierTools:
    """TIER_TOOLS 字典是 tier→tool 映射的单一数据源。"""

    def test_l1_has_8_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L1]) == 8

    def test_l2_has_6_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L2]) == 6

    def test_l3_has_5_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L3]) == 5

    def test_l4_has_3_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L4]) == 3

    def test_l5_has_1_tool(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L5]) == 1

    def test_total_23_tools(self) -> None:
        total = sum(len(tools) for tools in TIER_TOOLS.values())
        assert total == 23

    def test_all_tool_names_unique(self) -> None:
        all_tools = [tool for tools in TIER_TOOLS.values() for tool in tools]
        assert len(all_tools) == len(set(all_tools))

    def test_tool_tier_reverse_mapping(self) -> None:
        for tier, tools in TIER_TOOLS.items():
            for tool in tools:
                assert TOOL_TIER[tool] == tier


class TestToolsUpTo:
    """tools_up_to 返回 level ≤ tier 的全部工具。"""

    def test_l1_returns_8(self) -> None:
        assert len(tools_up_to(DcToolTier.L1)) == 8

    def test_l2_returns_14(self) -> None:
        assert len(tools_up_to(DcToolTier.L2)) == 14

    def test_l3_returns_19(self) -> None:
        assert len(tools_up_to(DcToolTier.L3)) == 19

    def test_l4_returns_22(self) -> None:
        assert len(tools_up_to(DcToolTier.L4)) == 22

    def test_l5_returns_23(self) -> None:
        assert len(tools_up_to(DcToolTier.L5)) == 23

    def test_l1_contains_click(self) -> None:
        assert DcToolName.CLICK in tools_up_to(DcToolTier.L1)

    def test_l5_contains_execute_shell(self) -> None:
        assert DcToolName.EXECUTE_SHELL in tools_up_to(DcToolTier.L5)

    def test_l1_does_not_contain_execute_shell(self) -> None:
        assert DcToolName.EXECUTE_SHELL not in tools_up_to(DcToolTier.L1)


class TestToolRegistry:
    """_TOOL_REGISTRY 必须包含全部 23 个工具。"""

    def test_registry_has_23_entries(self) -> None:
        assert len(_TOOL_REGISTRY) == 23

    def test_every_tool_name_in_registry(self) -> None:
        for tool_name in DcToolName:
            assert tool_name in _TOOL_REGISTRY, f"{tool_name} missing from registry"

    def test_registry_entries_are_callable(self) -> None:
        for tool_name, (func, desc) in _TOOL_REGISTRY.items():
            assert callable(func), f"{tool_name} function is not callable"
            assert isinstance(desc, str) and len(desc) > 0, f"{tool_name} description is empty"


class TestBuildTools:
    """build_tools 返回 pydantic-ai Tool 对象列表。"""

    def test_l1_returns_8_tools(self) -> None:
        tools = build_tools(DcToolTier.L1)
        assert len(tools) == 8

    def test_l5_returns_23_tools(self) -> None:
        tools = build_tools(DcToolTier.L5)
        assert len(tools) == 23

    def test_tool_names_match_tier(self) -> None:
        tools = build_tools(DcToolTier.L2)
        tool_names = {t.name for t in tools}
        expected = {name.value for name in tools_up_to(DcToolTier.L2)}
        assert tool_names == expected

    def test_l1_does_not_include_shell(self) -> None:
        tools = build_tools(DcToolTier.L1)
        tool_names = {t.name for t in tools}
        assert "execute_shell" not in tool_names

    def test_l5_includes_shell(self) -> None:
        tools = build_tools(DcToolTier.L5)
        tool_names = {t.name for t in tools}
        assert "execute_shell" in tool_names
